"""本文件对外提供共享会话 checkpoint 的单写入者投影器。

输入为已合流消息、权威表修订和各 Run 游标；输出为可恢复的 LangGraph checkpoint ID。
工作流使用独立投影 graph 与专用 thread key 写入共享状态，不包含任何 Run 的待执行工具队列。
示例：`writer = SharedCheckpointWriter(checkpointer); checkpoint_id = await writer.write("u", "t", messages, 1, cursors)`。
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages


class SharedState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    table_revision: int
    run_cursors: dict[str, int]


def _projection_node(_state: SharedState) -> dict:
    return {}


class SharedCheckpointWriter:
    def __init__(self, checkpointer):
        builder = StateGraph(SharedState)
        builder.add_node("projection", _projection_node)
        builder.add_edge(START, "projection")
        builder.add_edge("projection", END)
        self._graph = builder.compile(checkpointer=checkpointer)

    @staticmethod
    def config(user_id: str, thread_id: str) -> dict:
        return {"configurable": {"thread_id": f"decision-shared:{user_id}:{thread_id}"}}

    async def write(
        self, user_id: str, thread_id: str,
        messages: list[BaseMessage], table_revision: int, run_cursors: dict[str, int],
    ) -> str:
        result = await self._graph.aupdate_state(
            self.config(user_id, thread_id),
            {"messages": messages, "table_revision": table_revision, "run_cursors": run_cursors},
            as_node="projection",
        )
        return str(result["configurable"]["checkpoint_id"])

    async def read(self, user_id: str, thread_id: str):
        return await self._graph.aget_state(self.config(user_id, thread_id))
