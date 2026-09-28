"""本文件验证旧格式只读导入与权威快照导出。

输入为临时目录中的 v1/v2 决策文件；输出为内容、来源哈希及损坏文件报错断言。
工作流分别解析旧 frontmatter 表和 YAML，并核对导入不会改写源文件。
示例：`pytest tests/test_decision_governance_legacy.py`。
"""

from pathlib import Path

import pytest
import yaml

from caspian.agents.commitment.decision_table import build_decision_table
from caspian.decision_governance.domain import snapshot
from caspian.decision_governance.legacy import export_yaml, load_legacy


def _write(root: Path, content: str) -> Path:
    path = root / "requirements" / "u" / "t" / "decision-table.md"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_import_v1_v2_and_export_without_writing_source(tmp_path):
    v1 = "---\nversion: abc\nupdated: 2026-01-01\n---\n| 要求 | 决策 | 等级 |\n| --- | --- | --- |\n| 旧要求 | 保留 | 2 |\n"
    source = _write(tmp_path, v1)
    imported = load_legacy("u", "t", roots=(tmp_path,))
    assert imported.format == 1
    assert imported.legacy_version == "abc"
    assert imported.rows[0].requirement == "旧要求"
    assert imported.rows[0].id
    exported = yaml.safe_load(export_yaml(snapshot(0, imported.rows)))
    assert exported["rows"][0]["requirement"] == "旧要求"
    assert source.read_text(encoding="utf-8") == v1

    v2 = build_decision_table(
        {"requirements": ["新要求"], "discarded_requirements": []},
        {"requirements": [{"requirement": "新要求", "priority": 3}]},
    )
    source.write_text(v2, encoding="utf-8")
    imported_v2 = load_legacy("u", "t", roots=(tmp_path,))
    assert imported_v2.format == 2
    assert imported_v2.rows[0].priority == 3
    assert source.read_text(encoding="utf-8") == v2


def test_corrupt_file_is_not_an_empty_table(tmp_path):
    _write(tmp_path, "invalid")
    with pytest.raises(ValueError, match="损坏"):
        load_legacy("u", "t", roots=(tmp_path,))
