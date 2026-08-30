"""
本文件对外提供 `run_agent` 异步函数，作为 core-aob 链路的执行层主入口。

对外提供:
    run_agent — 在后台 asyncio task 中运行 agent graph，将 stream chunk 通过 StreamBridge 发布为 SSE 事件

输入:
    record: RunRecord — 当前 run 的运行时档案，含 run_id、model_name、abort_event 等
    bridge: StreamBridge — 进程内事件总线，worker 通过它发布事件
    run_manager: RunManager — run 状态管理注册表，用于更新状态
    app_config: AppConfig — 组合根配置
    graph_input: dict — agent graph 的输入（含 messages 等）
    runnable_config: RunnableConfig — LangGraph 执行配置
    stream_modes: list[str] | str | None — 前端传入的 stream mode
    agent_name: str | None — agent 名称
    tool_groups: list[str] | None — 工具分组过滤
    langgraph_context: dict | None — LangGraph context，传给 agent.astream(context=...)，框架据此构建 Runtime 供节点读取；其中 user_id 传给 make_lead_agent 用于定位 per-user custom skills
    checkpointer: BaseCheckpointSaver | None — checkpoint 持久化器，None 时不启用
    store: BaseStore | None — 跨 thread 长期记忆存储，None 时不启用
    COMMITMENT_MESSAGES_MONITOR — 设为 1 时把角色 messages 镜像到标准输出

输出:
    SSE 事件流 → bridge → 前端；可选 messages 镜像 → 标准输出；最终状态 → run_manager

具体工作流:
    (1) 设置 run 状态为 running，发布 metadata 事件（含 run_id + thread_id）
    (2) 读取当前 thread 的旧 checkpoint 保存为 rollback 快照（checkpointer 可用时）
    (3) 从 record.model_name 取模型名，从 langgraph_context 取 user_id，创建 agent
    (3.5) 将 checkpointer 挂载到 agent.checkpointer，将 store 挂载到 agent.store
    (4) 翻译 stream_modes（前端名称 → LangGraph 内部名称）
    (5) 调用 agent.astream()，每轮检查 abort_event
    (6) 每个 chunk 转换为 SSE 事件 publish 到 bridge
    (7) 监控开关开启时镜像 commitment_messages，不改变 SSE 消费
    (8) 终态处理：success / interrupted（rollback 时用旧 checkpoint 恢复 thread 状态）/ error
    (9) finally: publish_end 关流 + 延迟缓存清理

示例:
    task = asyncio.create_task(run_agent(
        record=record,
        bridge=bridge,
        run_manager=run_manager,
        app_config=app_config,
        graph_input={"messages": [HumanMessage(content="你好")]},
        runnable_config={"configurable": {"thread_id": "th-001"}},
        stream_modes=["values"],
        langgraph_context={"model_name": "deepseek-v4-flash", "app_config": app_config, "user_id": "uuid-xxx"},
    ))
"""

import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore
from langgraph.types import Command, Interrupt
from langgraph.errors import GraphInterrupt

from caspian.agents.lead import make_lead_agent
from caspian.config.app_config import AppConfig
from caspian.runtime.runs.manager import RunManager, RunRecord
from caspian.runtime.runs.schemas import RunStatus
from caspian.runtime.stream_bridge.base import StreamBridge
from caspian.runtime.stream_bridge.schemas import StreamEvent

logger = logging.getLogger(__name__)

# stream_mode 名称映射表：前端/SSE → LangGraph 内部名称
_STREAM_MODE_MAP: dict[str, str] = {
    "messages-tuple": "messages",
    "values": "values",
    "updates": "updates",
    "custom": "custom",
}

# 与 values 不兼容的 mode，需要跳过
_SKIP_MODES: frozenset = frozenset({"events"})


def _map_stream_modes(stream_modes: list[str] | str | None) -> list[str] | str | None:
    """将前端/SSE 名称翻译为 LangGraph 内部 stream_mode 名称。

    输入:
        stream_modes: list[str] | str | None — 前端传入的 stream mode

    输出:
        list[str] | str | None — 翻译后的 LangGraph stream_mode

    工作流:
        (1) 若为 None 或空，返回 ["values"] 作为默认
        (2) 若为单个字符串，按映射表翻译
        (3) 若为列表，逐项翻译，跳过不兼容的 mode（如 events）
        (4) 翻译后若为空列表，回退为 ["values"]
    """
    if not stream_modes:
        return ["values"]

    if isinstance(stream_modes, str):
        return _STREAM_MODE_MAP.get(stream_modes, stream_modes)

    mapped: list[str] = []
    for mode in stream_modes:
        if mode in _SKIP_MODES:
            logger.info("stream_mode '%s' 与 values 不兼容，已跳过", mode)
            continue
        mapped.append(_STREAM_MODE_MAP.get(mode, mode))

    if not mapped:
        return ["values"]

    return mapped


