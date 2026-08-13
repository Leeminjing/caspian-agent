"""
本文件对外提供九阶段指令、结果校验、路径片段校验、Context7 结果解析和等级表仲裁函数。

输入:
    当前阶段编号、Supervisor messages、WorkerOutput、Context7 工具返回内容及决策等级表。

输出:
    TaskEnvelope — 根据阶段规则生成的下一阶段任务信封。
    str | None — 阶段结果的校验错误；None 表示结构与前置约束通过。
    规范化结果 — 优先级、版本证据、冲突状态和参考输入判断结果。
    仲裁结果 — 新要求与已批准决策的降级冲突错误列表、升级冲突列表。

具体工作流:
    (1) 根据阶段编号选择指令、验收条件和执行时限。
    (2) 解析模型或 Context7 返回的结构化内容。
    (3) 从 Supervisor ToolMessage 读取前序结果，按稳定 requirement ID 关联阶段三优先级并校验一致性。
    (4) 对阶段2记录的等级表冲突做机械等级比较：降级冲突返回拒绝错误；
        升级/未声明冲突返回升级列表，由阶段3结果携带进入人工确认。
    (5) 输出 Supervisor 决定推进、重试或人工介入所需的确定性判断。

示例:
    error = _validate_stage_result(3, result, envelope.context)
    downgrades = _compare_table_conflicts(conflicts, rows, stage_three)
"""

import json
import re
from typing import Any

from langchain.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.messages import BaseMessage
from pydantic import BaseModel

from caspian.agents.commitment.schemas import (
    ReviewOutput,
    StageFiveResult,
    StageFourResult,
    StageTwoResult,
    TaskEnvelope,
    WorkerOutput,
)

_HUMAN_REVIEW_STAGES = frozenset({3, 5, 6, 7})

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")

_MAX_REVIEW_ATTEMPTS = 3

_STAGE_TIMEOUT_SECONDS = 600

_KNOWLEDGE_STAGE_TIMEOUT_SECONDS = 900

_LATEST_STABLE_VERSION = "latest-stable"

_STAGE_INSTRUCTIONS: dict[int, tuple[str, list[str]]] = {
    1: ("明确用户的单一主目标，保留边界和预期结果。", ["目标清晰", "不引入用户未提出的目标"]),
    2: (
        "汇总全部要求并核验技术兼容性。逐项识别应用类型、UI载体、运行平台和宿主模型；"
        "Web、桌面、移动、CMS模块与独立应用不得因同属一种语言或运行时而判为兼容。",
        [
            "要求完整",
            "返回requirements、compatibility_checks和conflicts",
            "每项技术有verified、conflict或unresolved状态",
            "冲突或无法核实的组合不得写成兼容",
        ],
    ),
    3: (
        "基于第二步已经明确且无未决矛盾的要求集合，给每条要求分配1、2、3三档优先级；"
        "3=必须，2=可协商，1=可选。",
        [
            "每条要求有等级且只使用1到3",
            "不得重新解释或夹带第二步的矛盾处理",
        ],
    ),
    4: (
        "解析用户引用的文件与网址。文件必须与<current_uploads>中的精确文件名核对；"
        "文件名不完整时列出上传文件候选并等待人工确认。用户提到网站、文档或项目但未给URL时，"
        "必须先调用find_reference_urls查找候选URL，再等待人工确认。",
        [
            "返回files和urls",
            "完整文件名标记matched，简称候选标记proposed，找不到标记unresolved",
            "用户给出的完整URL标记provided，搜索候选标记proposed，找不到标记unresolved",
            "不得把未确认候选写成已确认输入",
        ],
    ),
    5: ("识别涉及技术，对比项目当前版本与 Context7 候选最新稳定版。", ["每项技术有精确版本或latest-stable策略", "不得猜测版本"]),
    6: ("按已批准版本调用 Context7 获取官方技术知识。", ["每项知识含技术、版本、官方来源和正文", "不得使用非官方来源"]),
    7: (
        "把已批准的阶段结果组装为完整 Markdown 任务合同。",
        [
            "返回result.contract_markdown",
            "合同包含九步已有结论",
            "合同可直接指导执行",
        ],
    ),
    8: ("产出最终 task_contract。", ["内容与磁盘合同一致"]),
    9: ("准备 lead agent 的最终合同消息。", ["只包含合同和理论基础"]),
}

