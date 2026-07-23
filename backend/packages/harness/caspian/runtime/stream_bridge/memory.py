"""
本文件对外提供 MemoryStreamBridge 类，作为 StreamBridge 抽象接口的进程内内存实现。

对外提供:
    MemoryStreamBridge(StreamBridge) — 基于 asyncio.Condition 的进程内流式桥接器

内部类:
    _RunStream — 单个 run 的流式数据和流状态容器，使用 asyncio.Condition 保护并发访问

输入:
    MemoryStreamBridge.__init__(*, queue_maxsize: int = 256):
        queue_maxsize — 每个 run 的事件缓冲区最大容量，超出时裁剪最旧事件

输出:
    MemoryStreamBridge 实例

工作流:
    MemoryStreamBridge 维护 self._streams: dict[str, _RunStream] 和 self._counters: dict[str, int]
    分别管理每个 run 的事件缓冲区和事件 ID 计数器。
    publish 自动分配 ID → 追加事件 → 裁剪超限 → notify_all
    publish_end 设置 ended → notify_all
    subscribe 通过 async generator 实现三态循环（有事件 / 已结束 / 等待超时）
    cleanup 支持立即清理与延迟清理

示例:
    from caspian.runtime.stream_bridge.memory import MemoryStreamBridge
    from caspian.runtime.stream_bridge.schemas import StreamEvent, HEARTBEAT_SENTINEL, END_SENTINEL

    bridge = MemoryStreamBridge(queue_maxsize=512)
    bridge.publish("run-001", StreamEvent(id="", event="metadata", data={"hello": "world"}))
    async for event in bridge.subscribe("run-001"):
        if event is END_SENTINEL:
            break
        print(event.event, event.data)
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from caspian.runtime.stream_bridge.base import StreamBridge
from caspian.runtime.stream_bridge.schemas import (
    END_SENTINEL,
    HEARTBEAT_SENTINEL,
    StreamEvent,
)

logger = logging.getLogger(__name__)


@dataclass
class _RunStream:
    """单个 run 对应的流式数据和流状态容器。

    asyncio.Condition 同时提供 Lock（保证流状态一致性）和 wait()/notify_all()（等待并通知新事件）。
    同一个 _RunStream 至少被以下协程并发访问：
        生产者：bridge.publish()
        消费者：bridge.subscribe()
        结束者：bridge.publish_end()
        以及可能的多个重连/join 订阅者。
    """

    events: list[StreamEvent] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    ended: bool = False
    start_offset: int = 0  # 当前 events[0] 在整个事件流中的绝对位置


class MemoryStreamBridge(StreamBridge):
    """StreamBridge 的进程内内存实现。"""

    def __init__(self, *, queue_maxsize: int = 256) -> None:
        """初始化 MemoryStreamBridge。

        输入:
            queue_maxsize: int — 每个 run 的事件缓冲区最大容量，默认 256

        输出:
            MemoryStreamBridge 实例
        """
        self._maxsize = queue_maxsize
        self._streams: dict[str, _RunStream] = {}
        self._counters: dict[str, int] = {}

    # === helpers ===

    def _get_or_create_stream(self, run_id: str) -> _RunStream:
        """获取或创建指定 run_id 的 _RunStream。

        输入:
            run_id: str — run 的唯一标识

        输出:
            _RunStream 实例
        """
        if run_id not in self._streams:
            self._streams[run_id] = _RunStream()
        return self._streams[run_id]

    # === 核心操作 ===

    def publish(self, run_id: str, event: StreamEvent) -> None:
        """生产者端：给指定的 run_id 入队一条事件。

        输入:
            run_id: str      — run 的唯一标识
            event: StreamEvent — 要入队的流式事件；若 id 为空字符串则自动分配单调递增 ID

        输出:
            None
        """
        stream = self._get_or_create_stream(run_id)

        async def _do_publish() -> None:
            async with stream.condition:
                if stream.ended:
                    return  # publish_end 之后不再接受新事件

                run_counter = self._counters.get(run_id, 0)

                if event.id == "":
                    run_counter += 1
                    self._counters[run_id] = run_counter
                    evt = StreamEvent(id=str(run_counter), event=event.event, data=event.data)
                else:
                    evt = event

                stream.events.append(evt)

                if len(stream.events) > self._maxsize:
                    trimmed = len(stream.events) - self._maxsize
                    stream.events = stream.events[trimmed:]
                    stream.start_offset += trimmed

                stream.condition.notify_all()

        try:
            asyncio.get_running_loop()
            asyncio.create_task(_do_publish())
        except RuntimeError:
            pass

    def publish_end(self, run_id: str) -> None:
        """表明当前 run_id 不会再有事件产生。

        输入:
            run_id: str — run 的唯一标识

        输出:
            None
        """
        stream = self._get_or_create_stream(run_id)

        async def _do_end() -> None:
            async with stream.condition:
                stream.ended = True
                stream.condition.notify_all()

        try:
            asyncio.get_running_loop()
            asyncio.create_task(_do_end())
        except RuntimeError:
            pass

    async def subscribe(
        self,
        run_id: str,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15,
    ) -> AsyncIterator[StreamEvent]:
        """消费者端：为指定的 run_id 产出事件。

        输入:
            run_id: str             — 订阅哪个 run
            last_event_id: str | None — 断线重连起点
            heartbeat_interval: float — 心跳间隔（秒），默认 15

        输出:
            AsyncIterator[StreamEvent] — 逐条产出事件，无新事件时产出 HEARTBEAT_SENTINEL，
                                        流结束时产出 END_SENTINEL
        """
        stream = self._get_or_create_stream(run_id)

        async with stream.condition:
            if last_event_id is not None:
                try:
                    target_id = int(last_event_id)
                except (ValueError, TypeError):
                    target_id = 0
                next_offset = target_id
            else:
                next_offset = 0

            if next_offset < stream.start_offset:
                next_offset = stream.start_offset

        while True:
            async with stream.condition:
                local_index = next_offset - stream.start_offset

                if 0 <= local_index < len(stream.events):
                    event = stream.events[local_index]
                    next_offset += 1
                    yield event
                    continue

                if stream.ended:
                    yield END_SENTINEL
                    return

                try:
                    await asyncio.wait_for(
                        stream.condition.wait(),
                        timeout=heartbeat_interval,
                    )
                except TimeoutError:
                    yield HEARTBEAT_SENTINEL

    def cleanup(self, run_id: str, delay: float | None = None) -> None:
        """释放与指定 run_id 关联的资源。

        输入:
            run_id: str          — run 的唯一标识
            delay: float | None  — 延迟秒数，None 立即清理，有值则延迟后清理

        输出:
            None
        """
        if delay is None:
            self._streams.pop(run_id, None)
            self._counters.pop(run_id, None)
            logger.info("run '%s' 流资源已清理", run_id)
            return

        async def _delayed_cleanup() -> None:
            await asyncio.sleep(delay)
            self._streams.pop(run_id, None)
            self._counters.pop(run_id, None)
            logger.info("run '%s' 流资源已延迟清理 (delay=%.1fs)", run_id, delay)

        try:
            asyncio.get_running_loop()
            asyncio.create_task(_delayed_cleanup())
        except RuntimeError:
            self._streams.pop(run_id, None)
            self._counters.pop(run_id, None)
