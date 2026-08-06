"""
本文件对外提供 SubagentExecutor 执行引擎与 SubagentResult 结果档案。

对外提供:
    SubagentExecutor — subagent 执行引擎（独立装配 + 后台/隔离事件循环执行）
    SubagentResult — 单次执行的锁保护结果档案（终态单写）
    get_background_task_result / request_cancel_background_task / cleanup_background_task —
        后台任务注册表操作

输入:
    config: SubagentConfig — subagent 类型配置
    tools: list[BaseTool] — 父工具集（将按配置过滤）
    task: str — 委托任务描述

输出:
    SubagentResult — 终态结果（completed / failed / cancelled / timed_out）

具体工作流:
    (1) execute_async 在全局 ThreadPoolExecutor 调度，任务运行在进程级持久化隔离事件循环
    (2) _aexecute 独立装配 create_agent（模型/过滤后工具/中间件链/系统提示词），astream 执行
    (3) 迭代边界检查 cancel_event 协作取消；recursion_limit=max_turns 触发部分结果恢复
    (4) 终态经 try_set_terminal 单写，后台超时/取消与执行 worker 竞态先到先得
    (5) task 工具侧按 task_id 轮询 SubagentResult

示例:
    result = executor.execute_async("调研竞品定价", task_id="tc-001")
    status = get_background_task_result("tc-001").status
"""

import asyncio
import atexit
import logging
import threading
import uuid
from collections.abc import Callable, Coroutine
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextvars import Context, copy_context
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from langchain.agents import create_agent
from langchain.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.errors import GraphRecursionError

from caspian.agents.lead_agent_state import LeadAgentState
from caspian.config import get_app_config
from caspian.models import create_chat_model
from caspian.subagents.config import SubagentConfig, resolve_subagent_model_name

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_background_tasks: dict[str, "SubagentResult"] = {}
_background_tasks_lock = threading.Lock()

_scheduler_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="subagent-scheduler-")

_isolated_subagent_loop: asyncio.AbstractEventLoop | None = None
_isolated_subagent_loop_thread: threading.Thread | None = None
_isolated_subagent_loop_started: threading.Event | None = None
_isolated_subagent_loop_lock = threading.Lock()


class SubagentStatus(Enum):
    """subagent 执行状态。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

    @property
    def is_terminal(self) -> bool:
        return self in {
            type(self).COMPLETED,
            type(self).FAILED,
            type(self).CANCELLED,
            type(self).TIMED_OUT,
        }


@dataclass
class SubagentResult:
    """单次 subagent 执行的结果档案。"""

    task_id: str
    trace_id: str
    status: SubagentStatus
    result: str | None = None
    error: str | None = None
    stop_reason: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    ai_messages: list[dict[str, Any]] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _state_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def try_set_terminal(
        self,
        status: SubagentStatus,
        *,
        result: str | None = None,
        error: str | None = None,
        stop_reason: str | None = None,
        ai_messages: list[dict[str, Any]] | None = None,
        completed_at: datetime | None = None,
    ) -> bool:
        """以终态写入结果，仅首次写入生效（后台超时/取消与执行 worker 竞态保护）。

        输入:
            status: SubagentStatus — 终态状态
            result/error/stop_reason/ai_messages/completed_at — 载荷字段

        输出:
            bool — True 表示本次写入生效，False 表示已终态被拒绝

        工作流:
            (1) 非终态参数抛 ValueError
            (2) 加锁检查当前状态，已终态返回 False
            (3) 首次写入状态与载荷，返回 True
        """
        if not status.is_terminal:
            raise ValueError(f"Status {status} is not terminal")

        with self._state_lock:
            if self.status.is_terminal:
                return False
            if result is not None:
                self.result = result
            if error is not None:
                self.error = error
            if stop_reason is not None:
                self.stop_reason = stop_reason
            if ai_messages is not None:
                self.ai_messages = ai_messages
            self.completed_at = completed_at or datetime.now()
            self.status = status
            return True


def message_content_to_text(content: Any) -> str:
    """将消息 content（str 或 block 列表）归一化为纯文本。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content) if content is not None else ""
    parts = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            parts.append(str(item.get("text", "")))
    return "".join(parts)


