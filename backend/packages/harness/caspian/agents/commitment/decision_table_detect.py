"""
本文件对外提供决策等级表改写的冲突检测与等级裁决纯函数，作为改表事务的裁决层。

对外提供:
    detect_decision_table — 对候选新表与旧表执行语义冲突扫描 + 确定性等级裁决
    _semantic_conflicts — LLM 语义扫描，识别候选条目与旧表条目的语义冲突（受保护 helper）
    _adjudicate_conflicts — 确定性等级比较，返回需人工确认与应拒绝的条目（受保护 helper）

输入:
    candidate: list[DecisionRow] — 本次改表操作构造出的候选新表条目
    existing: list[DecisionRow] — 当前磁盘上的旧表条目
    model: BaseChatModel — 用于语义冲突扫描的模型实例

输出:
    detect_decision_table → DecisionTableVerdict —
        (pass_to_commit / require_confirm) 与冲突说明，供工具决定提交或中断

具体工作流:
    (1) 构造语义扫描输入（候选条目 vs 旧表条目），调用 LLM 识别语义冲突
    (2) 对语义冲突做确定性等级比较：候选等级高于或等于旧表对应条目 → 需人工确认；
        候选等级低于旧表对应条目 → 放弃新决策；未声明等级 → 需人工确认（安全默认）
    (3) 汇总为裁决结果

示例:
    verdict = await detect_decision_table(candidate, existing, model)
    if verdict.require_confirm:
        ...  # 中断待机
    else:
        ...  # 直接提交
"""

import logging
import re
from enum import Enum
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from caspian.agents.commitment.decision_table import DecisionRow
from caspian.agents.commitment.stage_rules import _classify_table_conflicts

logger = logging.getLogger(__name__)

_SEMANTIC_SCAN_PROMPT = """你是决策等级表语义冲突扫描器。给定一个待写入的"候选表"条目列表与当前"已有表"条目列表，判断候选表条目与已有表条目之间是否存在语义冲突。

判定规则：
1. 只判定语义冲突（覆盖措辞改写、技术替换、范围伸缩、含义相反等字面无关的冲突），不判字符串相同与否为冲突。
2. 只有候选条目与某已有条目针对同一事实给出明确相反或冲突含义，才标 relation="explicit"。
3. 疑似冲突但无法可靠确认的，标 relation="potential"，不得标 explicit。
4. 无冲突的条目不列入输出。
5. 输出必须是一个 JSON 对象，格式：
{"conflicts": [{"candidate": "<候选条目文本>", "existing": "<已有条目文本>", "relation": "explicit|potential", "explanation": "..."}]}"""


class DecisionTableConflict(BaseModel):
    """候选表条目与已有表条目之间的语义冲突边。"""

    candidate: str
    existing: str
    relation: Literal["explicit", "potential"] = "explicit"
    explanation: str = ""


class DecisionTableAction(str, Enum):
    """改表提案的处置动作。"""

    COMMIT = "commit"  # 无冲突或仅 potential（不阻断），直接落盘候选新表
    CONFIRM = "confirm"  # 任一明确冲突（升级/同级/降级/未声明），需中断用户二选一


class DecisionTableVerdict(BaseModel):
    """一次改表提案的裁决结果。"""

    action: DecisionTableAction = DecisionTableAction.COMMIT
    conflicts: list[DecisionTableConflict] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)

    @property
    def require_confirm(self) -> bool:
        return self.action == DecisionTableAction.CONFIRM


def _existing_priority_by_text(existing: list[DecisionRow]) -> dict[str, int]:
    """提取旧表条目文本 → 优先级的映射（受保护 helper）。"""
    result: dict[str, int] = {}
    for row in existing or []:
        result[row.requirement.strip()] = row.priority
    return result


def _candidate_priority_rows(candidate: list[DecisionRow]) -> list[dict]:
    """把候选表条目转为等级裁决所需的 {'requirement', 'priority'} 列表（受保护 helper）。"""
    return [
        {"requirement": row.requirement.strip(), "priority": row.priority}
        for row in candidate or []
    ]


