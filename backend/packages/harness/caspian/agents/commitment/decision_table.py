"""
本文件对外提供决策等级表的组装、写入与读取函数，作为承诺层等级表持久化的唯一入口。

对外提供:
    Guard — 硬层守卫谓词（kind/target/operator/pattern/message），可机械匹配
    DecisionRow — 单条决策条目（id/requirement/decision/priority/guards）
    DecisionTable — 等级表（version/updated/format/rows），format=2 为结构化格式
    build_decision_table — 由阶段2与阶段3结果组装等级表文本（v2 YAML）
    compute_version — 计算内容 sha256 前 12 位版本号
    write_decision_table — 组装并写入 decision-table.md（best-effort）
    read_decision_table — 解析并返回 DecisionTable，失败返回 None 不抛异常
    rewrite_decision_table — 按数据行重写等级表（支持 expected_version CAS 与 user 维度）

输入:
    write_decision_table / build_decision_table:
        stage_two_result: dict — 阶段2 artifacts（requirements/discarded_requirements）
        stage_three_result: dict — 阶段3 artifacts（requirements 列表，每项含 requirement 与 priority）
    read_decision_table / rewrite_decision_table:
        thread_id: str — 线程标识
        user_id: str | None — 用户标识；非空时存储到 requirements/{user_id}/{thread_id}/ 下
        root: Path | None — 仓库根目录，None 时取模块推导的项目根（测试传入临时目录）

输出:
    build_decision_table → str — 完整等级表文本（v2 YAML）
    compute_version → str — 12 位十六进制版本号
    write_decision_table / rewrite_decision_table → str | None — 成功返回 version，失败/冲突返回 None
    read_decision_table → DecisionTable | None — 解析成功返回实例，失败返回 None

具体工作流:
    (1) v2 序列化：表内容为结构化 rows 列表（每条含 id/requirement/decision/priority/可选 guards），
        用 YAML 落盘，避免裸 markdown 表格对 | / --- / 换行 的转义脆弱性
    (2) version 只基于 rows 的规范化序列化内容（不含 updated），保证内容不变版本稳定
    (3) 解析双读：v2 为纯 YAML（含 format 与 rows）；v1 为旧 frontmatter + markdown 表格，
        回填 id=sha256(requirement)[:12]、guards=空、format=1，行为与升级前一致
    (4) guard 解析期校验：非法 guard（坏 kind/target/operator/不可编译正则）→ 丢弃该 guard 并记 warning
    (5) rewrite 支持 expected_version CAS：磁盘版本不符则拒绝覆盖（返回 None）

示例:
    table = read_decision_table("th-001", user_id="u-1")
    version = rewrite_decision_table("th-001", rows, user_id="u-1", expected_version="abc")
"""

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[6]

_FORMAT_V2 = 2
_FORMAT_V1 = 1

_DISCARDED_PRIORITY = 0

_VALID_KINDS = frozenset({"forbid", "require"})
_VALID_TARGETS = frozenset({
    "shell", "file_path", "file_content", "url", "query", "knowledge", "subagent",
})
_VALID_OPERATORS = frozenset({"regex", "glob", "contains", "exact"})


@dataclass
class Guard:
    """硬层守卫谓词。"""

    kind: str
    target: str
    operator: str
    pattern: str
    message: str = ""

    def to_dict(self) -> dict[str, str]:
        value: dict[str, str] = {
            "kind": self.kind,
            "target": self.target,
            "operator": self.operator,
            "pattern": self.pattern,
        }
        if self.message:
            value["message"] = self.message
        return value

    @staticmethod
    def from_dict(value: Any) -> "Guard | None":
        """解析 guard 字典，非法返回 None。

        输入:
            value: Any — guard 字典（或非法值）

        输出:
            Guard | None — 解析成功返回实例，非法返回 None
        """
        if not isinstance(value, dict):
            return None
        kind = str(value.get("kind", "") or "").strip()
        target = str(value.get("target", "") or "").strip()
        operator = str(value.get("operator", "") or "").strip()
        pattern = str(value.get("pattern", "") or "")
        message = str(value.get("message", "") or "").strip()
        if kind not in _VALID_KINDS or target not in _VALID_TARGETS or operator not in _VALID_OPERATORS:
            return None
        if not pattern:
            return None
        if operator == "regex":
            try:
                re.compile(pattern)
            except re.error:
                return None
        return Guard(kind=kind, target=target, operator=operator, pattern=pattern, message=message)