def _build_chunk_event(event_type: str, data: dict | None = None) -> StreamEvent:
    """将 chunk 转换为 StreamEvent（id 留空由 bridge 自动分配）。

    输入:
        event_type: str — SSE 事件类型
        data: dict | None — 事件载荷

    输出:
        StreamEvent — id 为空字符串，由 bridge.publish 自动分配
    """
    return StreamEvent(id="", event=event_type, data=data)


def _serialize_chunk(data: Any) -> Any:
    """递归转换 chunk 中的 LangChain Message 对象为 JSON 可序列化的 dict。

    输入:
        data: Any — agent.astream 产出的 chunk（可能含 BaseMessage 子类实例）

    输出:
        Any — JSON 可序列化的等价值

    工作流:
        (1) 若 data 是 BaseMessage 实例 → 调用 .model_dump() 转为 dict
        (2) 若 data 是 dict → 递归处理每个 value，保留原始 key
        (3) 若 data 是 list 或 tuple → 递归处理每个元素，保留原始容器类型
        (4) 其他类型 → 原样返回

    示例:
        >>> chunk = {"messages": [HumanMessage(content="你好")]}
        >>> serialized = _serialize_chunk(chunk)
        >>> json.dumps(serialized)  # 不再抛 TypeError
    """
    if isinstance(data, BaseMessage):
        return data.model_dump()

    if isinstance(data, Interrupt):
        return {
            "id": data.id,
            "value": _serialize_chunk(data.value),
        }

    if isinstance(data, dict):
        return {key: _serialize_chunk(value) for key, value in data.items()}

    if isinstance(data, list):
        return [_serialize_chunk(item) for item in data]

    if isinstance(data, tuple):
        return tuple(_serialize_chunk(item) for item in data)

    return data


def _extract_interrupts(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, Interrupt):
        return [_serialize_chunk(data)]
    if isinstance(data, dict):
        result: list[dict[str, Any]] = []
        for value in data.values():
            result.extend(_extract_interrupts(value))
        return result
    if isinstance(data, (list, tuple)):
        result = []
        for item in data:
            result.extend(_extract_interrupts(item))
        return result
    return []


def _accumulate_usage(record: RunRecord, mode: str, chunk: Any) -> None:
    """从 stream chunk 累计标准 input / cache-read usage 到 RunRecord。

    输入:
        record: RunRecord — 当前 run 档案
        mode: str — stream mode
        chunk: Any — astream 产出的 chunk

    输出:
        None — 原地累计 record.prompt_input_tokens / prompt_cache_hit_tokens

    具体工作流:
        (1) messages 模式: 模型流终块携带 usage_metadata
        (2) values 模式: 状态快照中新追加的 AI 消息携带 usage_metadata
            （LangChain 组装消息时跨 chunk 求和 usage），按消息 id 去重每条计一次
        (3) ponytail: 若同时启用 messages 与 values 模式会双重累计；当前装配只流 values+custom
    """
    if mode == "messages":
        try:
            message, _metadata = chunk
        except (TypeError, ValueError):
            return
        _add_usage(record, getattr(message, "usage_metadata", None))
        return
    if mode != "values" or not isinstance(chunk, dict):
        return
    for message in chunk.get("messages", []) or []:
        message_id = getattr(message, "id", None)
        if message_id is not None:
            if message_id in record._usage_seen_ids:
                continue
            record._usage_seen_ids.add(message_id)
        _add_usage(record, getattr(message, "usage_metadata", None))


def _add_usage(record: RunRecord, usage: Any) -> None:
    """把单条消息的 usage_metadata 累计进 RunRecord（受保护 helper）。"""
    if not usage:
        return
    input_tokens = usage.get("input_tokens")
    cache_read = (usage.get("input_token_details") or {}).get("cache_read")
    if isinstance(input_tokens, int):
        record.prompt_input_tokens += input_tokens
    if isinstance(cache_read, int):
        record.prompt_cache_hit_tokens += cache_read