def _adjudicate_conflicts(
    conflicts: list[DecisionTableConflict],
    existing: list[DecisionRow],
    candidate: list[DecisionRow],
) -> DecisionTableVerdict:
    """对语义冲突做确定性等级裁决（受保护 helper）。

    复用 stage_rules._classify_table_conflicts 的 int 单调比较判断冲突性质；
    存在任一 explicit 冲突（含降级/升级/同级/未声明）→ 需中断用户二选一；
    仅 potential 或无冲突 → 直接提交（不确定不阻断）。

    输入:
        conflicts: 语义冲突边列表
        existing / candidate: 旧表与候选表条目
    输出:
        DecisionTableVerdict — action 为 CONFIRM 表示需中断用户确认
    """
    if not conflicts:
        return DecisionTableVerdict(action=DecisionTableAction.COMMIT)

    table_priorities = _existing_priority_by_text(existing)
    # 构造 _classify_table_conflicts 期望的冲突结构：requirement/table_requirement/table_priority
    structured = [
        {
            "requirement": conflict.candidate,
            "table_requirement": conflict.existing,
            "table_priority": table_priorities.get(conflict.existing.strip()),
        }
        for conflict in conflicts
        if conflict.relation == "explicit"
        and conflict.existing.strip() in table_priorities
    ]
    escc, downgrades = _classify_table_conflicts(
        structured,
        table_priorities,
        _candidate_priority_rows(candidate),
    )

    # 无 explicit 冲突（仅 potential 或无结构化冲突）→ 不阻断，直接提交
    if not structured:
        reasons = [
            f"候选条目 '{c.existing}' 与已有条目存在冲突，但仅 potential（不确定不压制）"
            for c in conflicts
            if c.relation == "potential"
        ]
        return DecisionTableVerdict(
            action=DecisionTableAction.COMMIT, conflicts=conflicts, reasons=reasons
        )

    # 任一明确冲突（降级/升级/同级/未声明）→ 中断用户二选一
    reasons = [
        f"候选条目 '{conflict.candidate}' 与已有条目 '{conflict.existing}' 存在语义冲突"
        for conflict in conflicts
    ]
    for conflict in downgrades:
        reasons.append(
            f"候选条目 '{conflict['requirement']}' 等级低于旧表条目 '{conflict['table_requirement']}'（降级）"
        )
    for conflict in escc:
        reasons.append(
            f"候选条目 '{conflict.get('requirement')}' 与已有条目 '{conflict.get('table_requirement')}' 等级冲突（升级/同级/未声明）"
        )
    reasons.append("存在冲突，需用户二选一决定最终等级表")
    return DecisionTableVerdict(
        action=DecisionTableAction.CONFIRM, conflicts=conflicts, reasons=reasons
    )


def _parse_semantic_conflicts(raw: dict, known_existing: set[str]) -> list[DecisionTableConflict]:
    """解析 LLM 语义扫描输出，过滤未知条目（受保护 helper）。"""
    result: list[DecisionTableConflict] = []
    for item in raw.get("conflicts", []) or []:
        if not isinstance(item, dict):
            continue
        candidate = str(item.get("candidate", "") or "").strip()
        existing = str(item.get("existing", "") or "").strip()
        if not candidate or not existing or existing not in known_existing:
            continue
        relation = item.get("relation")
        if relation not in ("explicit", "potential"):
            relation = "explicit"
        result.append(
            DecisionTableConflict(
                candidate=candidate,
                existing=existing,
                relation=relation,
                explanation=str(item.get("explanation", "") or ""),
            )
        )
    return result


async def _semantic_conflicts(
    candidate: list[DecisionRow],
    existing: list[DecisionRow],
    model: BaseChatModel,
) -> list[DecisionTableConflict] | None:
    """LLM 语义扫描：识别候选条目与旧表条目的语义冲突（受保护 helper）。

    返回 None 表示扫描失败（调用方应转人工确认）。
    """
    if not candidate or not existing:
        return []

    payload = {
        "candidate": [row.requirement for row in candidate],
        "existing": [row.requirement for row in existing],
    }
    bound = model.bind(max_tokens=2048)
    known_existing = {row.requirement.strip() for row in existing}
    try:
        response = await bound.ainvoke(
            [
                SystemMessage(content=_SEMANTIC_SCAN_PROMPT),
                HumanMessage(content=str(payload)),
            ]
        )
        import json
        import re

        text = str(response.content)
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        raw = json.loads(fenced.group(1)) if fenced else json.loads(text)
        if not isinstance(raw, dict):
            return None
        return _parse_semantic_conflicts(raw, known_existing)
    except Exception as exc:
        logger.warning("决策等级表语义冲突扫描失败：%s", exc)
        # fail-closed：扫描失败返回 None，由调用方转人工确认，不再放行
        return None


def _normalize_text(text: str) -> str:
    """归一化条目文本：折叠所有空白并折叠大小写（受保护 helper）。"""
    return re.sub(r"\s+", "", text).strip().lower()


