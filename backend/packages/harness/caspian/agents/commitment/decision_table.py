"""
本文件对外提供决策等级表的组装、写入与读取函数，作为承诺层等级表持久化的唯一入口。

对外提供:
    DecisionRow — 单条决策条目数据类（requirement/decision/priority）
    DecisionTable — 等级表数据类（version/rows）
    build_decision_table — 由阶段2与阶段3结果组装等级表 markdown 文本
    compute_version — 计算表体内容的 sha256 前 12 位版本号
    write_decision_table — 组装并写入 requirements/{thread_id}/decision-table.md（best-effort）
    read_decision_table — 解析并返回 DecisionTable，失败返回 None 不抛异常

输入:
    build_decision_table / write_decision_table:
        stage_two_result: dict — 阶段2 artifacts（requirements/discarded_requirements）
        stage_three_result: dict — 阶段3 artifacts（requirements 列表，每项含 requirement 与 priority）
        thread_id: str — 线程标识，用于定位 requirements/{thread_id}/decision-table.md
    read_decision_table:
        thread_id: str — 线程标识
        root: Path | None — 仓库根目录，None 时取模块推导的项目根（测试传入临时目录）

输出:
    build_decision_table → str — 含 frontmatter（version/updated）与表体的完整 markdown
    compute_version → str — 12 位十六进制版本号
    write_decision_table → str | None — 写入成功返回 version，失败返回 None
    read_decision_table → DecisionTable | None — 解析成功返回实例，失败返回 None

具体工作流:
    (1) build_decision_table 将阶段2保留要求标记 decision=保留、丢弃要求标记 decision=丢弃；
        优先级从阶段3按要求文本匹配，匹配不到默认 3；丢弃条目优先级记为 0（重提即升级冲突）
    (2) compute_version 只基于表体（不含 updated 时间戳），保证内容不变时版本稳定，
        时间戳变化不会误触发重新注入
    (3) write_decision_table 组装文本并写入文件，任何失败仅记录日志（best-effort，不阻断合同流程）
    (4) read_decision_table 解析 frontmatter 与表格体，非法格式返回 None

示例:
    table = build_decision_table(stage_two, stage_three)
    version = write_decision_table("th-001", stage_two, stage_three)
    loaded = read_decision_table("th-001")
"""

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[6]

_HEADER_LINE = "| requirement | decision | priority |"
_SEPARATOR_LINE = "|---|---|---|"

_DISCARDED_PRIORITY = 0


@dataclass
class DecisionRow:
    requirement: str
    decision: str
    priority: int


@dataclass
class DecisionTable:
    version: str
    updated: str
    rows: list[DecisionRow]


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
            )
        )
    return rows


def _body_markdown(rows: list[DecisionRow]) -> str:
    """将数据行序列化为表格体 markdown（受保护 helper）。

    输入:
        rows: list[DecisionRow] — 等级表数据行

    输出:
        str — 表头 + 分隔行 + 数据行的完整表格文本
    """
    lines = [_HEADER_LINE, _SEPARATOR_LINE]
    lines.extend(
        f"| {row.requirement} | {row.decision} | {row.priority} |"
        for row in rows
    )
    return "\n".join(lines)


def compute_version(body: str) -> str:
    """计算表体内容的 sha256 前 12 位版本号。

    输入:
        body: str — 表格体文本（不含 frontmatter）

    输出:
        str — 12 位十六进制版本号
    """
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def build_decision_table(stage_two_result: dict, stage_three_result: dict) -> str:
    """由阶段2与阶段3结果组装等级表 markdown。

    输入:
        stage_two_result: dict — 阶段2 artifacts
        stage_three_result: dict — 阶段3 artifacts

    输出:
        str — 含 frontmatter（version/updated）与表体的完整 markdown
    """
    rows = _build_body_rows(stage_two_result, stage_three_result)
    body = _body_markdown(rows)
    version = compute_version(body)
    updated = datetime.now(timezone.utc).isoformat()
    return (
        f"---\n"
        f"version: {version}\n"
        f"updated: {updated}\n"
        f"---\n\n"
        f"{body}\n"
    )


def rewrite_decision_table(
    thread_id: str,
    rows: list[DecisionRow],
    root: Path | None = None,
) -> str | None:
    """按数据行重写决策等级表（通用写入入口）。

    输入:
        thread_id: str — 线程标识
        rows: list[DecisionRow] — 完整条目列表（替换整个表体）
        root: Path | None — 仓库根目录，None 时取模块推导的项目根

    输出:
        str | None — 写入成功返回 version；任何失败返回 None 并记录日志
    """
    try:
        body = _body_markdown(rows)
        version = compute_version(body)
        updated = datetime.now(timezone.utc).isoformat()
        content = (
            f"---\n"
            f"version: {version}\n"
            f"updated: {updated}\n"
            f"---\n\n"
            f"{body}\n"
        )
        base = root if root is not None else _PROJECT_ROOT
        path = base / "requirements" / thread_id / "decision-table.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return version
    except Exception:
        logger.error("重写决策等级表失败 (thread_id=%s)，已跳过（best-effort）", thread_id, exc_info=True)
        return None


def write_decision_table(
    thread_id: str,
    stage_two_result: dict,
    stage_three_result: dict,
    root: Path | None = None,
) -> str | None:
    """由阶段2/3结果组装并写入决策等级表（best-effort）。

    输入:
        thread_id: str — 线程标识
        stage_two_result: dict — 阶段2 artifacts
        stage_three_result: dict — 阶段3 artifacts
        root: Path | None — 仓库根目录，None 时取模块推导的项目根

    输出:
        str | None — 写入成功返回 version；任何失败返回 None 并记录日志
    """
    return rewrite_decision_table(
        thread_id,
        _build_body_rows(stage_two_result, stage_three_result),
        root=root,
    )


def read_decision_table(thread_id: str, root: Path | None = None) -> DecisionTable | None:
    """解析并返回决策等级表。

    输入:
        thread_id: str — 线程标识
        root: Path | None — 仓库根目录，None 时取模块推导的项目根

    输出:
        DecisionTable | None — 解析成功返回实例；文件不存在、格式非法或解析异常返回 None
    """
    base = root if root is not None else _PROJECT_ROOT
    path = base / "requirements" / thread_id / "decision-table.md"
    try:
        content = path.read_text(encoding="utf-8")
        return _parse_decision_table(content)
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning("解析决策等级表失败 (thread_id=%s)", thread_id, exc_info=True)
        return None


def _parse_decision_table(content: str) -> DecisionTable | None:
    """解析等级表 markdown 文本（受保护 helper）。

    输入:
        content: str — 等级表文件全文

    输出:
        DecisionTable | None — 解析成功返回实例，frontmatter 或表格体非法返回 None
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
        rows.append(
            DecisionRow(
                requirement=requirement,
                decision=decision,
                priority=priority_value,
            )
        )

    return DecisionTable(version=version, updated=updated, rows=rows)
