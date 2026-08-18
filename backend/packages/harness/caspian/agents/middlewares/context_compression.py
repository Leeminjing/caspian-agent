"""
本文件对外提供 ContextCompressionMiddleware,作为 lead agent 主图的上下文压缩中间件。

对外提供:
    ContextCompressionMiddleware — 单类双 hook:
        before_model/abefore_model — 预防压缩(超阈值 → 切点 → LLM 摘要 → RemoveMessage 替换旧区)
        wrap_model_call/awrap_model_call — 溢出恢复(L0 剪大 tool result → 重试 → L1 摘要 → 重试 → 抛错)
    _is_overflow — 溢出异常识别(受保护 helper)

输入:
    config: ContextCompressionConfig — 压缩配置(默认 ContextCompressionConfig(),enabled=False)
    summary_model: BaseChatModel | None — 摘要模型,None 时从 runtime.context 的 app_config 懒创建

输出:
    before_model 返回 {"messages": [RemoveMessage(REMOVE_ALL_MESSAGES), 摘要消息, ...保留消息]} | None
    awrap_model_call 返回 ModelResponse 或 ExtendedModelResponse(model_response, command=Command(update))

具体工作流:
    (1) 预防:abefore_model 计数超 trigger_tokens → 复用 _compress(切点/摘要/后置校验),
        失败任何环节返回 None(fail-soft)
    (2) 恢复:awrap_model_call 捕获溢出异常 → L0 prune_large_tool_messages 剪枝重试,
        仍溢出 → L1 与预防路径复用同一 _compress,重试成功经 ExtendedModelResponse 落盘
    (3) 非溢出异常原样上抛;阶梯耗尽抛原始异常

示例:
    from caspian.agents.middlewares.context_compression import ContextCompressionMiddleware
    middleware = ContextCompressionMiddleware(cfg, summary_model=model)
"""

import asyncio
import json
import logging
from pathlib import Path

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ExtendedModelResponse,
    ModelRequest,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import RemoveMessage
from langgraph.config import get_stream_writer
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command

from caspian.agents.middlewares.context_compression_plan import (
    SUMMARY_PROMPT_TEMPLATE,
    build_summary_message,
    make_token_counter,
    plan_compression,
    prune_large_tool_messages,
    render_side_channels,
    verify_shrink,
)
from caspian.config.context_compression_config import ContextCompressionConfig
from caspian.sandbox.path_utils import REAL_ROOT

logger = logging.getLogger(__name__)

ARCHIVE_FILENAME = "archive.jsonl"


def archive_path_for(user_id: str, thread_id: str) -> Path | None:
    """构造当前线程的压缩存档文件路径(与沙箱目录同级的运行时产物)。

    输入:
        user_id / thread_id — 用户与线程标识

    输出:
        Path | None — .caspian/users/{uid}/threads/{tid}/archive.jsonl;
        任一标识缺失返回 None
    """
    if not user_id or not thread_id:
        return None
    root = Path(REAL_ROOT.format(user_id=str(user_id), thread_id=str(thread_id)))
    return root.parent / ARCHIVE_FILENAME


