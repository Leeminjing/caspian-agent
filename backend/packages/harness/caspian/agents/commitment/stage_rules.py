"""
本文件对外提供九阶段指令、结果校验、路径片段校验和 Context7 结果解析函数。

输入:
    当前阶段编号、Supervisor messages、WorkerOutput 及 Context7 工具返回内容。

输出:
    TaskEnvelope — 根据阶段规则生成的下一阶段任务信封。
    str | None — 阶段结果的校验错误；None 表示结构与前置约束通过。
    规范化结果 — 优先级、版本证据、冲突状态和参考输入判断结果。

具体工作流:
    (1) 根据阶段编号选择指令、验收条件和执行时限。
    (2) 解析模型或 Context7 返回的结构化内容。
    (3) 从 Supervisor ToolMessage 读取前序结果并校验阶段一致性。
    (4) 输出 Supervisor 决定推进、重试或人工介入所需的确定性判断。

示例:
    error = _validate_stage_result(3, result, envelope.context)
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

def _context7_version_evidence(
    result: Any,
    version: str,
) -> str | None:
    for line in _context7_text(result).splitlines():
        if version in line and (
            "version" in line.lower()
            or "latest stable" in line.lower()
        ):
            return line.strip()[:1000]
    return None

def _normalize_stage_three_result(
    output: WorkerOutput,
    requirements: list[str],
) -> WorkerOutput:
    raw_items = output.result.get("requirements", [])
    priorities = {
        "1": 1,
        "low": 1,
        "optional": 1,
        "2": 2,
        "medium": 2,
        "negotiable": 2,
        "3": 3,
        "high": 3,
        "must": 3,
    }

    def priority_at(index: int, requirement: str) -> int:
        raw = (
            raw_items[index].get("priority")
            if index < len(raw_items) and isinstance(raw_items[index], dict)
            else None
        )
        if type(raw) is int and raw in {1, 2, 3}:
            return raw
        mapped = priorities.get(str(raw).strip().lower())
        if mapped:
            return mapped
        if any(word in requirement for word in ("可选", "最好")):
            return 1
        if any(word in requirement for word in ("可协商", "可以", "尽量")):
            return 2
        return 3

    return output.model_copy(
        update={"result": {
            "requirements": [
                {
                    "requirement": requirement,
                    "priority": priority_at(index, requirement),
                }
                for index, requirement in enumerate(requirements)
            ]
        }}
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
        requirements = result.get("requirements")
        if not isinstance(requirements, list) or any(
            not isinstance(item, dict)
            or not str(item.get("requirement") or item.get("text") or "").strip()
            or type(item.get("priority")) is not int
            or item["priority"] not in {1, 2, 3}
            for item in requirements
        ):
            return "阶段3必须返回 requirements 列表，priority 仅允许1、2、3"
        expected = _stage_three_requirements(messages or [])
        actual = [
            str(item.get("requirement") or item.get("text")).strip()
            for item in requirements
        ]
        if actual != expected:
            return "阶段3只能逐字、逐项沿用阶段2仍需完成的 requirements"
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