@dataclass
class DecisionRow:
    """单条决策条目。"""

    requirement: str
    decision: str
    priority: int
    id: str = ""
    guards: list[Guard] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.id,
            "requirement": self.requirement,
            "decision": self.decision,
            "priority": self.priority,
        }
        if self.guards:
            value["guards"] = [guard.to_dict() for guard in self.guards]
        return value

    @property
    def is_hard(self) -> bool:
        """是否硬层条目（含至少一条守卫谓词）。"""
        return bool(self.guards)


@dataclass
class DecisionTable:
    """决策等级表。"""

    version: str
    updated: str
    rows: list[DecisionRow]
    format: int = _FORMAT_V2

    def hard_entries(self) -> list[DecisionRow]:
        """返回硬层条目（含 guards 的条目）。"""
        return [row for row in self.rows if row.is_hard]


def _row_id(requirement: str) -> str:
    """由 requirement 文本生成确定性条目 id（sha256 前 12 位）。

    输入:
        requirement: str — 条目要求文本

    输出:
        str — 12 位十六进制 id
    """
    return hashlib.sha256(requirement.encode("utf-8")).hexdigest()[:12]


def compute_version(body: str) -> str:
    """计算内容的 sha256 前 12 位版本号。

    输入:
        body: str — 待哈希的内容

    输出:
        str — 12 位十六进制版本号
    """
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def _rows_body(rows: list[DecisionRow]) -> str:
    """将数据行规范化为稳定字符串（用于版本计算）。

    输入:
        rows: list[DecisionRow] — 数据行

    输出:
        str — 规范化 YAML 字符串（sort_keys 保证键顺序稳定）
    """
    return yaml.safe_dump(
        [row.to_dict() for row in rows],
        allow_unicode=True,
        sort_keys=True,
    )


def _serialize(table: DecisionTable) -> str:
    """将等级表序列化为 v2 YAML 文本。

    输入:
        table: DecisionTable — 等级表实例

    输出:
        str — v2 YAML 文本
    """
    doc: dict[str, Any] = {
        "format": table.format,
        "version": table.version,
        "updated": table.updated,
        "rows": [row.to_dict() for row in table.rows],
    }
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)


def _table_path(base: Path, user_id: str | None, thread_id: str) -> Path:
    """构造等级表文件路径（user 维度可选）。

    输入:
        base: Path — 仓库根目录
        user_id: str | None — 用户标识；非空则按用户隔离
        thread_id: str — 线程标识

    输出:
        Path — decision-table.md 文件路径
    """
    if user_id:
        return base / "requirements" / str(user_id) / str(thread_id) / "decision-table.md"
    return base / "requirements" / str(thread_id) / "decision-table.md"


def _stage_three_priorities(stage_three_result: dict) -> dict[str, int]:
    """从阶段3结果提取 requirement → priority 映射（受保护 helper）。

    输入:
        stage_three_result: dict — 阶段3 artifacts

    输出:
        dict[str, int] — 要求文本（strip 后）到优先级的映射；仅收集 1/2/3 的合法值
    """
    result: dict[str, int] = {}
    for item in stage_three_result.get("requirements", []):
        if not isinstance(item, dict):
            continue
        requirement = str(item.get("requirement") or item.get("text") or "").strip()
        priority = item.get("priority")
        if requirement and isinstance(priority, int) and priority in {1, 2, 3}:
            result[requirement] = priority
    return result