def _stage_timeout(stage: int) -> int:
    return (
        _KNOWLEDGE_STAGE_TIMEOUT_SECONDS
        if stage in {5, 6}
        else _STAGE_TIMEOUT_SECONDS
    )

def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)

def _safe_segment(value: str, label: str) -> str:
    if value in {".", ".."} or not value or not _SAFE_SEGMENT.fullmatch(value):
        raise ValueError(f"{label} 只允许字母、数字、点、下划线和连字符")
    return value

def _slug_segment(value: str, label: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return _safe_segment(slug, label)

def _stage_envelope(stage: int, state: dict[str, Any], feedback: str = "") -> TaskEnvelope:
    instruction, criteria = _STAGE_INSTRUCTIONS[stage]
    context: dict[str, Any] = {}
    if source_text := state.get("source_text"):
        context["source_text"] = source_text
    if uploads_tag := state.get("uploads_tag"):
        context["current_uploads"] = uploads_tag
    if decision_table := state.get("decision_table"):
        context["decision_table"] = decision_table
        rows = decision_table.get("rows", [])
        if rows:
            context["decision_table_rows"] = rows
    if feedback:
        context["human_feedback"] = feedback
    return TaskEnvelope(
        stage=stage,
        instruction=instruction,
        context=context,
        acceptance_criteria=criteria,
    )

def _extract_structured(result: dict[str, Any], schema: type[BaseModel]) -> BaseModel:
    value = result.get("structured_response")
    if isinstance(value, schema):
        return value
    if value is not None:
        return schema.model_validate(value)
    for message in reversed(result.get("messages", [])):
        if not isinstance(message, AIMessage):
            continue
        content = message.content
        if isinstance(content, list):
            content = "\n".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict)
            )
        text = str(content).strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1)
        try:
            return schema.model_validate_json(text)
        except ValueError:
            if schema is not ReviewOutput:
                raise
            # 宽容解析链：尾随垃圾 → 截断 JSON → 正则回退
            try:
                import json as _json

                value, _ = _json.JSONDecoder().raw_decode(text)
                return ReviewOutput.model_validate(value)
            except Exception:
                pass
            try:
                from pydantic_core import from_json

                value = from_json(text, allow_partial=True)
                return ReviewOutput.model_validate(value)
            except Exception:
                pass
            approved = re.search(
                r'"approved"\s*:\s*(true|false)',
                text,
                re.IGNORECASE,
            )
            feedback = re.search(
                r'"feedback"\s*:\s*"(.*)"\s*}\s*$',
                text,
                re.DOTALL,
            )
            if not approved or not feedback:
                raise
            return ReviewOutput(
                approved=approved.group(1).lower() == "true",
                feedback=feedback.group(1),
            )
    raise ValueError(f"模型未返回 {schema.__name__} JSON")

def _context7_text(result: Any) -> str:
    if isinstance(result, list):
        return "\n".join(
            str(item.get("text", ""))
            for item in result
            if isinstance(item, dict)
        )
    return str(result)

def _context7_library_id(result: Any) -> str | None:
    match = re.search(
        r"Context7-compatible library ID:\s*(/\S+)",
        _context7_text(result),
    )
    return match.group(1).strip() if match else None

