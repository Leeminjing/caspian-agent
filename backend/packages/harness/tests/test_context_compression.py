"""
本文件提供上下文压缩功能的测试:纯函数(切点/锚点/校验/prompt/剪枝)与中间件集成
(预防压缩、幂等、溢出恢复、fail-soft)。

输入: 无(自包含,stub 模型与 InMemorySaver 本地构造)
输出: 测试通过/失败

运行: 仓库根执行
    backend\\packages\\harness\\.venv\\Scripts\\python.exe -m pytest backend/packages/harness/tests/test_context_compression.py
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from langchain.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, RemoveMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from caspian.agents.middlewares.context_compression import (
    ContextCompressionMiddleware,
    _is_overflow,
    append_archive,
    read_archive,
)
from caspian.agents.middlewares.context_compression_plan import (
    CONTRACT_TAG,
    DECISION_TABLE_MESSAGE_ID,
    SUMMARY_MARKER_KEY,
    SUMMARY_MESSAGE_ID,
    SUMMARY_PROMPT_TEMPLATE,
    _PRUNE_NOTICE,
    build_summary_message,
    compute_cutoff,
    is_anchor,
    make_token_counter,
    plan_compression,
    prune_large_tool_messages,
    render_side_channels,
    verify_shrink,
)
from caspian.config.context_compression_config import ContextCompressionConfig


def _ai(tool_call_ids=("tc-1",), msg_id="ai-1"):
    return AIMessage(
        content="",
        id=msg_id,
        tool_calls=[{"name": "bash", "args": {}, "id": tid} for tid in tool_call_ids],
    )


def _tool(tid="tc-1"):
    return ToolMessage(content="result", tool_call_id=tid, id=f"tool-{tid}")


def _human(uid):
    return HumanMessage(content=f"u{uid}", id=f"h{uid}")


class PlanFunctionTests(unittest.TestCase):
    """纯函数用例:切点规则、锚点、后置校验、prompt 组装、剪枝。"""

    def test_cutoff_advances_over_tool_message_block(self):
        seq = [_human(1), _ai(), _tool(), _tool("tc-2"), _human(2)]
        cutoff = compute_cutoff(seq, keep_messages=2)
        self.assertIsNotNone(cutoff)
        self.assertEqual(seq[cutoff].id, "h2")
        # 配对不变量:ai(tc-1) 与其 ToolMessage 在边界同一侧
        summarized = seq[:cutoff]
        preserved = seq[cutoff:]
        for message in summarized:
            if isinstance(message, AIMessage) and message.tool_calls:
                ids = {tc.get("id") for tc in message.tool_calls}
                self.assertFalse(
                    any(
                        isinstance(m, ToolMessage) and m.tool_call_id in ids
                        for m in preserved
                    )
                )
        for message in preserved:
            if isinstance(message, ToolMessage):
                self.assertFalse(any(m.id == "ai-1" and message.tool_call_id == "tc-1" for m in summarized))

    def test_cutoff_advances_past_ai_tool_pair(self):
        seq = [_human(1), _ai(), _tool(), _human(2)]
        cutoff = compute_cutoff(seq, keep_messages=2)
        self.assertIsNotNone(cutoff)
        self.assertEqual(seq[cutoff].id, "h2")

    def test_cutoff_aligns_to_human_turn(self):
        seq = [_human(1), _ai(), _human(2), _ai(msg_id="ai-2"), _tool()]
        cutoff = compute_cutoff(seq, keep_messages=2)
        self.assertIsNotNone(cutoff)
        self.assertIsInstance(seq[cutoff], HumanMessage)

    def test_cutoff_none_when_too_few_messages(self):
        seq = [_human(1), _ai(), _tool()]
        self.assertIsNone(compute_cutoff(seq, keep_messages=5))

    def test_anchors_move_to_preserved(self):
        contract = HumanMessage(content=f"{CONTRACT_TAG}\n合同\n</task_contract>", id="h1")
        table = SystemMessage(content='<decision_table version="v1">', id=DECISION_TABLE_MESSAGE_ID)
        seq = [contract, table, _human(2), _ai(), _tool(), _human(3)]
        plan = plan_compression(seq, keep_messages=2)
        self.assertIsNotNone(plan)
        to_summarize, preserved = plan
        self.assertIn(contract, preserved)
        self.assertIn(table, preserved)
        self.assertNotIn(contract, to_summarize)
        self.assertNotIn(table, to_summarize)

    def test_plan_none_when_only_anchors_before_cutoff(self):
        contract = HumanMessage(content=f"{CONTRACT_TAG}\n合同\n</task_contract>", id="h1")
        table = SystemMessage(content='<decision_table version="v1">', id=DECISION_TABLE_MESSAGE_ID)
        seq = [contract, table, _human(2)]
        self.assertIsNone(plan_compression(seq, keep_messages=2))

    def test_verify_shrink(self):
        counter = make_token_counter()
        big_list = [HumanMessage(content="x" * 1000, id="big")]
        small_msg = HumanMessage(content="x" * 10, id="small")
        self.assertTrue(verify_shrink(small_msg, big_list, counter))
        self.assertFalse(verify_shrink(big_list[0], [small_msg], counter))

    def test_build_summary_message_marker_and_id(self):
        summary = build_summary_message("摘要正文")
        self.assertTrue(summary.id.startswith(SUMMARY_MESSAGE_ID + "-"))
        self.assertNotEqual(summary.id, build_summary_message("摘要正文").id)
        self.assertTrue(summary.additional_kwargs.get(SUMMARY_MARKER_KEY))
        self.assertEqual(summary.additional_kwargs.get("lc_source"), "summarization")
        self.assertIn("摘要正文", str(summary.content))

    def test_render_side_channels_empty_state(self):
        text = render_side_channels({})
        self.assertIn("已 present 文件列表", text)
        self.assertIn("任务合同: 无", text)
        self.assertIn("决策等级表版本: 无", text)

    def test_render_side_channels_full_state(self):
        table = SystemMessage(content='<decision_table version="abc123">', id=DECISION_TABLE_MESSAGE_ID)
        text = render_side_channels({
            "artifacts": ["/mnt/user-data/outputs/a.md"],
            "delegations": [{
                "id": "tc-1",
                "description": "调研",
                "subagent_type": "general-purpose",
                "status": "completed",
                "created_at": "2026-08-16T00:00:00Z",
            }],
            "task_contract": "合同内容" * 100,
            "messages": [table],
        })
        self.assertIn("/mnt/user-data/outputs/a.md", text)
        self.assertIn("已存在", text)
        self.assertIn("abc123", text)
        self.assertIn("completed", text)

    def test_prune_large_tool_messages(self):
        counter = make_token_counter()
        long_tool = ToolMessage(content="y" * 5000, tool_call_id="tc-9", id="tool-9")
        pruned = prune_large_tool_messages(
            [long_tool], prune_max_chars=800, token_counter=counter
        )
        self.assertIsNotNone(pruned)
        new_messages, replacement = pruned
        self.assertEqual(replacement.tool_call_id, "tc-9")
        self.assertEqual(replacement.id, "tool-9")
        self.assertEqual(len(replacement.content), 800 + len(_PRUNE_NOTICE))
        self.assertIs(new_messages[0], replacement)

    def test_prune_returns_none_when_nothing_long(self):
        counter = make_token_counter()
        short = ToolMessage(content="short", tool_call_id="tc-1", id="tool-1")
        self.assertIsNone(
            prune_large_tool_messages([short], prune_max_chars=800, token_counter=counter)
        )

    def test_prompt_template_placeholders(self):
        rendered = SUMMARY_PROMPT_TEMPLATE.format(side_channels="S", history="H")
        self.assertIn("S", rendered)
        self.assertIn("H", rendered)
        self.assertIn("PRESERVE", rendered)
        self.assertIn("## 用户目标", rendered)

    def test_is_anchor_negative(self):
        self.assertFalse(is_anchor(_human(1)))
        self.assertFalse(is_anchor(_ai()))
        self.assertFalse(is_anchor(_tool()))
        self.assertFalse(
            is_anchor(SystemMessage(content="普通系统消息", id="other"))
        )

    def test_is_anchor_positive_for_summary(self):
        summary = build_summary_message("摘要")
        self.assertTrue(is_anchor(summary))

    def test_old_summary_preserved_on_second_compression(self):
        old_summary = build_summary_message("旧摘要")
        seq = [old_summary, _human(2), _ai(), _tool(), _human(3)]
        plan = plan_compression(seq, keep_messages=2)
        self.assertIsNotNone(plan)
        to_summarize, preserved = plan
        self.assertIn(old_summary, preserved)
        self.assertNotIn(old_summary, to_summarize)


class FakeOverflow(Exception):
    """模拟 provider 400 上下文溢出异常。"""

    status_code = 400

    def __init__(self) -> None:
        super().__init__("This model's maximum context length is 8192 tokens.")


class StubChatModel(BaseChatModel):
    """按输入形状分派主/摘要角色,支持可编程溢出与摘要失败。"""

    def __init__(
        self,
        *,
        main_texts=("回答",),
        summary_text="## 用户目标\n完成测试",
        overflow_after=0,
        fail_summary=False,
    ) -> None:
        super().__init__()
        # BaseChatModel 为 Pydantic 模型,非字段属性须绕过校验赋值
        object.__setattr__(self, "main_texts", list(main_texts))
        object.__setattr__(self, "summary_text", summary_text)
        object.__setattr__(self, "overflow_after", overflow_after)  # 前 N 次调用抛溢出,0=不抛
        object.__setattr__(self, "fail_summary", fail_summary)
        object.__setattr__(self, "main_calls", 0)
        object.__setattr__(self, "summary_calls", 0)

    @property
    def _llm_type(self) -> str:
        return "stub-compression"

    @staticmethod
    def _is_summary_call(messages) -> bool:
        if len(messages) != 1:
            return False
        content = messages[0].content
        text = content if isinstance(content, str) else str(content)
        return "对话历史压缩助手" in text

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError("stub 仅异步")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if self._is_summary_call(messages):
            object.__setattr__(self, "summary_calls", self.summary_calls + 1)
            if self.fail_summary:
                raise RuntimeError("summary boom")
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content=self.summary_text))]
            )
        object.__setattr__(self, "main_calls", self.main_calls + 1)
        if self.overflow_after and self.main_calls <= self.overflow_after:
            raise FakeOverflow()
        text = self.main_texts[min(self.main_calls - 1, len(self.main_texts) - 1)]
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=text))]
        )


def _cfg(**kwargs) -> ContextCompressionConfig:
    defaults = dict(
        enabled=True,
        trigger_tokens=200,
        keep_messages=2,
        max_tokens_to_summarize=4000,
        summary_timeout_seconds=5,
        prune_max_chars=800,
        recovery_max_attempts=1,
    )
    defaults.update(kwargs)
    return ContextCompressionConfig(**defaults)


class MiddlewareHookTests(unittest.IsolatedAsyncioTestCase):
    """中间件 before_model 钩子直测:压缩/幂等/锚点/fail-soft。"""

    def _state(self, messages):
        return {
            "messages": messages,
            "artifacts": [],
            "delegations": [],
            "task_contract": None,
        }

    async def test_abefore_model_below_threshold(self):
        summary_stub = StubChatModel()
        mw = ContextCompressionMiddleware(_cfg(), summary_model=summary_stub)
        result = await mw.abefore_model(
            self._state([HumanMessage(content="hi", id="h1")]), None
        )
        self.assertIsNone(result)
        self.assertEqual(summary_stub.summary_calls, 0)

    async def test_abefore_model_compresses_and_preserves_anchor(self):
        summary_stub = StubChatModel()
        mw = ContextCompressionMiddleware(_cfg(), summary_model=summary_stub)
        table = SystemMessage(content='<decision_table version="v1">', id="decision-table")
        state = self._state([
            table,
            HumanMessage(content="x" * 5000, id="h1"),
            HumanMessage(content="y" * 100, id="h2"),
            AIMessage(content="ok", id="ai1"),
            HumanMessage(content="z" * 100, id="h3"),
        ])
        update = await mw.abefore_model(state, None)
        self.assertIsNotNone(update)
        rebuilt = [m for m in update["messages"] if not isinstance(m, RemoveMessage)]
        ids = [m.id for m in rebuilt]
        self.assertTrue(any(i.startswith(SUMMARY_MESSAGE_ID + "-") for i in ids))
        self.assertIn("decision-table", ids)
        self.assertNotIn("h1", ids)
        self.assertIn("h3", ids)
        # 锚点逐字保留
        kept_table = next(m for m in rebuilt if m.id == "decision-table")
        self.assertEqual(kept_table.content, table.content)
        self.assertEqual(summary_stub.summary_calls, 1)

    async def test_abefore_model_idempotent(self):
        summary_stub = StubChatModel()
        mw = ContextCompressionMiddleware(_cfg(), summary_model=summary_stub)
        table = SystemMessage(content='<decision_table version="v1">', id="decision-table")
        state = self._state([
            table,
            HumanMessage(content="x" * 5000, id="h1"),
            HumanMessage(content="y" * 100, id="h2"),
            AIMessage(content="ok", id="ai1"),
            HumanMessage(content="z" * 100, id="h3"),
        ])
        update = await mw.abefore_model(state, None)
        self.assertIsNotNone(update)
        # 压缩后状态低于阈值 → 再次执行返回 None,不重复摘要
        compressed_state = self._state(
            [m for m in update["messages"] if not isinstance(m, RemoveMessage)]
        )
        result = await mw.abefore_model(compressed_state, None)
        self.assertIsNone(result)
        self.assertEqual(summary_stub.summary_calls, 1)

    async def test_abefore_model_fail_soft(self):
        summary_stub = StubChatModel(fail_summary=True)
        mw = ContextCompressionMiddleware(_cfg(), summary_model=summary_stub)
        state = self._state([
            HumanMessage(content="x" * 5000, id="h1"),
            HumanMessage(content="y" * 100, id="h2"),
            AIMessage(content="ok", id="ai1"),
            HumanMessage(content="z" * 100, id="h3"),
        ])
        result = await mw.abefore_model(state, None)
        self.assertIsNone(result)

    async def test_abefore_model_verify_shrink_skips(self):
        # 摘要比被替换原文更长 → 后置校验拒绝
        summary_stub = StubChatModel(summary_text="x" * 8000)
        mw = ContextCompressionMiddleware(_cfg(), summary_model=summary_stub)
        state = self._state([
            HumanMessage(content="x" * 5000, id="h1"),
            HumanMessage(content="y" * 100, id="h2"),
            AIMessage(content="ok", id="ai1"),
            HumanMessage(content="z" * 100, id="h3"),
        ])
        result = await mw.abefore_model(state, None)
        self.assertIsNone(result)
        self.assertEqual(summary_stub.summary_calls, 1)

    async def test_disabled_returns_none(self):
        mw = ContextCompressionMiddleware(_cfg(enabled=False))
        result = await mw.abefore_model(
            self._state([HumanMessage(content="x" * 5000, id="h1")]), None
        )
        self.assertIsNone(result)


class OverflowDetectionTests(unittest.TestCase):
    """溢出异常识别纯判定。"""

    def test_overflow_markers(self):
        self.assertTrue(_is_overflow(FakeOverflow()))
        self.assertTrue(_is_overflow(Exception("context_length_exceeded")))
        self.assertTrue(_is_overflow(Exception("最大上下文长度超限")))

    def test_non_overflow(self):
        class BadRequest(Exception):
            status_code = 400

        self.assertFalse(_is_overflow(BadRequest("bad request")))
        self.assertFalse(_is_overflow(ValueError("boom")))


class MiddlewareIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """create_agent + InMemorySaver 集成:压缩落盘、溢出恢复、阶梯耗尽。"""

    def _agent(self, main_stub, summary_stub, cfg):
        from langchain.agents import create_agent
        from langgraph.checkpoint.memory import InMemorySaver

        return create_agent(
            model=main_stub,
            tools=[],
            middleware=[
                ContextCompressionMiddleware(cfg, summary_model=summary_stub)
            ],
            checkpointer=InMemorySaver(),
        )

    async def test_integration_compress_in_run(self):
        main_stub = StubChatModel()
        summary_stub = StubChatModel()
        agent = self._agent(main_stub, summary_stub, _cfg())
        config = {"configurable": {"thread_id": "t1"}}
        await agent.ainvoke(
            {
                "messages": [
                    HumanMessage(content="a" * 5000, id="h1"),
                    HumanMessage(content="b", id="h2"),
                ]
            },
            config=config,
        )
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content="c", id="h3")]}, config=config
        )
        ids = [m.id for m in result["messages"]]
        self.assertTrue(any(i.startswith(SUMMARY_MESSAGE_ID + "-") for i in ids))
        self.assertNotIn("h1", ids)
        self.assertIn("h2", ids)
        self.assertEqual(summary_stub.summary_calls, 1)

    async def test_integration_overflow_recovery_prunes(self):
        main_stub = StubChatModel(overflow_after=1, main_texts=["ok"])
        summary_stub = StubChatModel()
        agent = self._agent(main_stub, summary_stub, _cfg(trigger_tokens=100000))
        long_tool = ToolMessage(content="t" * 5000, tool_call_id="tc-1", id="tool-1")
        result = await agent.ainvoke(
            {
                "messages": [
                    HumanMessage(content="任务", id="h1"),
                    long_tool,
                ]
            },
            config={"configurable": {"thread_id": "t1"}},
        )
        self.assertEqual(main_stub.main_calls, 2)
        tool_msg = next(m for m in result["messages"] if m.id == "tool-1")
        self.assertEqual(len(tool_msg.content), 800 + len(_PRUNE_NOTICE))
        self.assertEqual(summary_stub.summary_calls, 0)  # L0 已足够,未进 L1

    async def test_integration_overflow_recovers_via_summary(self):
        # 无超长 ToolMessage 可剪 → L1 摘要路径
        main_stub = StubChatModel(overflow_after=1, main_texts=["ok"])
        summary_stub = StubChatModel()
        agent = self._agent(main_stub, summary_stub, _cfg(trigger_tokens=100000))
        result = await agent.ainvoke(
            {
                "messages": [
                    HumanMessage(content="a" * 5000, id="h1"),
                    HumanMessage(content="b", id="h2"),
                    HumanMessage(content="c", id="h3"),
                ]
            },
            config={"configurable": {"thread_id": "t1"}},
        )
        self.assertEqual(main_stub.main_calls, 2)
        self.assertEqual(summary_stub.summary_calls, 1)
        ids = [m.id for m in result["messages"]]
        self.assertTrue(any(i.startswith(SUMMARY_MESSAGE_ID + "-") for i in ids))
        self.assertNotIn("h1", ids)

    async def test_integration_overflow_exhausted_raises(self):
        main_stub = StubChatModel(overflow_after=1)
        summary_stub = StubChatModel(fail_summary=True)
        agent = self._agent(main_stub, summary_stub, _cfg(trigger_tokens=100000))
        with self.assertRaises(FakeOverflow):
            await agent.ainvoke(
                {"messages": [HumanMessage(content="短", id="h1")]},
                config={"configurable": {"thread_id": "t1"}},
            )

    async def test_integration_non_overflow_error_raises(self):
        class Boom(Exception):
            pass

        class BoomModel(StubChatModel):
            async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
                raise Boom()

        agent = self._agent(BoomModel(), StubChatModel(), _cfg())
        with self.assertRaises(Boom):
            await agent.ainvoke(
                {"messages": [HumanMessage(content="hi", id="h1")]},
                config={"configurable": {"thread_id": "t1"}},
            )


class ArchiveTests(unittest.TestCase):
    """压缩存档:写入/读取/中间件压缩时落盘。"""

    def test_append_and_read_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "archive.jsonl"
            append_archive(path, [HumanMessage(content="你好", id="h1"), AIMessage(content="ok", id="ai1")])
            records = read_archive(path)
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["type"], "human")
            self.assertEqual(records[0]["content"], "你好")
            self.assertEqual(records[1]["type"], "ai")
        # 不存在的文件返回空列表
        self.assertEqual(read_archive(Path(tempfile.gettempdir()) / "nope.jsonl"), [])

    async def test_compress_appends_archive(self):
        summary_stub = StubChatModel()
        mw = ContextCompressionMiddleware(_cfg(), summary_model=summary_stub)
        state = {
            "messages": [
                HumanMessage(content="x" * 5000, id="h1"),
                HumanMessage(content="y" * 100, id="h2"),
                AIMessage(content="ok", id="ai1"),
                HumanMessage(content="z" * 100, id="h3"),
            ],
            "artifacts": [],
            "delegations": [],
            "task_contract": None,
        }
        runtime = SimpleNamespace(
            execution_info=SimpleNamespace(thread_id="th-1"),
            context={"user_id": "u-1"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            fake_root = Path(tmp) / "{user_id}" / "threads" / "{thread_id}" / "user-data"
            with patch(
                "caspian.agents.middlewares.context_compression.REAL_ROOT",
                str(fake_root),
            ):
                update = await mw.abefore_model(state, runtime)
                self.assertIsNotNone(update)
            archive_file = Path(tmp) / "u-1" / "threads" / "th-1" / "archive.jsonl"
            records = read_archive(archive_file)
            self.assertEqual(len(records), 1)  # 被压的是 h1
            self.assertEqual(records[0]["id"], "h1")

    async def test_compress_without_runtime_info_skips_archive(self):
        summary_stub = StubChatModel()
        mw = ContextCompressionMiddleware(_cfg(), summary_model=summary_stub)
        state = {
            "messages": [
                HumanMessage(content="x" * 5000, id="h1"),
                HumanMessage(content="y" * 100, id="h2"),
                AIMessage(content="ok", id="ai1"),
                HumanMessage(content="z" * 100, id="h3"),
            ],
            "artifacts": [],
            "delegations": [],
            "task_contract": None,
        }
        update = await mw.abefore_model(state, None)
        self.assertIsNotNone(update)  # 无 runtime 信息时压缩照常,仅跳过归档


if __name__ == "__main__":
    asyncio.run(unittest.main())