def _build_body_rows(stage_two_result: dict, stage_three_result: dict) -> list[DecisionRow]:
    """组装等级表数据行（受保护 helper）。

    输入:
        stage_two_result: dict — 阶段2 artifacts
        stage_three_result: dict — 阶段3 artifacts

    输出:
        list[DecisionRow] — 保留要求（decision=保留，优先级取自阶段3，缺省 3）
                            与丢弃要求（decision=丢弃，优先级 0）的数据行
    """
    priorities = _stage_three_priorities(stage_three_result)
    rows: list[DecisionRow] = []
    for requirement in stage_two_result.get("requirements", []):
        if not isinstance(requirement, str):
            continue
        rows.append(
            DecisionRow(
                requirement=requirement,
                decision="保留",
                priority=priorities.get(requirement.strip(), 3),
                id=_row_id(requirement),
            )
        )
    for requirement in stage_two_result.get("discarded_requirements", []):
        if not isinstance(requirement, str):
            continue
        rows.append(
            DecisionRow(
                requirement=requirement,
                decision="丢弃",
                priority=_DISCARDED_PRIORITY,
                id=_row_id(requirement),
            )
        )
    return rows


def build_decision_table(stage_two_result: dict, stage_three_result: dict) -> str:
    """由阶段2与阶段3结果组装等级表文本（v2 YAML）。

    输入:
        stage_two_result: dict — 阶段2 artifacts
        stage_three_result: dict — 阶段3 artifacts

    输出:
        str — v2 YAML 等级表文本
    """
    rows = _build_body_rows(stage_two_result, stage_three_result)
    version = compute_version(_rows_body(rows))
    updated = datetime.now(timezone.utc).isoformat()
    return _serialize(DecisionTable(version=version, updated=updated, rows=rows, format=_FORMAT_V2))


def rewrite_decision_table(
    thread_id: str,
    rows: list[DecisionRow],
    *,
    user_id: str | None = None,
    expected_version: str | None = None,
    root: Path | None = None,
) -> str | None:
    """按数据行重写决策等级表（通用写入入口，支持 CAS 与 user 维度）。

    输入:
        thread_id: str — 线程标识
        rows: list[DecisionRow] — 完整条目列表（替换整个表体）
        user_id: str | None — 用户标识，非空则按用户隔离
        expected_version: str | None — 期望版本；非空且与磁盘版本不符时拒绝覆盖
        root: Path | None — 仓库根目录，None 时取模块推导的项目根

    输出:
        str | None — 写入成功返回 version；CAS 冲突或任何失败返回 None 并记录日志
    """
    try:
        base = root if root is not None else _PROJECT_ROOT
        path = _table_path(base, user_id, thread_id)

        if expected_version is not None:
            existing = read_decision_table(thread_id, user_id=user_id, root=root)
            if existing is not None and existing.version != expected_version:
                logger.warning(
                    "重写决策等级表被 CAS 拒绝 (thread_id=%s, expected=%s, actual=%s)",
                    thread_id, expected_version, existing.version,
                )
                return None

        for row in rows:
            if not row.id:
                row.id = _row_id(row.requirement)

        version = compute_version(_rows_body(rows))
        updated = datetime.now(timezone.utc).isoformat()
        table = DecisionTable(version=version, updated=updated, rows=rows, format=_FORMAT_V2)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_serialize(table), encoding="utf-8")
        return version
    except Exception:
        logger.error("重写决策等级表失败 (thread_id=%s)，已跳过（best-effort）", thread_id, exc_info=True)
        return None


def write_decision_table(
    thread_id: str,
    stage_two_result: dict,
    stage_three_result: dict,
    *,
    user_id: str | None = None,
    root: Path | None = None,
) -> str | None:
    """由阶段2/3结果组装并写入决策等级表（best-effort）。

    输入:
        thread_id: str — 线程标识
        stage_two_result: dict — 阶段2 artifacts
        stage_three_result: dict — 阶段3 artifacts
        user_id: str | None — 用户标识，非空则按用户隔离
        root: Path | None — 仓库根目录，None 时取模块推导的项目根

    输出:
        str | None — 写入成功返回 version；任何失败返回 None 并记录日志
    """
    return rewrite_decision_table(
        thread_id,
        _build_body_rows(stage_two_result, stage_three_result),
        user_id=user_id,
        root=root,
    )