def _context7_stable_version(result: Any) -> str | None:
    text = _context7_text(result)
    patterns = (
        r"latest stable version(?:\s+of\s+[^.\n]+)?\s+(?:is|as)\s+",
        r"latest stable version\s*:\s*",
        r"latest version\s*:\s*",
        r"current latest version(?:\s+of\s+[^.\n]+)?\s+is\s+",
    )
    version_pattern = r"`?[vV]?(\d+\.\d+(?:\.\d+)?)`?(?![-0-9A-Za-z])"
    for prefix in patterns:
        match = re.search(prefix + version_pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None

def _context7_candidate_version(
    result: Any,
    library_id: str,
) -> str | None:
    text = _context7_text(result)
    block = next(
        (
            item
            for item in re.split(r"\n-+\n", text)
            if f"Context7-compatible library ID: {library_id}" in item
        ),
        "",
    )
    versions = re.search(
        r"^\s*-?\s*Versions:\s*(.+)$",
        block,
        re.MULTILINE,
    )
    if not versions:
        return None
    candidates = re.findall(
        r"(?<![0-9A-Za-z])v?(\d+\.\d+(?:\.\d+)?)(?![-0-9A-Za-z.])",
        versions.group(1),
    )
    return max(
        candidates,
        key=lambda value: tuple(int(part) for part in value.split(".")),
        default=None,
    )

_POLLUTED_VERSION_PATTERN: re.Pattern = re.compile(
    r"__branch__[A-Za-z0-9._-]+"
    r"|\bv?\d[\d.]*-(?:canary|beta|rc|nightly|alpha)[\d.]*\b",
    re.IGNORECASE,
)


def _evidence_snippet(line: str, version: str) -> str | None:
    """截取版本附近短片段并清洗污染 token（受保护 helper）。

    输入:
        line: str — Context7 返回的原始行
        version: str — 目标版本号

    输出:
        str | None — 目标版本附近 ±60 字符的清洗片段；清洗后为空返回 None
    """
    idx = line.find(version)
    if idx == -1:
        return None
    start = max(0, idx - 60)
    end = min(len(line), idx + len(version) + 60)
    snippet = line[start:end]
    # 移除分支名与预发布版本 token，与 _context7_candidate_version 同一过滤标准
    snippet = _POLLUTED_VERSION_PATTERN.sub("", snippet)
    snippet = re.sub(r"\s{2,}", " ", snippet).strip(" ,|")
    return snippet or None


def _context7_version_evidence(
    result: Any,
    version: str,
) -> str | None:
    for line in _context7_text(result).splitlines():
        if version in line and (
            "version" in line.lower()
            or "latest stable" in line.lower()
        ):
            snippet = _evidence_snippet(line, version)
            if snippet:
                return snippet[:1000]
    return None

def _normalize_stage_three_result(
    output: WorkerOutput,
    requirements: list[str],
) -> WorkerOutput:
    assignments = output.result.get("priority_assignments")
    if not isinstance(assignments, list):
        return output.model_copy(
            update={
                "result": {
                    "priority_assignments": assignments,
                    "worker_result": output.result,
                }
            }
        )

    expected_ids = [f"R{index}" for index in range(1, len(requirements) + 1)]
    if len(assignments) != len(expected_ids):
        return output

    by_id: dict[str, int] = {}
    for item in assignments:
        if not isinstance(item, dict):
            return output
        requirement_id = item.get("requirement_id")
        priority = item.get("priority")
        if (
            requirement_id not in expected_ids
            or requirement_id in by_id
            or type(priority) is not int
            or priority not in {1, 2, 3}
        ):
            return output
        by_id[requirement_id] = priority

    normalized = [
        {"requirement": requirement, "priority": by_id[requirement_id]}
        for requirement_id, requirement in zip(
            expected_ids,
            requirements,
            strict=True,
        )
    ]
    priority_map = "、".join(
        f"R{index}={item['priority']}"
        for index, item in enumerate(normalized, start=1)
    )

    return output.model_copy(
        update={
            "result": {"requirements": normalized},
            "reasoning_summary": (
                f"阶段3已逐项、逐字沿用阶段2的{len(normalized)}条保留要求并首次分配优先级："
                f"{priority_map}。已放弃要求未进入等级列表；"
                "result.requirements 是最终等级的唯一事实来源。"
            ),
        }
    )

def _message_payload(message: BaseMessage) -> dict[str, Any] | None:
    if not isinstance(message, ToolMessage):
        return None
    try:
        value = json.loads(str(message.content))
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None

def _stage_result(messages: list[BaseMessage], stage: int) -> dict[str, Any]:
    for message in reversed(messages):
        payload = _message_payload(message)
        if payload and payload.get("stage") == stage:
            result = payload.get("result")
            if isinstance(result, dict):
                return result
    return {}

def _source_text(messages: list[BaseMessage]) -> str:
    return "\n\n".join(
        str(message.content)
        for message in messages
        if isinstance(message, HumanMessage)
    )

def _stage_three_requirements(
    messages: list[BaseMessage],
) -> list[str]:
    stage_two = _stage_result(messages, 2)
    requirements = stage_two.get("requirements", [])
    discarded = set(stage_two.get("discarded_requirements", []))
    if not isinstance(requirements, list):
        return []
    return [
        requirement
        for requirement in requirements
        if isinstance(requirement, str) and requirement not in discarded
    ]

def _validate_stage_result(
    stage: int,
    result: dict[str, Any],
    messages: list[BaseMessage] | None = None,
    decision_table_rows: list[Any] | None = None,
) -> str | None:
    if not result:
        return "结果为空"
    if stage == 2:
        try:
            parsed = StageTwoResult.model_validate(result)
        except Exception as exc:
            return f"阶段2结果不符合 StageTwoResult: {exc}"
        if set(parsed.requirements) & set(parsed.discarded_requirements):
            return "阶段2已放弃要求必须移出 requirements"
        if any(
            item.status in {"conflict", "unresolved"}
            for item in parsed.compatibility_checks
        ) and not parsed.conflicts:
            return "阶段2存在 conflict 或 unresolved 时 conflicts 不得为空"
    if stage == 4:
        try:
            parsed = StageFourResult.model_validate(result)
        except Exception as exc:
            return f"阶段4结果不符合 StageFourResult: {exc}"
        for item in parsed.files:
            if item.status == "matched" and not item.uploaded_filename:
                return "阶段4 matched 文件必须包含 uploaded_filename"
            if item.status == "proposed" and not item.candidates:
                return "阶段4 proposed 文件必须包含 candidates"
        for item in parsed.urls:
            if item.status == "provided" and (
                not item.url or not item.url.startswith(("http://", "https://"))
            ):
                return "阶段4 provided URL 必须包含完整 http(s) URL"
            if item.status == "proposed" and not (
                (item.url and item.url.startswith(("http://", "https://")))
                or (
                    item.candidates
                    and all(
                        url.startswith(("http://", "https://"))
                        for url in item.candidates
                    )
                )
            ):
                return "阶段4 proposed URL 必须包含完整候选 URL"
    if stage == 3:
        if "priority_assignments" in result:
            assignments = result.get("priority_assignments")
            if not isinstance(assignments, list):
                return (
                    "阶段3字段 result.priority_assignments 实际值不是列表；"
                    "必须为每个稳定 requirement_id 明确返回 priority。"
                )
            expected_ids = [
                f"R{index}"
                for index in range(
                    1,
                    len(_stage_three_requirements(messages or [])) + 1,
                )
            ]
            seen: set[str] = set()
            for index, item in enumerate(assignments):
                if not isinstance(item, dict):
                    return (
                        f"阶段3字段 result.priority_assignments[{index}] 实际值为 {item!r}；"
                        "必须是包含 requirement_id 和 priority 的对象。"
                    )
                requirement_id = item.get("requirement_id")
                if requirement_id not in expected_ids:
                    return (
                        f"阶段3字段 result.priority_assignments[{index}].requirement_id "
                        f"实际值为 {requirement_id!r}；属于未知 requirement_id。"
                    )
                if requirement_id in seen:
                    return (
                        f"阶段3字段 result.priority_assignments[{index}].requirement_id "
                        f"实际值为 {requirement_id!r}；同一 requirement_id 不得重复。"
                    )
                seen.add(requirement_id)
                priority = item.get("priority")
                if type(priority) is not int or priority not in {1, 2, 3}:
                    return (
                        f"阶段3字段 result.priority_assignments[{index}].priority "
                        f"实际值为 {priority!r}；必须明确填写整数 1、2、3 之一。"
                    )
            missing = [item for item in expected_ids if item not in seen]
            if missing:
                return (
                    "阶段3字段 result.priority_assignments 缺少稳定 requirement_id："
                    f"{', '.join(missing)}。"
                )
            return (
                "阶段3 priority_assignments 已通过校验但尚未关联为最终 requirements；"
                "必须先使用阶段2不可变原文完成确定性关联。"
            )
        requirements = result.get("requirements")
        if not isinstance(requirements, list):
            return "阶段3必须返回 requirements 列表"
        for index, item in enumerate(requirements, start=1):
            if not isinstance(item, dict) or not str(
                item.get("requirement") or item.get("text") or ""
            ).strip():
                return f"阶段3第 {index} 条要求缺少 requirement 文本"
            if (
                type(item.get("priority")) is not int
                or item["priority"] not in {1, 2, 3}
            ):
                return (
                    f"阶段3第 {index} 条要求缺少有效的 priority"
                    "（仅允许 1、2、3）"
                )
        expected = _stage_three_requirements(messages or [])
        actual = [
            str(item.get("requirement") or item.get("text")).strip()
            for item in requirements
        ]
        if actual != expected:
            return "阶段3只能逐字、逐项沿用阶段2仍需完成的 requirements"
        if decision_table_rows:
            conflicts = _stage_result(messages or [], 2).get("table_conflicts", [])
            errors = _compare_table_conflicts(
                conflicts,
                decision_table_rows,
                requirements,
            )
            if errors:
                return "; ".join(errors)
    if stage == 5:
        try:
            StageFiveResult.model_validate(result)
        except Exception as exc:
            return f"阶段5结果不符合 StageFiveResult: {exc}"
    if stage == 6:
        knowledge = result.get("knowledge")
        if not isinstance(knowledge, list) or not knowledge:
            return "阶段6必须返回非空 knowledge 列表"
    if stage == 7 and (
        not isinstance(result.get("contract_markdown"), str)
        or not result["contract_markdown"].strip()
    ):
        return "阶段7必须返回 contract_markdown"
    return None

def _table_priority_by_requirement(rows: Any) -> dict[str, int]:
    """提取等级表条目的 requirement → priority 映射（受保护 helper）。

    输入:
        rows: Any — 等级表行列表（dict 或 DecisionRow 均可）

    输出:
        dict[str, int] — 表内 requirement（strip 后）到优先级的映射
    """
    result: dict[str, int] = {}
    for row in rows or []:
        requirement = getattr(row, "requirement", None) if not isinstance(row, dict) else row.get("requirement")
        priority = getattr(row, "priority", None) if not isinstance(row, dict) else row.get("priority")
        if isinstance(requirement, str) and isinstance(priority, int):
            result[requirement.strip()] = priority
    return result


def _classify_table_conflicts(
    conflicts: list[Any],
    table_priorities: dict[str, int],
    stage_three_requirements: list[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """机械比较等级表冲突的等级大小（受保护 helper）。

    输入:
        conflicts: list[Any] — 阶段2记录的 table_conflicts（requirement/table_requirement/table_priority）
        table_priorities: dict[str, int] — 表内 requirement → priority 映射
        stage_three_requirements: list[Any] — 阶段3结果 requirements（含逐条 priority）

    输出:
        tuple[list[dict], list[dict]] — (升级/未声明冲突列表, 降级冲突列表)

    具体工作流:
        (1) 从阶段3结果构建新要求 → 新等级映射。
        (2) 对每个冲突取新等级与表内等级做 int 比较。
        (3) 新等级缺失（未声明）或大于等于表内等级 → 升级列表。
        (4) 新等级低于表内等级 → 降级列表。
    """
    new_priorities: dict[str, int] = {}
    for item in stage_three_requirements or []:
        if not isinstance(item, dict):
            continue
        requirement = str(item.get("requirement") or item.get("text") or "").strip()
        priority = item.get("priority")
        if requirement and isinstance(priority, int):
            new_priorities[requirement] = priority

    escalations: list[dict[str, Any]] = []
    downgrades: list[dict[str, Any]] = []
    for conflict in conflicts or []:
        if not isinstance(conflict, dict):
            continue
        requirement = str(conflict.get("requirement", "")).strip()
        table_requirement = str(conflict.get("table_requirement", "")).strip()
        table_priority = conflict.get("table_priority")
        if not isinstance(table_priority, int):
            continue
        # 冲突引用的表内条目不在等级表中 → 视为无效冲突，忽略
        if table_requirement not in table_priorities:
            continue
        new_priority = new_priorities.get(requirement)
        if new_priority is None or new_priority >= table_priority:
            escalations.append(conflict)
        else:
            downgrades.append(conflict)
    return escalations, downgrades


def _compare_table_conflicts(
    conflicts: list[Any],
    decision_table_rows: list[Any],
    stage_three_requirements: list[Any],
) -> list[str]:
    """机械等级比较等级表冲突，返回降级冲突的拒绝错误列表。

    输入:
        conflicts: list[Any] — 阶段2记录的 table_conflicts
        decision_table_rows: list[Any] — 等级表行（用于校验冲突引用与提取表内等级）
        stage_three_requirements: list[Any] — 阶段3结果 requirements（新要求等级）

    输出:
        list[str] — 降级冲突的拒绝错误；空列表表示无降级冲突

    示例:
        errors = _compare_table_conflicts(conflicts, rows, stage_three_requirements)
        # → ["新要求 'X' 与已批准决策 'Y' 冲突，新等级 1 低于表内等级 3，降级决策被拒绝"]
    """
    table_priorities = _table_priority_by_requirement(decision_table_rows)
    _, downgrades = _classify_table_conflicts(
        conflicts, table_priorities, stage_three_requirements
    )
    errors: list[str] = []
    for conflict in downgrades:
        requirement = conflict.get("requirement", "")
        table_requirement = conflict.get("table_requirement", "")
        table_priority = conflict.get("table_priority", "?")
        errors.append(
            f"新要求 '{requirement}' 与已批准决策 '{table_requirement}' 冲突且等级更低"
            f"（表内等级 {table_priority}），降级决策被拒绝"
        )
    return errors


def _merge_table_escalations(
    result: dict[str, Any],
    conflicts: list[Any],
    decision_table_rows: list[Any],
    stage_three_requirements: list[Any],
) -> dict[str, Any]:
    """将升级/未声明的等级表冲突合并进阶段结果（受保护 helper）。

    输入:
        result: dict — 阶段结果（阶段3 requirements）
        conflicts: list[Any] — 阶段2记录的 table_conflicts
        decision_table_rows: list[Any] — 等级表行
        stage_three_requirements: list[Any] — 阶段3结果 requirements

    输出:
        dict — 含 table_escalations 字段的副本（无升级冲突时原样返回）
    """
    table_priorities = _table_priority_by_requirement(decision_table_rows)
    escalations, _ = _classify_table_conflicts(
        conflicts, table_priorities, stage_three_requirements
    )
    if not escalations:
        return result
    merged = dict(result)
    merged["table_escalations"] = escalations
    return merged


def _contains_unresolved_versions(result: Any) -> bool:
    if not isinstance(result, dict):
        return True
    technologies = result.get("technologies")
    if not isinstance(technologies, list) or not technologies:
        return True
    return any(
        not isinstance(item, dict)
        or not item.get("version")
        or str(item.get("version")).lower() == "unresolved"
        for item in technologies
    )

def _has_open_conflicts(result: Any) -> bool:
    if not isinstance(result, dict):
        return True
    checks = result.get("compatibility_checks", [])
    conflicts = result.get("conflicts", [])
    return any(
        isinstance(item, dict)
        and item.get("status") in {"conflict", "unresolved"}
        for item in checks
    ) or any(
        not isinstance(item, dict) or item.get("status") != "resolved"
        for item in conflicts
    )

def _stage_four_needs_review(result: Any) -> bool:
    if not isinstance(result, dict):
        return True
    return any(
        isinstance(item, dict) and item.get("status") in {"proposed", "unresolved"}
        for key in ("files", "urls")
        for item in result.get(key, [])
    )

def _stage_four_has_unresolved(result: Any) -> bool:
    if not isinstance(result, dict):
        return True
    return any(
        not isinstance(item, dict) or item.get("status") == "unresolved"
        for key in ("files", "urls")
        for item in result.get(key, [])
    )

def _filter_stage_four_result(
    output: WorkerOutput,
    source_text: str,
) -> WorkerOutput:
    intent = re.compile(
        r"参考|参照|依据|文档|网址|网站|链接|官网|"
        r"reference|refer(?:\s+to)?|according\s+to|"
        r"docs?|documentation|website|url|link",
        re.IGNORECASE,
    )

    def explicitly_referenced(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        url = str(item.get("url") or "")
        if url and url in source_text:
            return True
        mention = str(item.get("mention") or "").strip()
        if not mention:
            return False
        for match in re.finditer(re.escape(mention), source_text, re.IGNORECASE):
            nearby = source_text[
                max(0, match.start() - 24) : min(
                    len(source_text),
                    match.end() + 24,
                )
            ]
            if intent.search(nearby):
                return True
        return False

    result = dict(output.result)
    result["urls"] = [
        item
        for item in result.get("urls", [])
        if explicitly_referenced(item)
    ]
    return output.model_copy(update={"result": result})

def _must_revise(stage: int, draft: Any) -> bool:
    return (
        isinstance(draft, dict) and draft.get("status") == "reviewed_failed"
    ) or (stage == 2 and _has_open_conflicts(draft)) or (
        stage == 4 and _stage_four_has_unresolved(draft)
    )
