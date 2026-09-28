"""本文件对外提供已验证表视图、完整渲染和模型请求唯一性校验。

输入为权威快照与 ModelRequest；输出为仅含一份当前有效表的模型请求。
工作流显式区分空表与当前表，移除旧注入消息，再用独立 system 段传递修订、摘要和完整行。
示例：`bound = bind_model_request(request, TableView.from_snapshot(snapshot))`。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from langchain.agents.middleware import ModelRequest
from langchain_core.messages import SystemMessage

from caspian.decision_governance.domain import TableSnapshot

_START = "<current_decision_table "
_END = "</current_decision_table>"
_OLD_TABLE = re.compile(r"<decision_table\b[^>]*>.*?</decision_table>", re.DOTALL)
_OLD_CURRENT = re.compile(r"<current_decision_table\b[^>]*>.*?</current_decision_table>", re.DOTALL)


@dataclass(frozen=True)
class TableView:
    status: str
    snapshot: TableSnapshot | None
    error: str | None = None

    @classmethod
    def from_snapshot(cls, table: TableSnapshot) -> TableView:
        return cls("verified_empty" if not table.rows else "verified_current", table)


def render(view: TableView) -> str:
    if view.status not in {"verified_empty", "verified_current"} or view.snapshot is None:
        raise RuntimeError(view.error or "决策表读取未验证")
    table = view.snapshot
    rows = json.dumps([row.to_dict() for row in table.rows], ensure_ascii=False, sort_keys=True)
    rows = rows.replace("<", "\\u003c").replace(">", "\\u003e")
    return (
        f'{_START}revision="{table.revision}" content_hash="{table.content_hash}" status="{view.status}">\n'
        f"{rows}\n{_END}\n"
        "本段是此模型调用唯一的当前有效决策表。历史版本和待审批候选均不是当前决策。"
        "关键动作须遵守当前表；有权修改者可通过受审计改表流程删除、降级或替换旧决策。"
    )


def bind_model_request(request: ModelRequest, view: TableView) -> ModelRequest:
    current = render(view)
    original = request.system_message.text if request.system_message else ""
    original = _OLD_TABLE.sub("", _OLD_CURRENT.sub("", original))
    if _START in original or _END in original:
        raise RuntimeError("基础系统消息存在无法验证的旧表片段")
    messages = []
    for message in request.messages:
        if isinstance(message, SystemMessage) and (
            message.id == "decision-table" or "<decision_table " in message.text or _START in message.text
        ):
            continue
        if isinstance(message.content, str) and (_START in message.content or _END in message.content):
            message = message.model_copy(update={"content": message.content.replace(
                "current_decision_table", "historical_decision_table",
            )})
        messages.append(message)
    system = SystemMessage(content=f"{original}\n\n{current}" if original else current)
    bound = request.override(messages=messages, system_message=system)
    visible = [bound.system_message, *bound.messages]
    starts = sum(str(message.content).count(_START) for message in visible if message is not None)
    ends = sum(str(message.content).count(_END) for message in visible if message is not None)
    if starts != 1 or ends != 1:
        raise RuntimeError("模型请求中的当前决策表数量不为一")
    if f'revision="{view.snapshot.revision}" content_hash="{view.snapshot.content_hash}"' not in system.text:
        raise RuntimeError("模型请求中的决策表修订不匹配")
    return bound