def _deterministic_collisions(
    candidate: list[DecisionRow],
    existing: list[DecisionRow],
) -> list[DecisionTableConflict]:
    """确定性底线：归一化后文本相同但原文不同的候选/既有条目判为冲突（不依赖 LLM，受保护 helper）。"""
    conflicts: list[DecisionTableConflict] = []
    by_norm: dict[str, DecisionRow] = {}
    for row in existing:
        by_norm[_normalize_text(row.requirement)] = row
    for crow in candidate:
        norm = _normalize_text(crow.requirement)
        erow = by_norm.get(norm)
        if erow is not None and crow.requirement.strip() != erow.requirement.strip():
            conflicts.append(
                DecisionTableConflict(
                    candidate=crow.requirement,
                    existing=erow.requirement,
                    relation="explicit",
                    explanation="归一化后文本相同（仅大小写/空白差异）",
                )
            )
    return conflicts


def _fail_closed_verdict(reason: str) -> DecisionTableVerdict:
    """构造 fail-closed 的 CONFIRM 裁决（受保护 helper）。"""
    return DecisionTableVerdict(
        action=DecisionTableAction.CONFIRM, conflicts=[], reasons=[reason]
    )


async def detect_decision_table(
    candidate: list[DecisionRow],
    existing: list[DecisionRow],
    model: BaseChatModel | None = None,
) -> DecisionTableVerdict:
    """对候选新表与旧表执行冲突检测与等级裁决。

    输入:
        candidate: 候选新表条目
        existing: 旧表条目（磁盘当前）
        model: 语义扫描所用模型；None 时内部创建

    输出:
        DecisionTableVerdict — action 为 CONFIRM 则工具应中断待机
    """
    if not existing:
        return _adjudicate_conflicts([], existing, candidate)

    # 确定性底线：归一化文本碰撞，无需 LLM
    deterministic = _deterministic_collisions(candidate, existing)
    if deterministic:
        return _adjudicate_conflicts(deterministic, existing, candidate)

    resolved_model = model
    if resolved_model is None:
        try:
            from caspian.models import create_chat_model

            resolved_model = create_chat_model()
        except Exception as exc:
            logger.warning("决策等级表语义扫描模型创建失败：%s", exc)
            resolved_model = None

    if resolved_model is None:
        return _fail_closed_verdict("语义冲突扫描不可用（模型创建失败），需人工确认")

    conflicts = await _semantic_conflicts(candidate, existing, resolved_model)
    if conflicts is None:
        return _fail_closed_verdict("语义冲突扫描失败，需人工确认")
    return _adjudicate_conflicts(conflicts, existing, candidate)


async def detect_decision_table_internal(
    rows: list[DecisionRow],
    model: BaseChatModel | None = None,
) -> DecisionTableVerdict:
    """对完整目标新表做内部语义自洽检测（编辑路径专用）。

    手工编辑提交的是完整目标表（替换旧表），冲突检测对象为**新表条目之间的语义自洽性**，
    而非"新表 vs 旧表"。复用 `_semantic_conflicts` 的双列表语义扫描，把新表同时当作
    candidate 与 existing（二者同一集合），从而识别条目两两之间的语义冲突；
    再用 `_adjudicate_conflicts` 判定是否需要中断。

    输入:
        rows: 完整目标新表条目
        model: 语义扫描所用模型；None 时懒创建

    输出:
        DecisionTableVerdict — action 为 CONFIRM 则编辑应中断待机
    """
    if not rows:
        return DecisionTableVerdict(action=DecisionTableAction.COMMIT)

    resolved_model = model
    if resolved_model is None:
        try:
            from caspian.models import create_chat_model

            resolved_model = create_chat_model()
        except Exception as exc:
            logger.warning("决策等级表内部自洽扫描模型创建失败：%s", exc)
            resolved_model = None

    if resolved_model is None:
        return _fail_closed_verdict("语义冲突扫描不可用（模型创建失败），需人工确认")

    conflicts = await _semantic_conflicts(rows, rows, resolved_model)
    if conflicts is None:
        return _fail_closed_verdict("语义冲突扫描失败，需人工确认")

    # 过滤自对与对称对（同一集合自比较会产生噪音）
    seen: set[frozenset] = set()
    filtered: list[DecisionTableConflict] = []
    for conflict in conflicts:
        if conflict.candidate.strip() == conflict.existing.strip():
            continue
        pair = frozenset((conflict.candidate, conflict.existing))
        if pair in seen:
            continue
        seen.add(pair)
        filtered.append(conflict)
    return _adjudicate_conflicts(filtered, rows, rows)