def _extract_final_result(final_state: Any, *, name: str) -> str:
    """从流式终态提取最终结果文本。

    输入:
        final_state: dict | None — astream 最后一帧（values 模式）
        name: str — subagent 名称（仅日志）

    输出:
        str — 结果文本；无可用内容时返回哨兵 "No response generated"
    """
    if final_state is None:
        return "No response generated"
    messages = final_state.get("messages", [])
    last_ai = None
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            last_ai = message
            break
    if last_ai is not None:
        text = message_content_to_text(last_ai.content).strip()
        return text if text else "No response generated"
    if messages:
        text = message_content_to_text(getattr(messages[-1], "content", "")).strip()
        return text if text else "No response generated"
    return "No response generated"


def _run_isolated_subagent_loop(loop: asyncio.AbstractEventLoop, started_event: threading.Event) -> None:
    """在专用 daemon 线程中运行持久化隔离事件循环。"""
    asyncio.set_event_loop(loop)
    loop.call_soon(started_event.set)
    try:
        loop.run_forever()
    finally:
        started_event.clear()


def _shutdown_isolated_subagent_loop() -> None:
    """停止并关闭持久化隔离事件循环。"""
    global _isolated_subagent_loop
    with _isolated_subagent_loop_lock:
        loop = _isolated_subagent_loop
        _isolated_subagent_loop = None
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)


def _ensure_isolated_loop() -> asyncio.AbstractEventLoop:
    """获取（或创建）进程级持久化隔离事件循环。"""
    global _isolated_subagent_loop, _isolated_subagent_loop_thread, _isolated_subagent_loop_started
    with _isolated_subagent_loop_lock:
        if _isolated_subagent_loop is not None and not _isolated_subagent_loop.is_closed():
            return _isolated_subagent_loop
        loop = asyncio.new_event_loop()
        started = threading.Event()
        thread = threading.Thread(
            target=_run_isolated_subagent_loop,
            args=(loop, started),
            name="caspian-subagent-loop",
            daemon=True,
        )
        thread.start()
        started.wait(timeout=5)
        _isolated_subagent_loop = loop
        _isolated_subagent_loop_thread = thread
        _isolated_subagent_loop_started = started
        return loop


def _submit_to_isolated_loop_in_context(
    parent_context: Context,
    coro_fn: Callable[[], Coroutine[Any, Any, Any]],
) -> Future:
    """在父 contextvar 上下文中向隔离 loop 提交协程，返回 Future。"""
    loop = _ensure_isolated_loop()
    return asyncio.run_coroutine_threadsafe(
        parent_context.run(coro_fn),
        loop,
    )


def _copy_isolated_subagent_context() -> Context:
    """复制调用线程的 contextvar 上下文。"""
    return copy_context()