async def _stream_one_round(
    agent: Any,
    graph_input: dict | Command,
    runnable_config: RunnableConfig,
    stream_mode: list[str] | str | None,
    langgraph_context: dict | None,
    record: RunRecord,
    bridge: StreamBridge,
) -> tuple[bool, bool]:
    """执行一轮 agent.astream 并发布 SSE 事件。

    输入:
        agent / graph_input / runnable_config / stream_mode / langgraph_context — 同 run_agent 参数
        record: RunRecord — 当前 run 档案
        bridge: StreamBridge — 事件总线

    输出:
        tuple[bool, bool] — (graph_interrupted, aborted)；aborted 表示收到 abort 信号

    工作流:
        (1) 逐 chunk 累计 usage、检查 abort、提取 interrupt、序列化并 publish
        (2) commitment_messages 自定义事件与既有 special-case 一致
    """
    graph_interrupted = False
    aborted = False
    try:
        async for mode, chunk in agent.astream(
            graph_input,
            config=runnable_config,
            context=langgraph_context,
            stream_mode=stream_mode,
        ):
            _accumulate_usage(record, mode, chunk)
            if record.abort_event.is_set():
                logger.info("run '%s' 收到 abort 信号，停止执行", record.run_id)
                aborted = True
                break

            interrupts = _extract_interrupts(chunk)
            if interrupts:
                graph_interrupted = True
                for interrupt_data in interrupts:
                    bridge.publish(
                        record.run_id,
                        _build_chunk_event("interrupt", interrupt_data),
                    )
                continue

            serialized_chunk = _serialize_chunk(chunk)
            if (
                isinstance(serialized_chunk, (list, tuple))
                and len(serialized_chunk) == 2
                and serialized_chunk[0] == "custom"
                and isinstance(serialized_chunk[1], dict)
                and serialized_chunk[1].get("type") == "commitment_messages"
            ):
                if os.getenv("COMMITMENT_MESSAGES_MONITOR") == "1":
                    print(
                        "COMMITMENT_MESSAGES "
                        + json.dumps(
                            {
                                "run_id": record.run_id,
                                **serialized_chunk[1],
                            },
                            ensure_ascii=True,
                        ),
                        flush=True,
                    )
                bridge.publish(
                    record.run_id,
                    _build_chunk_event(
                        "events",
                        serialized_chunk[1],
                    ),
                )
                continue
            chunk_event = _build_chunk_event("events", serialized_chunk)
            bridge.publish(record.run_id, chunk_event)
    except GraphInterrupt as exc:
        # before_agent / 非工具节点内的 interrupt() 以 GraphInterrupt 抛出而非 chunk；
        # 捕获后同样发布 interrupt 事件，使其与工具中断的前端呈现一致。
        graph_interrupted = True
        interrupts = getattr(exc, "__interrupt__", None) or _extract_interrupts(exc)
        for interrupt_data in interrupts:
            bridge.publish(
                record.run_id,
                _build_chunk_event("interrupt", _serialize_chunk(interrupt_data)),
            )
    return graph_interrupted, aborted


