"""
本文件对外提供 task 委托工具：lead agent 把有界任务派发给 subagent 的唯一入口。

对外提供:
    task_tool — 内置委托工具（后台启动 + 工具内轮询 + 流式自定义事件 + 结构化结果）

输入:
    description: str — 3-5 词任务标签（日志/展示）
    prompt: str — 任务描述
    subagent_type: str — 注册表类型名
    tool_call_id: str — 注入的当前工具调用 ID（即 task_id）
    runtime: ToolRuntime — 注入的运行时（state/context/config）

输出:
    Command — ToolMessage（模型可见结果文本 + additional_kwargs 结构化元数据）

具体工作流:
    (1) 校验类型存在与 bash 可用性
    (2) 从 runtime 捕获父上下文（thread_id / user_id / model_name / tool_groups）
    (3) execute_async 后台启动，task_id=tool_call_id
    (4) 每 5s 轮询：新增 AI 消息发 task_running；终态发对应事件并返回
    (5) 轮询超时（timeout+60s）→ polling_timed_out；父取消 → 协作取消 + shielded 等待

示例:
    result = await task_tool.ainvoke({
        "description": "调研竞品定价",
        "prompt": "调研 Acme 前 5 名竞品的定价并汇总",
        "subagent_type": "general-purpose",
    })
"""

import asyncio
import logging
import uuid
from typing import Annotated, Any

from langchain.tools import InjectedToolCallId, tool
from langchain_core.messages import ToolMessage
from langgraph.config import get_stream_writer
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command

from caspian.config import get_app_config
from caspian.subagents import SubagentExecutor, get_available_subagent_names, get_subagent_config
from caspian.subagents.executor import (
    SubagentStatus,
    cleanup_background_task,
    get_background_task_result,
    request_cancel_background_task,
)
from caspian.subagents.status_contract import (
    SubagentStatusValue,
    SubagentStopReasonValue,
    format_subagent_result_message,
    make_subagent_additional_kwargs,
)

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 5

_BASH_SUPPORTED_SANDBOX_PREFIXES = (
    "caspian.sandbox.local",
    "caspian.community.aio_sandbox",
)


def is_host_bash_allowed(app_config: Any | None = None) -> bool:
    """判断当前沙箱配置是否允许 bash 执行。

    输入:
        app_config: AppConfig | None — 配置对象，None 时自动加载

    输出:
        bool — LocalSandbox / AioSandbox 返回 True，未知沙箱类型返回 False

    工作流:
        (1) 读取 sandbox.use 配置
        (2) 命中内置沙箱前缀返回 True，否则 False
    """
    if app_config is None:
        try:
            app_config = get_app_config("config.yaml")
        except FileNotFoundError:
            return False
    sandbox_use = str(getattr(getattr(app_config, "sandbox", None), "use", ""))
    return sandbox_use.startswith(_BASH_SUPPORTED_SANDBOX_PREFIXES)


def _emit_custom_event(payload: dict[str, Any]) -> None:
    """向当前 stream writer 发布自定义事件（无 writer 时静默跳过）。"""
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    writer(payload)


def _task_result_command(
    *,
    tool_call_id: str,
    status: SubagentStatusValue,
    result: str | None = None,
    error: str | None = None,
    stop_reason: SubagentStopReasonValue | None = None,
    model_name: str | None = None,
) -> Command:
    """构造终态 ToolMessage 的 Command 更新。

    输入:
        tool_call_id: str — 当前工具调用 ID
        status / result / error / stop_reason / model_name — 终态信息

    输出:
        Command — 携带 ToolMessage 的状态更新
    """
    content, metadata_error = format_subagent_result_message(
        status, result=result, error=error, stop_reason=stop_reason
    )
    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=content,
                    tool_call_id=tool_call_id,
                    name="task",
                    additional_kwargs=make_subagent_additional_kwargs(
                        status,
                        result=result,
                        error=metadata_error,
                        stop_reason=stop_reason,
                        model_name=model_name,
                    ),
                )
            ]
        }
    )