class SubagentExecutor:
    """subagent 执行引擎：独立装配 + 后台/隔离事件循环执行。"""

    def __init__(
        self,
        config: SubagentConfig,
        tools: list[BaseTool],
        *,
        parent_model: str | None = None,
        user_id: str | None = None,
        thread_id: str | None = None,
        tool_groups: list[str] | None = None,
        trace_id: str | None = None,
    ) -> None:
        self.config = config
        self.parent_model = parent_model
        self.user_id = user_id
        self.thread_id = thread_id
        self.tool_groups = tool_groups
        self.trace_id = trace_id or str(uuid.uuid4())[:8]
        self.model_name: str | None = resolve_subagent_model_name(config, parent_model)
        self._base_tools = _filter_tools(tools, config.tools, config.disallowed_tools)
        self._available_skill_names: set[str] = set()
        logger.info(
            "[trace=%s] SubagentExecutor 初始化: %s 工具 %d 个",
            self.trace_id,
            config.name,
            len(self._base_tools),
        )

    async def _load_skills(self) -> list[Any]:
        """按 config.skills 白名单加载 enabled 技能元数据。

        输入: 无

        输出:
            list[Skill] — 过滤后的技能列表

        工作流:
            (1) config.skills=[] → 禁用技能，直接返回空
            (2) build_enabled_skill_catalog(user_id) 加载全部 enabled 技能
            (3) config.skills 白名单非 None 时按名称过滤
        """
        if self.config.skills is not None and len(self.config.skills) == 0:
            return []
        # 函数级导入避免 tools/builtins → subagents → lead.agent 初始化期循环
        from caspian.agents.lead.agent import build_enabled_skill_catalog

        catalog = build_enabled_skill_catalog(user_id=self.user_id)
        if self.config.skills is not None:
            allowed = set(self.config.skills)
            return [s for s in catalog.skills if s.name in allowed]
        return list(catalog.skills)

    async def _build_initial_state(self, task: str) -> dict[str, Any]:
        """构造执行初始状态：系统提示词 + 技能索引 + 任务消息。

        输入:
            task: str — 委托任务描述

        输出:
            dict[str, Any] — 初始状态（messages + 其他 LeadAgentState 字段）

        工作流:
            (1) 加载技能并记录可用技能名
            (2) 组装系统提示词（config.system_prompt + 技能索引段）
            (3) 返回 messages=[SystemMessage, HumanMessage(task)]
        """
        from caspian.agents.lead.prompt import apply_prompt_template

        skills = await self._load_skills()
        self._available_skill_names = {skill.name for skill in skills}

        system_parts: list[str] = []
        if self.config.system_prompt:
            system_parts.append(self.config.system_prompt)
        if skills:
            skill_names = ", ".join(sorted(self._available_skill_names))
            system_parts.append(
                apply_prompt_template(
                    agent_name=self.config.name,
                    skill_names=skill_names,
                    container_base_path="/mnt/skills",
                ).split("<uploads>")[0]  # 截断上传段，subagent 上下文不注入 uploads
            )
        system_prompt = "\n\n".join(system_parts) or f"You are {self.config.name}."
        return {
            "messages": [
                SystemMessage(content=system_prompt),
                HumanMessage(content=task),
            ],
        }

    def _create_agent(self, tools: list[BaseTool]):
        """创建 subagent 的 CompiledStateGraph 实例。"""
        app_config = get_app_config("config.yaml")
        model: BaseChatModel = create_chat_model(name=self.model_name, app_config=app_config)

        from caspian.agents.middlewares.builder import build_subagent_middlewares

        middlewares = build_subagent_middlewares(
            model=model,
            skill_names=frozenset(self._available_skill_names),
        )
        return create_agent(
            model=model,
            tools=tools,
            middleware=middlewares,
            system_prompt=None,  # 系统提示词已放入初始消息，避免多 SystemMessage
            state_schema=LeadAgentState,
            checkpointer=False,
        )

    async def _aexecute(self, task: str, result_holder: SubagentResult | None = None) -> SubagentResult:
        """异步执行 subagent 任务。

        输入:
            task: str — 委托任务描述
            result_holder: SubagentResult | None — 已创建的结果档案（后台路径复用）

        输出:
            SubagentResult — 终态结果
        """
        result = result_holder or SubagentResult(
            task_id=str(uuid.uuid4())[:8],
            trace_id=self.trace_id,
            status=SubagentStatus.RUNNING,
            started_at=datetime.now(),
        )
        ai_messages = result.ai_messages
        seen_message_ids: set[str] = {
            message_id
            for message in ai_messages
            if (message_id := message.get("id"))
        }

        try:
            state = await self._build_initial_state(task)
            agent = self._create_agent(self._base_tools)

            run_config: dict[str, Any] = {
                "recursion_limit": self.config.max_turns,
                "tags": [f"subagent:{self.config.name}"],
            }
            context: dict[str, Any] = {}
            if self.thread_id:
                context["thread_id"] = self.thread_id
            if self.user_id:
                context["user_id"] = self.user_id
            if self.tool_groups:
                context["tool_groups"] = self.tool_groups
            context["model_name"] = self.model_name
            context["is_subagent"] = True

            logger.info(
                "[trace=%s] Subagent %s 开始执行 max_turns=%s",
                self.trace_id,
                self.config.name,
                self.config.max_turns,
            )

            if result.cancel_event.is_set():
                result.try_set_terminal(SubagentStatus.CANCELLED, error="Cancelled by user")
                return result

            final_state = None
            async for chunk in agent.astream(
                state,
                config=run_config,
                context=context,
                stream_mode="values",
            ):
                if result.cancel_event.is_set():
                    logger.info("[trace=%s] Subagent %s 被父取消", self.trace_id, self.config.name)
                    result.try_set_terminal(SubagentStatus.CANCELLED, error="Cancelled by user")
                    return result

                final_state = chunk
                for message in chunk.get("messages", []):
                    if not isinstance(message, (AIMessage, ToolMessage)):
                        continue
                    message_dict = message.model_dump()
                    message_id = message_dict.get("id")
                    if message_id:
                        if message_id in seen_message_ids:
                            continue
                        seen_message_ids.add(message_id)
                    elif message_dict in ai_messages:
                        continue
                    ai_messages.append(message_dict)

            logger.info("[trace=%s] Subagent %s 执行完成", self.trace_id, self.config.name)
            final_result = _extract_final_result(final_state, name=self.config.name)
            result.try_set_terminal(
                SubagentStatus.COMPLETED,
                result=final_result,
                ai_messages=ai_messages,
            )

        except GraphRecursionError:
            # recursion_limit == max_turns：恢复部分结果（有可用文本 → completed+turn_capped）
            max_turns = self.config.max_turns
            logger.warning(
                "[trace=%s] Subagent %s 达到 max_turns=%s，恢复部分结果",
                self.trace_id,
                self.config.name,
                max_turns,
            )
            messages = (final_state or {}).get("messages", [])
            usable_partial: str | None = None
            for message in reversed(messages):
                if isinstance(message, AIMessage):
                    text = message_content_to_text(message.content).strip()
                    if text:
                        usable_partial = text
                    break
            if usable_partial is not None:
                result.try_set_terminal(
                    SubagentStatus.COMPLETED,
                    result=usable_partial,
                    stop_reason="turn_capped",
                    ai_messages=ai_messages,
                )
            else:
                result.try_set_terminal(
                    SubagentStatus.FAILED,
                    error=f"Reached max_turns={max_turns}",
                    stop_reason="turn_capped",
                    ai_messages=ai_messages,
                )

        except Exception as exc:
            logger.exception("[trace=%s] Subagent %s 执行异常", self.trace_id, self.config.name)
            result.try_set_terminal(
                SubagentStatus.FAILED,
                error=str(exc),
                ai_messages=ai_messages,
            )

        return result

    def _execute_in_isolated_loop(self, task: str, result_holder: SubagentResult | None = None) -> SubagentResult:
        """在持久化隔离事件循环上同步等待执行（父上下文已在事件循环内时使用）。"""
        future: Future | None = None
        parent_context = _copy_isolated_subagent_context()
        try:
            future = _submit_to_isolated_loop_in_context(
                parent_context,
                lambda: self._aexecute(task, result_holder),
            )
            return future.result(timeout=self.config.timeout_seconds)
        except FuturesTimeoutError:
            if result_holder is not None:
                result_holder.cancel_event.set()
            if future is not None:
                future.cancel()
            raise
        except Exception:
            if future is not None:
                logger.debug("Subagent %s 隔离 loop 执行失败", self.config.name, exc_info=True)
            raise

    def execute(self, task: str, result_holder: SubagentResult | None = None) -> SubagentResult:
        """同步执行任务。

        输入:
            task: str — 任务描述
            result_holder: SubagentResult | None — 可选预创建结果档案

        输出:
            SubagentResult — 终态结果

        工作流:
            (1) 检测当前是否已在运行事件循环
            (2) 已在循环内 → 隔离 loop 同步等待（避免嵌套 asyncio.run）
            (3) 未在循环内 → asyncio.run 直接执行
        """
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop is not None and loop.is_running():
                return self._execute_in_isolated_loop(task, result_holder)
            return asyncio.run(self._aexecute(task, result_holder))
        except Exception as exc:
            logger.exception("[trace=%s] Subagent %s 执行失败", self.trace_id, self.config.name)
            if result_holder is None:
                result_holder = SubagentResult(
                    task_id=str(uuid.uuid4())[:8],
                    trace_id=self.trace_id,
                    status=SubagentStatus.RUNNING,
                )
            result_holder.try_set_terminal(SubagentStatus.FAILED, error=str(exc))
            return result_holder

    def execute_async(self, task: str, task_id: str | None = None) -> str:
        """后台启动任务执行并立即返回 task_id。

        输入:
            task: str — 任务描述
            task_id: str | None — 指定 task_id（默认 tool_call_id），None 时随机生成

        输出:
            str — task_id，供轮询与取消使用

        工作流:
            (1) 创建 PENDING 结果档案并注册到 _background_tasks
            (2) 提交到全局线程池；线程内提交隔离 loop 执行并等待（timeout 兜底）
            (3) 超时 → cancel_event + TIMED_OUT；异常 → FAILED
        """
        if task_id is None:
            task_id = str(uuid.uuid4())[:8]

        result = SubagentResult(
            task_id=task_id,
            trace_id=self.trace_id,
            status=SubagentStatus.PENDING,
        )
        with _background_tasks_lock:
            _background_tasks[task_id] = result

        parent_context = _copy_isolated_subagent_context()

        def run_task() -> None:
            with _background_tasks_lock:
                holder = _background_tasks[task_id]
                holder.status = SubagentStatus.RUNNING
                holder.started_at = datetime.now()

            try:
                execution_future = _submit_to_isolated_loop_in_context(
                    parent_context,
                    lambda: self._aexecute(task, holder),
                )
                try:
                    execution_future.result(timeout=self.config.timeout_seconds)
                except FuturesTimeoutError:
                    logger.error(
                        "[trace=%s] Subagent %s 执行超时 %ss",
                        self.trace_id,
                        self.config.name,
                        self.config.timeout_seconds,
                    )
                    holder.cancel_event.set()
                    holder.try_set_terminal(
                        SubagentStatus.TIMED_OUT,
                        error=f"Execution timed out after {self.config.timeout_seconds} seconds",
                    )
                    execution_future.cancel()
            except Exception as exc:
                logger.exception("[trace=%s] Subagent %s 后台执行失败", self.trace_id, self.config.name)
                holder.try_set_terminal(SubagentStatus.FAILED, error=str(exc))

        _scheduler_pool.submit(run_task)
        return task_id