async def run_agent(
    *,
    record: RunRecord,
    bridge: StreamBridge,
    run_manager: RunManager,
    app_config: AppConfig,
    graph_input: dict | Command,
    runnable_config: RunnableConfig,
    stream_modes: list[str] | str | None = None,
    agent_name: str | None = None,
    tool_groups: list[str] | None = None,
    langgraph_context: dict | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    store: BaseStore | None = None,
    before_end: Callable[[RunRecord], Awaitable[None]] | None = None,
) -> None:
    try:
        # (1) 置 running，发 metadata 事件
        run_manager.update(record.run_id, status=RunStatus.running)
        logger.info("run '%s' 状态 → running", record.run_id)

        metadata_event = _build_chunk_event("metadata", {
            "run_id": record.run_id,
            "thread_id": record.thread_id,
        })
        bridge.publish(record.run_id, metadata_event)

        # (2) 读取旧 checkpoint 保存 rollback 快照
        rollback_snapshot = None
        if checkpointer is not None:
            try:
                config_for_latest = {"configurable": {"thread_id": record.thread_id}}
                latest = await checkpointer.aget_tuple(config_for_latest)
                if latest is not None:
                    rollback_snapshot = latest
                    logger.info("run '%s' 已保存 rollback 快照 (checkpoint_id=%s)", record.run_id, latest.checkpoint.get("id", "?"))
                else:
                    logger.info("run '%s' thread 无旧 checkpoint，rollback 快照为空", record.run_id)
            except Exception:
                logger.warning("run '%s' 读取旧 checkpoint 失败，rollback 不可用", record.run_id, exc_info=True)

        # (3) 从 record.model_name 取模型名，从 langgraph_context 取 user_id，创建 agent
        model_name = record.model_name or (app_config.models[0].name if app_config.models else None)
        mapped_stream_modes = _map_stream_modes(stream_modes)
        if app_config.commitment.enabled or app_config.subagents.enabled:
            if isinstance(mapped_stream_modes, str):
                mapped_stream_modes = list(
                    dict.fromkeys([mapped_stream_modes, "values", "custom"])
                )
            elif mapped_stream_modes is not None:
                mapped_stream_modes = list(
                    dict.fromkeys([*mapped_stream_modes, "values", "custom"])
                )
        user_id = langgraph_context.get("user_id") if langgraph_context else None
        selected_skills = langgraph_context.get("selected_skills", []) if langgraph_context else []
        if langgraph_context is not None:
            # run_id 供 SubagentLimitMiddleware 按 run 记账委托总额
            langgraph_context["run_id"] = record.run_id

        agent = await make_lead_agent(
            model_name=model_name or None,
            agent_name=agent_name,
            tool_groups=tool_groups,
            user_id=user_id,
            selected_skills=selected_skills,
            subagent_enabled=app_config.subagents.enabled,
        )

        # (3.5) 挂载 checkpointer 和 store 到 agent
        if checkpointer is not None:
            agent.checkpointer = checkpointer
        if store is not None:
            agent.store = store

        # (4) agent.astream 主循环（目标模式下可由 GoalRoundDriver 驱动多轮）
        graph_interrupted = False
        goal_driver = None
        goal_mode_cfg = getattr(app_config, "goal_mode", None)
        if goal_mode_cfg is not None and goal_mode_cfg.enabled:
            from caspian.goal import GoalRoundDriver

            goal_thread_id = (
                (runnable_config.get("configurable") or {}).get("thread_id")
                if isinstance(runnable_config, dict)
                else None
            )
            goal_user_id = (langgraph_context or {}).get("user_id")
            if store is None:
                raise RuntimeError("goal_mode.enabled 需要 LangGraph store；请配置 langgraph_store")
            if goal_user_id and goal_thread_id:
                goal_driver = GoalRoundDriver(
                    store,
                    str(goal_user_id),
                    str(goal_thread_id),
                    goal_mode_cfg.default_max_goal_rounds,
                )
                await goal_driver.disarm_on_run_start()

        round_input = graph_input
        while True:
            round_interrupted, aborted = await _stream_one_round(
                agent,
                round_input,
                runnable_config,
                mapped_stream_modes,
                langgraph_context,
                record,
                bridge,
            )
            graph_interrupted = graph_interrupted or round_interrupted
            if goal_driver is not None:
                # 目标模式：每轮后发布 goal_state 事件，供前端渲染 Goal 徽章
                goal_view = await goal_driver.current_view()
                bridge.publish(record.run_id, _build_chunk_event("goal_state", {"goal": goal_view}))
            if aborted or goal_driver is None:
                break
            action = await goal_driver.decide_after_round()
            if action != "continue":
                break
            round_input = await goal_driver.build_next_round_input()

        # (6) 终态处理
        if record.abort_event.is_set():
            action = record.abort_action
            if action == "rollback":
                if rollback_snapshot is not None and checkpointer is not None:
                    try:
                        await checkpointer.aput(
                            rollback_snapshot.config,
                            rollback_snapshot.checkpoint,
                            rollback_snapshot.metadata,
                            {},
                        )
                        logger.info("run '%s' rollback 已恢复旧 checkpoint", record.run_id)
                    except Exception:
                        logger.error("run '%s' rollback 恢复 checkpoint 失败", record.run_id, exc_info=True)
                else:
                    logger.info("run '%s' rollback 无旧快照可恢复", record.run_id)
                run_manager.update(record.run_id, status=RunStatus.interrupted)
            else:
                run_manager.update(record.run_id, status=RunStatus.interrupted)
                logger.info("run '%s' 状态 → interrupted", record.run_id)
        elif graph_interrupted:
            run_manager.update(record.run_id, status=RunStatus.interrupted)
            logger.info("run '%s' 状态 → interrupted (graph interrupt)", record.run_id)
        else:
            run_manager.update(record.run_id, status=RunStatus.success)
            logger.info("run '%s' 状态 → success", record.run_id)

    except Exception as exc:
        logger.error("run '%s' 异常: %s", record.run_id, exc, exc_info=True)
        run_manager.update(record.run_id, status=RunStatus.error, error=str(exc))
        error_event = _build_chunk_event("error", {"error": str(exc)})
        try:
            bridge.publish(record.run_id, error_event)
        except Exception:
            logger.error("发布 error 事件失败", exc_info=True)

    finally:
        # (6.5) 终态钩子（如 usage 落库）先于关流，保证订阅者收到 end 帧时数据已就绪
        if before_end is not None:
            try:
                await before_end(record)
            except Exception:
                logger.error("run '%s' before_end 钩子失败", record.run_id, exc_info=True)
        # (7) 关流 + 延迟清理
        bridge.publish_end(record.run_id)
        bridge.cleanup(record.run_id, delay=300)
        logger.info("run '%s' 流资源已关流，延迟清理已安排", record.run_id)
