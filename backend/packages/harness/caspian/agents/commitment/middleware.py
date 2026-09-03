"""
本文件对外提供 CommitmentMiddleware，作为 lead agent 执行前的可拆卸承诺层入口。

输入:
    model — 承诺层内部 Worker 和 Evaluator 使用的 BaseChatModel。
    context7_loader — Context7 工具的懒加载器（callable，返回 awaitable[list[BaseTool]]）。
        仅在确认 /commit 触发承诺层时才被调用；未触发承诺层时不被调用，从而普通对话
        不依赖 Context7 是否可达。
    skill_names — 当前用户 enabled 技能名集合，用于剥离消息前导的 /name skill token。
    AgentState / runtime — lead agent 当前消息状态和包含 thread_id 的运行时信息。

输出:
    None — 已存在 task_contract、没有消息或未显式输入 /commit 指令时跳过承诺层。
    dict[str, Any] — 完成后写入 task_contract，并原位替换 /commit 消息。
    GraphInterrupt — 人工确认节点暂停时向父图传播的中断。

具体工作流:
    (1) before_agent 检查当前 thread 是否已经存在任务合同及有效 /commit 指令；
        消息前导的 skill token（如 /docx）先剥离再匹配，剥离的 token 不进入承诺任务文本。
    (2) 确认 /commit 触发后，调用 context7_loader 获取 Context7 工具：可用则通过
        _get_supervisor() 惰性构建并运行九阶段承诺子图；不可用（返回空或抛错）则生成
        一条说明 Context7 不可用的 HumanMessage（沿用触发消息 id）原位替换触发消息，
        不启动承诺子图、不抛异常，run 正常继续。
    (3) 只把指令 HumanMessage（沿用触发消息 id）种子化隔离的九阶段子图；
        /commit 前置历史不进入子图，指令经 source_text 传递，<current_uploads> 标签
        从指令中分离后经 uploads_tag 显式传递。
    (4) 按子图 checkpoint 存在性区分首次执行与 resume：首次从 stage 0 种子化；
        resume 时从父图 config 读取 resume 载荷并 Command(resume=...) 转发，
        子图从上次中断点继续，不重放已完成阶段。
    (5) 将子图 interrupt 原样传播给现有 run/resume 链路。
    (6) 子图完成后校验 stage、合同和最终消息。
    (7) 返回状态更新，由 LangGraph reducer 以相同 id 原位写入合同 HumanMessage。

示例:
    middleware = CommitmentMiddleware(model, context7_loader, skill_names)
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import AgentState
from langchain.messages import HumanMessage
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph._internal._constants import (
    CONFIG_KEY_CHECKPOINTER,
    CONFIG_KEY_SCRATCHPAD,
)
from langgraph.config import get_config
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

from caspian.agents.commitment.delegation import ReviewedDelegator
from caspian.agents.commitment.schemas import CommitmentState
from caspian.agents.commitment.tracing import _write_commitment_messages
from caspian.agents.commitment.workflow import _build_supervisor

logger = logging.getLogger(__name__)

_SUBGRAPH_THREAD_SUFFIX = ":commitment"

_UPLOADS_TAG_RE = re.compile(
    r"<current_uploads>.*?</current_uploads>", re.DOTALL
)


def _strip_leading_skill_tokens(text: str, skill_names: frozenset[str]) -> str:
    """剥离文本前导的 /<skill-name> token 序列，返回剩余文本。

    输入:
        text: str — 去除外围空白后的消息文本
        skill_names: frozenset[str] — 当前用户 enabled 技能名集合

    输出:
        str — 剥离前导 skill token 后的文本；无 token 或集合为空时原样返回

    工作流:
        (1) 从文本首部逐 token 匹配 /<skill-name>（精确、大小写敏感）。
        (2) 连续匹配的 token 全部剥离，遇到第一个不匹配 token 停止并返回剩余文本。
    """
    if not skill_names:
        return text
    rest = text
    while True:
        match = re.match(r"/(\S+)(?:\s+|$)", rest)
        if not match or match.group(1) not in skill_names:
            return rest
        rest = rest[match.end():].lstrip()


def _commit_instruction(
    message: Any,
    skill_names: frozenset[str] = frozenset(),
) -> str | None:
    if not isinstance(message, HumanMessage) or not isinstance(message.content, str):
        return None
    stripped = _strip_leading_skill_tokens(message.content.strip(), skill_names)
    match = re.fullmatch(r"/commit\s+(.+)", stripped, re.DOTALL)
    if not match:
        return None
    return match.group(1).strip() or None


def _extract_uploads_tag(instruction: str) -> tuple[str, str | None]:
    """从指令文本中分离 UploadsMiddleware 注入的 <current_uploads> 标签块。

    输入:
        instruction: str — 去掉 /commit 前缀后的指令文本。

    输出:
        tuple[str, str | None] — (剥离标签后的指令, 标签块原文或 None)。

    工作流:
        (1) 用非贪婪正则查找 <current_uploads>...</current_uploads> 块。
        (2) 找到则从指令中移除并返回标签原文；未找到返回指令原样。

    示例:
        clean, tag = _extract_uploads_tag("做X\\n\\n<current_uploads>...</current_uploads>")
    """
    match = _UPLOADS_TAG_RE.search(instruction)
    if not match:
        return instruction, None
    tag = match.group(0).strip()
    cleaned = (instruction[: match.start()] + instruction[match.end() :]).strip()
    return cleaned, tag


def _parent_config() -> dict[str, Any]:
    """读取当前节点执行的 configurable；不在节点上下文时返回空 dict。"""
    try:
        return dict(get_config().get("configurable", {}))
    except RuntimeError:
        return {}


def _parent_checkpointer() -> Any | None:
    """获取父图运行期挂载的 checkpointer 实例（经 config 注入）。

    实现依据: langgraph pregel 在节点 configurable 中注入
    CONFIG_KEY_CHECKPOINTER（_algo.py: checkpointer or configurable.get(...)）。
    """
    return _parent_config().get(CONFIG_KEY_CHECKPOINTER)


def _parent_resume_value() -> tuple[Any | None, bool]:
    """不消费地探测父图是否处于 resume 重执行，并返回 resume 载荷。

    输出:
        tuple[Any | None, bool] — (resume 值或 None, 是否处于 resume 重执行)。

    工作流:
        (1) 读取 configurable 中的 CONFIG_KEY_SCRATCHPAD。
        (2) get_null_resume(consume=False) 只探测不消费。
        (3) 不在节点上下文或无法读取时返回 (None, False)。
    """
    conf = _parent_config()
    scratchpad = conf.get(CONFIG_KEY_SCRATCHPAD)
    if scratchpad is None:
        return None, False
    try:
        value = scratchpad.get_null_resume(False)
    except Exception:
        return None, False
    return value, value is not None


def _subgraph_config(thread_id: str) -> dict[str, Any]:
    """构造承诺子图的隔离 checkpoint 配置。

    使用 thread_id 派生的隔离命名空间（{thread_id}:commitment），与父图 checkpoint
    键互不冲突；父图 checkpointer 经 CONFIG_KEY_CHECKPOINTER 注入，使子图在自身
    未挂 checkpointer 的情况下仍可持久化与恢复。
    """
    configurable: dict[str, Any] = {
        "thread_id": f"{thread_id}{_SUBGRAPH_THREAD_SUFFIX}",
    }
    if checkpointer := _parent_checkpointer():
        configurable[CONFIG_KEY_CHECKPOINTER] = checkpointer
    return {"configurable": configurable}


def _load_decision_table_dict(thread_id: str, user_id: str | None = None) -> dict[str, Any]:
    """读取当前 thread 的决策等级表并转为 JSON 兼容 dict（受保护 helper）。

    输入:
        thread_id: str — 线程标识

    输出:
        dict — {"version", "updated", "rows": [{requirement, decision, priority}, ...]}；
               无等级表时返回空 dict
    """
    from caspian.agents.commitment.decision_table import read_decision_table

    table = read_decision_table(str(thread_id), user_id=user_id)
    if table is None:
        return {}
    return {
        "version": table.version,
        "updated": table.updated,
        "rows": [
            {
                "requirement": row.requirement,
                "decision": row.decision,
                "priority": row.priority,
            }
            for row in table.rows
        ],
    }


def _seed_subgraph_input(
    trigger: HumanMessage,
    instruction: str,
    uploads_tag: str | None,
    thread_id: str,
    decision_table: dict[str, Any] | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """构造子图首次执行的种子输入：只含指令消息，不携带 /commit 前置历史。"""
    return {
        "messages": [
            trigger.model_copy(update={"content": instruction}),
        ],
        "stage": 0,
        "awaiting_human": None,
        "artifacts": {},
        "thread_id": str(thread_id),
        "user_id": user_id,
        "knowledge_files": [],
        "source_text": instruction,
        "uploads_tag": uploads_tag or "",
        "decision_table": decision_table or {},
    }


class CommitmentMiddleware(AgentMiddleware):
    state_schema = CommitmentState
    _skill_names: frozenset[str] = frozenset()
    # Context7 不可用时的用户可见说明
    _CONTEXT7_UNAVAILABLE_MSG = (
        "承诺层需要 Context7 服务以获取官方技术与版本资料，但 Context7 当前不可用，"
        "本次跳过承诺流程。请检查网络/代理后重试，或改用普通对话。"
    )

    def __init__(
        self,
        model: BaseChatModel,
        context7_loader: Callable[[], Awaitable[list[BaseTool]]] | None,
        skill_names: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__()
        self._model = model
        self._context7_loader = context7_loader
        self._skill_names = skill_names
        self._supervisor = None

    async def _context7_tools(self) -> list[BaseTool]:
        """调用 context7_loader 获取 Context7 工具，失败时返回空列表（受保护 helper）。

        输入: 无（读取 self._context7_loader）

        输出:
            list[BaseTool] — 成功时返回工具列表；loader 为 None、返回空或抛异常时返回 []，
            以此触发承诺层的优雅降级（返回说明消息）而非中断 run。
        """
        if self._context7_loader is None:
            return []
        try:
            tools = await self._context7_loader()
        except Exception:
            logger.warning("Context7 工具加载失败，承诺层降级处理", exc_info=True)
            return []
        return tools or []

    async def _ensure_supervisor(self, context7_tools: list[BaseTool]) -> Any:
        """确保隔离 Supervisor 承诺子图可用并返回它（受保护 helper）。

        输入:
            context7_tools: list[BaseTool] — 已解析的 Context7 工具列表，用于首次构建承诺子图

        输出:
            CompiledStateGraph — supervisor 实例；首次调用时构建并缓存，后续复用。

        具体工作流:
            (1) self._supervisor 已存在（如测试注入）时直接返回
            (2) 否则用 context7_tools 构建 ReviewedDelegator 并缓存 supervisor
        """
        if self._supervisor is None:
            delegator = ReviewedDelegator(self._model, context7_tools or [])
            self._supervisor = _build_supervisor(delegator)
        return self._supervisor

    async def _run(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        if state.get("task_contract"):
            return None
        messages = state.get("messages", [])
        if not messages:
            return None
        trigger = messages[-1]
        instruction = _commit_instruction(trigger, self._skill_names)
        if instruction is None:
            return None
        if trigger.id is None:
            raise ValueError("/commit 触发消息缺少 message id")
        thread_id = getattr(getattr(runtime, "execution_info", None), "thread_id", None)
        if thread_id is None:
            raise ValueError("CommitmentMiddleware 无法获取 thread_id")

        user_id = None
        ctx = getattr(runtime, "context", None)
        if isinstance(ctx, dict):
            raw_user_id = ctx.get("user_id")
            if raw_user_id:
                user_id = str(raw_user_id)

        # 确认为 /commit 触发后，才尝试加载 Context7 工具；不可用时优雅降级为说明消息。
        # 若 supervisor 已存在（如测试注入），视为工具已就绪，不重新要求 context7 loader。
        context7_tools = [] if self._supervisor is not None else await self._context7_tools()
        if self._supervisor is None and not context7_tools:
            return {
                "messages": [
                    HumanMessage(
                        content=self._CONTEXT7_UNAVAILABLE_MSG,
                        id=trigger.id,
                    ),
                ],
            }

        supervisor = await self._ensure_supervisor(context7_tools)

        instruction, uploads_tag = _extract_uploads_tag(instruction)
        subgraph_config = _subgraph_config(str(thread_id))

        # 区分首次执行与 resume：父图 resume 重执行时 config 携带 resume 载荷，
        # 子图 checkpoint 必须与之对应存在；任一方向不一致都显式报错，不静默重放。
        checkpointer = _parent_checkpointer()
        resume_value, is_resume = _parent_resume_value()
        subgraph_input: dict[str, Any] | Command
        if is_resume:
            latest = None
            if checkpointer is not None:
                try:
                    latest = await checkpointer.aget_tuple(subgraph_config)
                except Exception:
                    latest = None
            if checkpointer is None or latest is None:
                raise RuntimeError(
                    "父图正在恢复承诺流程，但承诺子图 checkpoint 不可用；"
                    "无法从上次中断点继续"
                )
            subgraph_input = Command(resume=resume_value)
        else:
            if checkpointer is not None:
                try:
                    latest = await checkpointer.aget_tuple(subgraph_config)
                except Exception:
                    latest = None
                if latest is not None:
                    raise RuntimeError(
                        "承诺子图存在 checkpoint 但父图未处于 resume 状态；"
                        "拒绝静默重新从 stage 0 执行并丢弃人工决定"
                    )
            subgraph_input = _seed_subgraph_input(
                trigger,
                instruction,
                uploads_tag,
                str(thread_id),
                _load_decision_table_dict(str(thread_id), user_id),
                user_id,
            )

        supervisor = await self._ensure_supervisor(context7_tools)
        result: dict[str, Any] = {}
        async for mode, chunk in supervisor.astream(
            subgraph_input,
            config=subgraph_config,
            stream_mode=["values", "custom"],
        ):
            if mode == "custom":
                _write_commitment_messages(chunk)
            elif mode == "values":
                result = chunk
                _write_commitment_messages(
                    {
                        "type": "commitment_messages",
                        "actor": "supervisor",
                        "stage": int(chunk.get("stage", 0)),
                        "messages": list(chunk.get("messages", [])),
                    }
                )
        if interrupts := result.get("__interrupt__"):
            raise GraphInterrupt(interrupts)
        if result.get("stage") != 9:
            raise RuntimeError(
                f"承诺流程异常终止于 stage {result.get('stage', 0)}"
            )
        contract = str(result.get("task_contract", ""))
        final_message = str(result.get("final_message", ""))
        if not contract or not final_message:
            raise RuntimeError("承诺流程未产出合同或最终消息")
        return {
            "task_contract": contract,
            "messages": [
                HumanMessage(content=final_message, id=trigger.id),
            ],
        }

    def before_agent(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
        return asyncio.run(self._run(state, runtime))

    async def abefore_agent(
        self, state: AgentState, runtime: Any
    ) -> dict[str, Any] | None:
        return await self._run(state, runtime)