def append_archive(path: Path, messages: list) -> None:
    """把被压缩消息逐条序列化追加到存档文件(JSONL)。

    输入:
        path: Path — 存档文件路径
        messages: list[BaseMessage] — 被压缩替换的原始消息

    输出:
        None — 写失败仅记日志(best-effort,不阻断压缩)
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for message in messages:
                record = (
                    message.model_dump()
                    if hasattr(message, "model_dump")
                    else {"content": str(message)}
                )
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        logger.error("上下文压缩: 存档写入失败 path=%s", path, exc_info=True)


def read_archive(path: Path) -> list[dict]:
    """读取压缩存档,返回消息 dict 列表(文件不存在或损坏返回空列表)。"""
    try:
        if not path.exists():
            return []
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        return records
    except Exception:
        logger.warning("上下文压缩: 存档读取失败 path=%s", path, exc_info=True)
        return []


def _runtime_archive_path(runtime) -> Path | None:
    """从 runtime 解析当前 (user_id, thread_id) 的存档路径(受保护 helper)。"""
    try:
        thread_id = getattr(getattr(runtime, "execution_info", None), "thread_id", None)
        context = getattr(runtime, "context", None)
        user_id = context.get("user_id") if isinstance(context, dict) else None
        return archive_path_for(user_id, thread_id)
    except Exception:
        return None


def emit_compaction_event(status: str) -> None:
    """向当前 stream writer 推压缩状态事件(无 writer 时静默跳过)。

    输入:
        status: str — started / done / failed / skipped

    输出:
        None — 事件经 custom 流上桥,前端据此显示"上下文正在压缩中"
    """
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    writer({"type": "compaction_status", "status": status})

# OpenAI 兼容 400/413 且消息含上下文长度关键词(DeepSeek 报错形态与 OpenAI SDK 同形状)
_OVERFLOW_STATUSES = frozenset({400, 413})
_OVERFLOW_MARKERS = (
    "context length",
    "context_length",
    "maximum context",
    "max context",
    "context window",
    "token limit",
    "too many tokens",
    "input is too long",
    "context_exceeded",
    "exceeded the maximum",
    "最大上下文",
    "上下文长度",
)


def _is_overflow(exc: BaseException) -> bool:
    """识别上下文溢出异常(受保护 helper)。

    输入:
        exc: BaseException — 模型调用抛出的异常

    输出:
        bool — True 表示命中溢出特征

    工作流:
        (1) status_code/status 存在且不在 {400, 413} → False
        (2) code 含 "context" → True
        (3) 消息文本含任一关键词 → True
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status is not None and status not in _OVERFLOW_STATUSES:
        return False
    code = getattr(exc, "code", None)
    if isinstance(code, str) and "context" in code.lower():
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _OVERFLOW_MARKERS)