def _filter_tools(
    tools: list[BaseTool],
    allow: list[str] | None,
    disallow: list[str] | None,
) -> list[BaseTool]:
    """按白名单/黑名单过滤工具列表。

    输入:
        tools: list[BaseTool] — 候选工具
        allow: list[str] | None — 白名单；None 表示继承全部
        disallow: list[str] | None — 黑名单

    输出:
        list[BaseTool] — 过滤后工具

    工作流:
        (1) 白名单非 None → 只保留白名单内的工具
        (2) 黑名单非 None → 移除黑名单内的工具
    """
    result = list(tools)
    if allow is not None:
        allow_set = set(allow)
        result = [tool for tool in result if tool.name in allow_set]
    if disallow is not None:
        disallow_set = set(disallow)
        result = [tool for tool in result if tool.name not in disallow_set]
    return result


def request_cancel_background_task(task_id: str) -> None:
    """请求取消后台任务（协作式：设置 cancel_event，线程在迭代边界停止）。

    输入:
        task_id: str — 任务 ID

    输出:
        None
    """
    with _background_tasks_lock:
        result = _background_tasks.get(task_id)
        if result is not None:
            result.cancel_event.set()
            logger.info("已请求取消后台任务 %s", task_id)


def get_background_task_result(task_id: str) -> SubagentResult | None:
    """获取后台任务结果。

    输入:
        task_id: str — 任务 ID

    输出:
        SubagentResult | None — 未找到时返回 None
    """
    with _background_tasks_lock:
        return _background_tasks.get(task_id)


def cleanup_background_task(task_id: str) -> None:
    """从后台注册表移除任务（仅终态，避免与执行线程竞态）。

    输入:
        task_id: str — 任务 ID

    输出:
        None
    """
    with _background_tasks_lock:
        result = _background_tasks.get(task_id)
        if result is None:
            return
        if result.status.is_terminal or result.completed_at is not None:
            del _background_tasks[task_id]


atexit.register(_shutdown_isolated_subagent_loop)
