"""本文件对外提供待执行动作绑定和结构化复核结论验证。

输入为工具名、实际参数、主体、表修订及模型返回；输出为稳定绑定摘要和可放行的结论。
工作流检查动作、检查过的具体参数、修订及相关行精确匹配；空泛理由、冲突或不确定结论不能直接 keep。
示例：`conclusion = parse_conclusion(raw, binding, {"row-1"}, {"command": "echo hi"})`。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from caspian.decision_governance.operations import digest


@dataclass(frozen=True)
class ActionBinding:
    action_name: str
    args_hash: str
    actor_id: str
    table_revision: int

    @classmethod
    def create(cls, action_name: str, args: dict, actor_id: str, table_revision: int):
        return cls(action_name, digest(args), actor_id, table_revision)


def _argument_values(args: Any) -> list[str]:
    if isinstance(args, dict):
        return [value for item in args.values() for value in _argument_values(item)]
    if isinstance(args, list):
        return [value for item in args for value in _argument_values(item)]
    if isinstance(args, (str, int, float, bool)):
        value = str(args).strip()
        return [value[:120]] if value else []
    return []


def parse_conclusion(raw: str, binding: ActionBinding, row_ids: set[str], args: dict) -> dict[str, Any]:
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
    value = json.loads(fenced.group(1) if fenced else raw)
    if not isinstance(value, dict):
        raise ValueError("复核结论必须是 JSON 对象")
    if value.get("action_name") != binding.action_name or value.get("args_hash") != binding.args_hash:
        raise ValueError("复核结论未绑定待执行动作及参数")
    if value.get("actor_id") != binding.actor_id:
        raise ValueError("复核结论未绑定动作主体")
    if value.get("table_revision") != binding.table_revision:
        raise ValueError("复核结论使用了其他决策表修订")
    if not isinstance(value.get("examined_args"), dict) or digest(value["examined_args"]) != binding.args_hash:
        raise ValueError("复核结论未展示实际检查的动作参数")
    related = value.get("related_rows")
    if not isinstance(related, list) or any(not isinstance(item, str) or item not in row_ids for item in related):
        raise ValueError("复核涉及的决策行无效")
    if value.get("conflict") not in {"none", "conflict", "uncertain"}:
        raise ValueError("复核冲突判定无效")
    if value.get("decision") not in {"keep", "modify", "cancel", "ask_human"}:
        raise ValueError("复核动作结论无效")
    if not isinstance(value.get("reason"), str) or len(value["reason"].strip()) < 8:
        raise ValueError("复核缺少具体依据")
    if value["decision"] == "keep":
        targets = _argument_values(args) or [binding.action_name]
        if not any(target in value["reason"] for target in targets + related):
            raise ValueError("放行理由未指明具体动作参数或决策条目")
    if value["decision"] == "keep" and value["conflict"] != "none":
        raise ValueError("冲突或不确定动作不能直接放行")
    if value["decision"] == "modify" and not isinstance(value.get("replacement_args"), dict):
        raise ValueError("修改结论缺少替换参数")
    return value
