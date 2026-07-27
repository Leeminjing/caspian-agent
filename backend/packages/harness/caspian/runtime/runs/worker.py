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

输出:
    SSE 事件流 → bridge → 前端；最终状态 → run_manager

具体工作流:
    (1) 设置 run 状态为 running，发布 metadata 事件（含 run_id + thread_id）
    (2) 读取当前 thread 的旧 checkpoint 保存为 rollback 快照（checkpointer 可用时）
    (3) 从 record.model_name 取模型名，从 langgraph_context 取 user_id，创建 agent
    (3.5) 将 checkpointer 挂载到 agent.checkpointer，将 store 挂载到 agent.store
    (4) 翻译 stream_modes（前端名称 → LangGraph 内部名称）
    (5) 调用 agent.astream()，每轮检查 abort_event
    (6) 每个 chunk 转换为 SSE 事件 publish 到 bridge
    (7) 终态处理：success / interrupted（rollback 时用旧 checkpoint 恢复 thread 状态）/ error
    (8) finally: publish_end 关流 + 延迟缓存清理

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

import logging
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore
from langgraph.types import Command, Interrupt

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
        if app_config.commitment.enabled:
            if isinstance(mapped_stream_modes, str):
                if mapped_stream_modes != "values":
                    mapped_stream_modes = [mapped_stream_modes, "values"]
            elif mapped_stream_modes is not None and "values" not in mapped_stream_modes:
                mapped_stream_modes = [*mapped_stream_modes, "values"]
        user_id = langgraph_context.get("user_id") if langgraph_context else None

        agent = await make_lead_agent(
            model_name=model_name or None,
            agent_name=agent_name,
            tool_groups=tool_groups,
            user_id=user_id,
        )

        # (3.5) 挂载 checkpointer 和 store 到 agent
        if checkpointer is not None:
            agent.checkpointer = checkpointer
        if store is not None:
            agent.store = store

        # (4) agent.astream 主循环
        graph_interrupted = False
        async for chunk in agent.astream(
            graph_input,
            config=runnable_config,
            context=langgraph_context,
            stream_mode=mapped_stream_modes,
        ):
            # (5) abort 中断检查
            if record.abort_event.is_set():
                logger.info("run '%s' 收到 abort 信号，停止执行", record.run_id)
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

            # 每个 chunk 序列化后转换为 SSE 事件 publish 到 bridge
            serialized_chunk = _serialize_chunk(chunk)
            chunk_event = _build_chunk_event("events", serialized_chunk)
            bridge.publish(record.run_id, chunk_event)

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
        # (7) 关流 + 延迟清理
        bridge.publish_end(record.run_id)
        bridge.cleanup(record.run_id, delay=300)
        logger.info("run '%s' 流资源已关流，延迟清理已安排", record.run_id)