@tool("task", parse_docstring=True)
async def task_tool(
    runtime: ToolRuntime,
    description: str,
    prompt: str,
    subagent_type: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> str | Command:
    """把有界任务委托给专用 subagent，在其独立上下文中执行。

    仅在预期收益明显超过委托开销时委托。有用收益：
    - 独立并行工作带来的实际墙钟时间节省
    - 专用工具、技能、模型或领域指令
    - 有界、异常上下文密集调查的上下文隔离

    不要委托的场景：
    - 仅因为任务复杂、多步、冗长或涉及大仓库
    - 跨 subagent 拆分相互依赖的步骤（保持链条完整）
    - 重叠文件、共享可变状态或外部副作用
    - 需要用户交互或澄清的任务

    委托代价：多个上下文重复仓库发现、结果协调验证与综合、父可更便宜完成的直接工具调用。

    内置 subagent 类型:
    - **general-purpose**: 通用推理与执行 agent，适合有界探索与执行
    - **bash**: 沙箱命令行执行专家，仅限有界 shell 工作流
    其他类型可在 config.yaml subagents.custom_agents 定义。

    Args:
        description: 任务的简短（3-5 词）描述，用于日志/展示。ALWAYS PROVIDE FIRST.
        prompt: 交给 subagent 的任务描述，须具体明确。ALWAYS PROVIDE SECOND.
        subagent_type: 使用的 subagent 类型。ALWAYS PROVIDE THIRD.
    """
    app_config = get_app_config("config.yaml")
    available_names = get_available_subagent_names(app_config=app_config)

    config = get_subagent_config(subagent_type, app_config=app_config)
    if config is None:
        available = ", ".join(available_names)
        return _task_result_command(
            tool_call_id=tool_call_id,
            status="failed",
            error=f"Unknown subagent type '{subagent_type}'. Available: {available}",
        )
    if subagent_type == "bash" and not is_host_bash_allowed(app_config):
        return _task_result_command(
            tool_call_id=tool_call_id,
            status="failed",
            error=(
                "Bash subagent is not available in the current sandbox configuration. "
                "Use direct shell tools or configure a bash-capable sandbox."
            ),
        )

    # 父上下文捕获
    thread_id = None
    if runtime.config is not None:
        thread_id = runtime.config.get("configurable", {}).get("thread_id")
    context = runtime.context if isinstance(runtime.context, dict) else {}
    user_id = context.get("user_id")
    parent_model = context.get("model_name")
    tool_groups = context.get("tool_groups")
    trace_id = context.get("run_id") or str(uuid.uuid4())[:8]

    # 继承父工具集（排除 task 防递归、排除 uploads 工具）
    from caspian.tools import get_available_tools

    tools = await get_available_tools(
        app_config=app_config,
        tool_groups=tool_groups,
        subagent_enabled=False,
    )

    executor = SubagentExecutor(
        config,
        tools,
        parent_model=parent_model,
        user_id=user_id,
        thread_id=thread_id,
        tool_groups=tool_groups,
        trace_id=trace_id,
    )

    # 后台启动，task_id=tool_call_id
    task_id = executor.execute_async(prompt, task_id=tool_call_id)
    max_poll_count = (config.timeout_seconds + 60) // _POLL_INTERVAL_SECONDS

    logger.info(
        "后台任务已启动 task_id=%s (subagent=%s, timeout=%ss, polling_limit=%s)",
        task_id,
        subagent_type,
        config.timeout_seconds,
        max_poll_count,
    )

    _emit_custom_event(
        {
            "type": "task_started",
            "task_id": task_id,
            "description": description,
            "model_name": executor.model_name,
        }
    )

    poll_count = 0
    last_status = None
    last_message_count = 0

    try:
        while True:
            result = get_background_task_result(task_id)
            if result is None:
                logger.error("任务 %s 已从后台注册表消失", task_id)
                return _task_result_command(
                    tool_call_id=tool_call_id,
                    status="failed",
                    error=f"Task {task_id} disappeared from background tasks",
                )

            if result.status != last_status:
                logger.info("任务 %s 状态: %s", task_id, result.status.value)
                last_status = result.status

            # 新步骤消息 → task_running 事件
            ai_messages = result.ai_messages or []
            current_count = len(ai_messages)
            if current_count > last_message_count:
                for index in range(last_message_count, current_count):
                    _emit_custom_event(
                        {
                            "type": "task_running",
                            "task_id": task_id,
                            "message": ai_messages[index],
                            "message_index": index + 1,
                            "total_messages": current_count,
                            "model_name": executor.model_name,
                        }
                    )
                last_message_count = current_count

            if result.status == SubagentStatus.COMPLETED:
                _emit_custom_event(
                    {
                        "type": "task_completed",
                        "task_id": task_id,
                        "result": result.result,
                        "model_name": executor.model_name,
                    }
                )
                cleanup_background_task(task_id)
                return _task_result_command(
                    tool_call_id=tool_call_id,
                    status="completed",
                    result=result.result,
                    stop_reason=result.stop_reason,
                    model_name=executor.model_name,
                )
            if result.status == SubagentStatus.FAILED:
                _emit_custom_event(
                    {
                        "type": "task_failed",
                        "task_id": task_id,
                        "error": result.error,
                        "model_name": executor.model_name,
                    }
                )
                cleanup_background_task(task_id)
                return _task_result_command(
                    tool_call_id=tool_call_id,
                    status="failed",
                    error=result.error,
                    stop_reason=result.stop_reason,
                    model_name=executor.model_name,
                )
            if result.status == SubagentStatus.CANCELLED:
                _emit_custom_event(
                    {
                        "type": "task_cancelled",
                        "task_id": task_id,
                        "error": result.error,
                        "model_name": executor.model_name,
                    }
                )
                cleanup_background_task(task_id)
                return _task_result_command(
                    tool_call_id=tool_call_id,
                    status="cancelled",
                    error=result.error,
                    model_name=executor.model_name,
                )
            if result.status == SubagentStatus.TIMED_OUT:
                _emit_custom_event(
                    {
                        "type": "task_timed_out",
                        "task_id": task_id,
                        "error": result.error,
                        "model_name": executor.model_name,
                    }
                )
                cleanup_background_task(task_id)
                return _task_result_command(
                    tool_call_id=tool_call_id,
                    status="timed_out",
                    error=result.error,
                    model_name=executor.model_name,
                )

            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            poll_count += 1

            # 轮询超时安全网（后台线程超时未生效时的兜底）
            if poll_count > max_poll_count:
                logger.error(
                    "任务 %s 轮询超时（%s 次轮询），请求协作取消",
                    task_id,
                    poll_count,
                )
                request_cancel_background_task(task_id)
                return _task_result_command(
                    tool_call_id=tool_call_id,
                    status="polling_timed_out",
                    error=(
                        f"Task polling timed out after {config.timeout_seconds // 60} minutes. "
                        "This may indicate the background task is stuck."
                    ),
                    model_name=executor.model_name,
                )
    except asyncio.CancelledError:
        # 父 run 被取消：协作取消后台任务并等待其终态
        request_cancel_background_task(task_id)
        try:
            await asyncio.shield(_await_subagent_terminal(task_id, max_poll_count))
        except asyncio.CancelledError:
            pass
        raise


async def _await_subagent_terminal(task_id: str, max_polls: int) -> Any | None:
    """轮询等待后台任务到达终态（最多 max_polls 次）。

    输入:
        task_id: str — 任务 ID
        max_polls: int — 最大轮询次数

    输出:
        SubagentResult | None — 终态结果；未到达终态或已消失返回 None
    """
    for _ in range(max_polls):
        result = get_background_task_result(task_id)
        if result is None:
            return None
        if result.status.is_terminal or result.completed_at is not None:
            return result
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    return None
