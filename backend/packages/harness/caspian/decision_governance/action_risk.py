"""本文件对外提供动作风险结论校验、上下文指纹及保守影响线索。

输入为待执行动作、实际参数、当前可见工作消息与模型 JSON；输出为稳定指纹及 low/high/uncertain 风险结论。
工作流排除闸门自身消息，再校验结论绑定与具体理由；复合命令按完整参数检查，确定性线索仅能把 low 升为 high。
示例：`risk = parse_risk(raw, binding, {"command": "pytest"})`。
"""

from __future__ import annotations

import json
import re
from typing import Any

from caspian.decision_governance.operations import digest
from caspian.decision_governance.review_protocol import ActionBinding


def context_fingerprint(messages: list[Any], context: dict) -> str:
    visible = []
    for message in messages:
        value = message.model_dump(mode="json") if hasattr(message, "model_dump") else message
        content = value.get("content", "") if isinstance(value, dict) else str(value)
        if isinstance(content, str) and content.startswith("[动作前复核]"):
            continue
        visible.append(value)
    relevant = {key: context.get(key) for key in ("working_directory", "workspace_root", "is_subagent") if key in context}
    return digest({"messages": visible, "context": relevant})


def impact_hint(action_name: str, args: dict) -> str | None:
    command = args.get("command") if isinstance(args, dict) else None
    if action_name in {"read_file_tool", "web_search", "web_search_tool", "web_fetch", "web_fetch_tool"}:
        return None
    if action_name == "write_file_tool":
        path = str(args.get("path", "")).lower().replace("\\", "/")
        if re.search(r"(^|/)(dockerfile|docker-compose\.ya?ml|package\.json|pyproject\.toml|requirements\.txt|config\.ya?ml)$", path) or "/migrations/" in path:
            return "核心依赖、部署、环境或数据库配置变更"
        return None
    if not isinstance(command, str):
        return None
    text = command.strip().lower()
    simple_read = re.match(r"^(cat|type|rg|grep|findstr|get-content|pytest|python -m pytest|git status|git diff)\b", text)
    if simple_read and not re.search(r"[;&|`\r\n]|\$\(", text):
        return None
    patterns = (
        (r"\b(alembic upgrade|terraform apply|kubectl apply|helm install|deploy|docker run|docker compose up)\b", "持久资源或部署变更"),
        (r"\b(drop database|drop table|rm -rf|remove-item.*-recurse|delete.*(database|bucket|cluster))\b", "大范围删除或数据库变更"),
        (r"\b(pip install|uv add|npm install|pnpm add|poetry add|apt install)\b", "核心依赖或环境变更"),
    )
    for pattern, reason in patterns:
        if re.search(pattern, text):
            return reason
    return None


def parse_risk(raw: str, binding: ActionBinding, args: dict) -> dict:
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
    value = json.loads(fenced.group(1) if fenced else raw)
    if not isinstance(value, dict):
        raise ValueError("风险结论必须是 JSON 对象")
    expected = {"action_name": binding.action_name, "args_hash": binding.args_hash, "context_hash": binding.context_hash}
    if any(value.get(key) != item for key, item in expected.items()):
        raise ValueError("风险结论未绑定实际动作、参数及上下文")
    if value.get("risk") not in {"low", "high", "uncertain"}:
        raise ValueError("风险等级无效")
    reason = value.get("reason")
    if not isinstance(reason, str) or len(reason.strip()) < 8:
        raise ValueError("风险理由不具体")
    if not isinstance(value.get("examined_args"), dict) or digest(value["examined_args"]) != binding.args_hash:
        raise ValueError("风险结论未检查实际参数")
    if not any(str(item) in reason for item in args.values() if isinstance(item, (str, int)) and str(item).strip()):
        raise ValueError("风险理由未指出具体参数")
    hint = impact_hint(binding.action_name, args)
    if hint and value["risk"] == "low":
        value = {**value, "risk": "high", "reason": f"{hint}；{reason}"}
    return value