def read_decision_table(
    thread_id: str,
    *,
    user_id: str | None = None,
    root: Path | None = None,
) -> DecisionTable | None:
    """解析并返回决策等级表。

    输入:
        thread_id: str — 线程标识
        user_id: str | None — 用户标识，非空则按用户隔离
        root: Path | None — 仓库根目录，None 时取模块推导的项目根

    输出:
        DecisionTable | None — 解析成功返回实例；文件不存在、格式非法或解析异常返回 None
    """
    base = root if root is not None else _PROJECT_ROOT
    path = _table_path(base, user_id, thread_id)
    try:
        content = path.read_text(encoding="utf-8")
        return _parse_decision_table(content)
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning("解析决策等级表失败 (thread_id=%s)", thread_id, exc_info=True)
        return None


def _parse_v2_rows(data: dict) -> list[DecisionRow]:
    """解析 v2 rows 列表（受保护 helper）。

    输入:
        data: dict — v2 YAML 解析结果

    输出:
        list[DecisionRow] — 解析出的条目；非法条目跳过，非法 guard 丢弃并记 warning
    """
    rows: list[DecisionRow] = []
    for item in data.get("rows", []) or []:
        if not isinstance(item, dict):
            continue
        requirement = str(item.get("requirement", "") or "").strip()
        decision = str(item.get("decision", "") or "").strip()
        priority = item.get("priority")
        row_id = str(item.get("id", "") or "").strip() or _row_id(requirement)
        if not requirement or decision not in {"保留", "丢弃"} or not isinstance(priority, int):
            continue
        guards: list[Guard] = []
        for guard_value in item.get("guards", []) or []:
            guard = Guard.from_dict(guard_value)
            if guard is None:
                logger.warning("决策等级表条目 '%s' 含非法 guard，已丢弃该 guard", row_id)
                continue
            guards.append(guard)
        rows.append(DecisionRow(
            requirement=requirement,
            decision=decision,
            priority=priority,
            id=row_id,
            guards=guards,
        ))
    return rows


def _parse_v2(data: dict) -> DecisionTable:
    """解析 v2 YAML 数据（受保护 helper）。

    输入:
        data: dict — v2 YAML 解析结果

    输出:
        DecisionTable — 解析出的等级表实例
    """
    version = str(data.get("version", "") or "")
    updated = str(data.get("updated", "") or "")
    rows = _parse_v2_rows(data)
    return DecisionTable(version=version, updated=updated, rows=rows, format=_FORMAT_V2)


def _parse_v1(content: str) -> DecisionTable | None:
    """解析 v1 旧格式（frontmatter + markdown 表格），回填 id 与 guards（受保护 helper）。

    输入:
        content: str — v1 文件全文

    输出:
        DecisionTable | None — 解析成功返回实例，非法返回 None
    """
    if not content.startswith("---"):
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None

    frontmatter: dict[str, str] = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            frontmatter[key.strip()] = value.strip()

    version = frontmatter.get("version", "")
    updated = frontmatter.get("updated", "")
    if not version:
        return None

    rows: list[DecisionRow] = []
    for line in parts[2].strip().splitlines():
        line = line.strip()
        if not line.startswith("|") or "---" in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 3:
            continue
        requirement, decision, priority = cells
        if not requirement or decision not in {"保留", "丢弃"}:
            continue
        try:
            priority_value = int(priority)
        except ValueError:
            continue
        rows.append(DecisionRow(
            requirement=requirement,
            decision=decision,
            priority=priority_value,
            id=_row_id(requirement),
        ))

    return DecisionTable(version=version, updated=updated, rows=rows, format=_FORMAT_V1)


def _parse_decision_table(content: str) -> DecisionTable | None:
    """解析等级表文本（双读：v2 纯 YAML / v1 旧 frontmatter+表格）。

    输入:
        content: str — 等级表文件全文

    输出:
        DecisionTable | None — 解析成功返回实例，非法返回 None
    """
    stripped = content.strip()
    if not stripped:
        return None
    if stripped.startswith("---"):
        return _parse_v1(content)
    try:
        data = yaml.safe_load(content)
    except Exception:
        return None
    if isinstance(data, dict) and "rows" in data:
        return _parse_v2(data)
    return None
