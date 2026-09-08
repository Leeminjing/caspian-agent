"""覆盖 add-unix-install 的平台差异：key 提示（setx/export）与沙箱可用 shell（平台守卫）。"""

import unittest


class KeyBlockPlatformTest(unittest.TestCase):
    def test_windows_uses_setx(self):
        from caspian.cli import _key_block_lines

        joined = "\n".join(_key_block_lines("win32"))
        self.assertIn('setx OPENAI_API_KEY', joined)
        self.assertNotIn("export OPENAI_API_KEY", joined)

    def test_unix_uses_export(self):
        from caspian.cli import _key_block_lines

        joined = "\n".join(_key_block_lines("darwin"))
        self.assertIn('export OPENAI_API_KEY="<your DeepSeek key>"', joined)
        self.assertTrue(any("export " in line for line in _key_block_lines("darwin")))
        self.assertFalse(any("setx" in line for line in _key_block_lines("darwin")))
        # Linux 与 macOS 一致
        self.assertEqual(_key_block_lines("linux"), _key_block_lines("darwin"))


class ShellMapPlatformTest(unittest.TestCase):
    def test_unix_only_bash_sh(self):
        from caspian.sandbox.local import _shell_map

        for plat in ("darwin", "linux"):
            self.assertEqual(set(_shell_map(plat).keys()), {"bash", "sh"})

    def test_windows_includes_cmd_powershell(self):
        from caspian.sandbox.local import _shell_map

        self.assertEqual(
            set(_shell_map("win32").keys()), {"bash", "sh", "cmd", "powershell"}
        )


if __name__ == "__main__":
    unittest.main()