def _message_text(response) -> str:
    """从模型响应提取纯文本(content 为 str 或 block 列表)。"""
    content = getattr(response, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(
            item if isinstance(item, str) else str(item.get("text", ""))
            for item in content
        ).strip()
    return str(content).strip()


class ContextCompressionMiddleware(AgentMiddleware):
    """上下文压缩中间件:预防压缩(before_model)+ 溢出恢复(wrap_model_call)。"""

    def __init__(
        self,
        config: ContextCompressionConfig | None = None,
        *,
        summary_model: BaseChatModel | None = None,
    ) -> None:
        super().__init__()
        self._cfg = config or ContextCompressionConfig()
        self._token_counter = make_token_counter()
        self._summary_model = summary_model

    # ------------------------------------------------------------------
    # 摘要与压缩核心(预防与恢复共用)
    # ------------------------------------------------------------------

    def _resolve_summary_model(self, runtime) -> BaseChatModel:
        """返回摘要模型:注入优先,runtime.context 的 app_config 懒创建兜底。"""
        if self._summary_model is not None:
            return self._summary_model
        from caspian.models import create_chat_model

        context = getattr(runtime, "context", None)
        app_config = context.get("app_config") if isinstance(context, dict) else None
        name = self._cfg.summary_model
        if name:
            self._summary_model = create_chat_model(name=name, app_config=app_config)
        elif app_config is not None and app_config.models:
            self._summary_model = create_chat_model(
                name=app_config.models[0].name, app_config=app_config
            )
        else:
            self._summary_model = create_chat_model()
        return self._summary_model

    async def _acreate_summary(self, to_summarize: list, state: dict, runtime) -> str | None:
        """调用摘要模型生成摘要文本,任何失败返回 None(fail-soft)。"""
        from langchain_core.messages.utils import (
            get_buffer_string,
            trim_messages,
        )

        try:
            trimmed = trim_messages(
                to_summarize,
                max_tokens=self._cfg.max_tokens_to_summarize,
                token_counter=self._token_counter,
                strategy="last",
                start_on="human",
                include_system=True,
                allow_partial=True,
            )
            history = get_buffer_string(trimmed, format="xml")
            prompt = SUMMARY_PROMPT_TEMPLATE.format(
                side_channels=render_side_channels(state),
                history=history,
            )
            model = self._resolve_summary_model(runtime)
            response = await asyncio.wait_for(
                model.ainvoke(prompt),
                timeout=self._cfg.summary_timeout_seconds,
            )
            text = _message_text(response)
            return text or None
        except Exception:
            logger.error("上下文压缩: 摘要生成失败, 本轮跳过", exc_info=True)
            return None

    async def _compress(self, messages: list, state: dict, runtime) -> dict | None:
        """压缩核心:切点 → 摘要 → 后置校验,返回状态增量 dict 或 None。

        压缩期间经 stream writer 推 compaction_status 事件(started/done/failed),
        替换前把被压消息追加存档,保证前端折叠条可还原完整历史。
        """
        plan = plan_compression(messages, keep_messages=self._cfg.keep_messages)
        if plan is None:
            return None
        to_summarize, preserved = plan
        emit_compaction_event("started")
        summary_text = await self._acreate_summary(to_summarize, state, runtime)
        if summary_text is None:
            emit_compaction_event("failed")
            return None
        summary_message = build_summary_message(summary_text)
        if not verify_shrink(summary_message, to_summarize, self._token_counter):
            logger.warning("上下文压缩: 摘要未变小(后置校验失败), 本轮跳过")
            emit_compaction_event("skipped")
            return None
        archive_file = _runtime_archive_path(runtime)
        if archive_file is not None:
            append_archive(archive_file, to_summarize)
        logger.info(
            "上下文压缩: 已压缩 %s 条消息为摘要, 保留 %s 条",
            len(to_summarize),
            len(preserved),
        )
        emit_compaction_event("done")
        return {
            "messages": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                summary_message,
                *preserved,
            ]
        }

    # ------------------------------------------------------------------
    # 预防路径:before_model
    # ------------------------------------------------------------------

    async def abefore_model(self, state, runtime) -> dict | None:
        """每轮模型调用前检查 tokens,超阈值则压缩旧历史。

        压缩后 tokens 低于阈值,再次执行直接返回 None(幂等)。
        """
        if not self._cfg.enabled:
            return None
        try:
            messages = list(state.get("messages", []))
            if self._token_counter(messages) < self._cfg.trigger_tokens:
                return None
            return await self._compress(messages, dict(state), runtime)
        except Exception:
            logger.error("上下文压缩: 预防压缩异常, 本轮跳过", exc_info=True)
            emit_compaction_event("failed")
            return None

    def before_model(self, state, runtime) -> dict | None:
        return asyncio.run(self.abefore_model(state, runtime))

    # ------------------------------------------------------------------
    # 恢复路径:wrap_model_call
    # ------------------------------------------------------------------

    async def awrap_model_call(self, request: ModelRequest, handler):
        """包装模型调用,溢出时执行 L0 剪枝 → L1 摘要 恢复阶梯。"""
        if not self._cfg.enabled:
            return await handler(request)

        try:
            return await handler(request)
        except Exception as exc:
            if not _is_overflow(exc):
                raise
            logger.info("上下文压缩: 检测到上下文溢出, 进入恢复阶梯")

            # L0: 免费剪枝最大的历史 ToolMessage(逐条,最多 recovery_max_attempts 条)
            for _ in range(max(1, self._cfg.recovery_max_attempts)):
                pruned = prune_large_tool_messages(
                    list(request.messages),
                    prune_max_chars=self._cfg.prune_max_chars,
                    token_counter=self._token_counter,
                )
                if pruned is None:
                    break
                new_messages, replacement = pruned
                try:
                    response = await handler(request.override(messages=new_messages))
                    return ExtendedModelResponse(
                        model_response=response,
                        command=Command(update={"messages": [replacement]}),
                    )
                except Exception as exc2:
                    if not _is_overflow(exc2):
                        raise
                    request = request.override(messages=new_messages)

            # L1: 摘要(与预防路径复用同一压缩函数)
            try:
                update = await self._compress(
                    list(request.messages), dict(request.state), request.runtime
                )
                if update is not None:
                    rebuilt = [
                        message
                        for message in update["messages"]
                        if not isinstance(message, RemoveMessage)
                    ]
                    response = await handler(request.override(messages=rebuilt))
                    return ExtendedModelResponse(
                        model_response=response,
                        command=Command(update=update),
                    )
            except Exception:
                logger.error("上下文压缩: 恢复摘要失败, 上抛原始异常", exc_info=True)

            raise exc

    def wrap_model_call(self, request: ModelRequest, handler):
        return asyncio.run(self.awrap_model_call(request, handler))
