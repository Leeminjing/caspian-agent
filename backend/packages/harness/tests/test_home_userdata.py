"""覆盖本次 change 的新行为：家目录解析、决策表 home 落盘、config 缺省沙箱翻转、readers 惰性导入。"""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("DASHSCOPE_API_KEY", "d-test")
os.environ.setdefault("JWT_SECRET", "j-test")

_REPO_ROOT = Path(__file__).resolve().parents[4]


class HomePathTest(unittest.TestCase):
    def test_home_paths(self):
        from caspian.runtime.home import (
            caspian_app,
            caspian_env_file,
            caspian_home,
            caspian_users,
        )

        home = Path(os.environ["CASPIAN_HOME"])
        self.assertEqual(caspian_home(), home)
        self.assertEqual(caspian_app(), home / "app")
        self.assertEqual(caspian_users(), home / "users")
        self.assertEqual(caspian_env_file(), home / "config" / ".env")


class DecisionTableHomeTest(unittest.TestCase):
    def test_write_read_in_home_not_repo(self):
        import caspian.agents.commitment.decision_table as dt

        home = Path(os.environ["CASPIAN_HOME"])
        rows = [dt.DecisionRow(requirement="支持 HTTPS", decision="保留", priority=3, id="abc")]
        dt.rewrite_decision_table("th-home", rows)
        home_file = home / "users" / "requirements" / "th-home" / "decision-table.md"
        self.assertTrue(home_file.exists())
        table = dt.read_decision_table("th-home")
        self.assertIsNotNone(table)
        self.assertEqual(table.rows[0].priority, 3)
        # 不应落在仓库根 requirements/
        self.assertFalse((_REPO_ROOT / "requirements" / "th-home" / "decision-table.md").exists())


class ConfigDefaultTest(unittest.TestCase):
    def test_sandbox_default_and_env_override(self):
        from caspian.config.app_config import _resolve_env_item

        os.environ.pop("CASPIAN_SANDBOX", None)
        self.assertEqual(
            _resolve_env_item("${CASPIAN_SANDBOX:-caspian.sandbox.local:LocalSandbox}"),
            "caspian.sandbox.local:LocalSandbox",
        )
        os.environ["CASPIAN_SANDBOX"] = "caspian.community.aio_sandbox.aio_sandbox:AioSandbox"
        self.assertEqual(
            _resolve_env_item("${CASPIAN_SANDBOX:-caspian.sandbox.local:LocalSandbox}"),
            "caspian.community.aio_sandbox.aio_sandbox:AioSandbox",
        )

    def test_model_env_override(self):
        from caspian.config.app_config import _resolve_env_item

        os.environ.pop("OPENAI_BASE_URL", None)
        os.environ.pop("OPENAI_MODEL", None)
        self.assertEqual(_resolve_env_item("${OPENAI_BASE_URL:-https://api.deepseek.com}"), "https://api.deepseek.com")
        self.assertEqual(_resolve_env_item("${OPENAI_MODEL:-deepseek-v4-flash-vision-exp}"), "deepseek-v4-flash-vision-exp")


class ReadersLazyTest(unittest.TestCase):
    HEAVY = ("docx", "pypdfium2", "olefile", "pytesseract")

    def test_heavy_libs_not_loaded_on_import(self):
        for mod in self.HEAVY:
            sys.modules.pop(mod, None)
        import caspian.sandbox.readers  # noqa: F401

        for mod in self.HEAVY:
            self.assertNotIn(mod, sys.modules)
        # 普通文本与 PDF 文本层仍可用（base 依赖 pypdf）
        import caspian.sandbox.readers as readers

        self.assertTrue(callable(getattr(readers, "_read_pdf", None)))
        self.assertTrue(callable(getattr(readers, "_read_docx", None)))


if __name__ == "__main__":
    unittest.main()
