"""本文件对外提供旧 v1/v2 决策表的严格只读导入与 v2 导出。

输入为用户和线程标识或权威快照；输出为旧文件来源、解析行或可读 YAML。
工作流优先读取用户数据目录，回退旧仓库目录；文件损坏时显式失败，导入后不写原文件。
示例：`legacy = load_legacy("u", "t")`。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from caspian.agents.commitment.decision_table import _PROJECT_ROOT, _parse_decision_table, _table_path
from caspian.decision_governance.domain import Row, TableSnapshot
from caspian.runtime.home import caspian_users


@dataclass(frozen=True)
class LegacyTable:
    path: str
    file_hash: str
    legacy_version: str
    format: int
    rows: tuple[Row, ...]


def load_legacy(user_id: str, thread_id: str, *, roots: tuple[Path, ...] | None = None) -> LegacyTable | None:
    bases = roots if roots is not None else (caspian_users(), _PROJECT_ROOT)
    for base in bases:
        path = _table_path(base, user_id, thread_id)
        if not path.exists():
            continue
        raw = path.read_bytes()
        parsed = _parse_decision_table(raw.decode("utf-8"))
        if parsed is None:
            raise ValueError(f"旧决策表损坏: {path}")
        rows = tuple(Row(
            row.id, row.requirement, row.decision, row.priority,
            tuple(guard.to_dict() for guard in row.guards),
        ) for row in parsed.rows)
        return LegacyTable(
            path=str(path), file_hash=hashlib.sha256(raw).hexdigest(),
            legacy_version=parsed.version, format=parsed.format, rows=rows,
        )
    return None


def export_yaml(table: TableSnapshot) -> str:
    import yaml

    return yaml.safe_dump({
        "format": 2, "revision": table.revision, "version": table.version,
        "rows": [row.to_dict() for row in table.rows],
    }, allow_unicode=True, sort_keys=False)
