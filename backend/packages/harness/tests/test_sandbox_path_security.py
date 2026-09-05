"""
本文件提供沙箱路径围栏安全性的标准库 unittest：

覆盖用例:
    - 根内路径正常放行并返回规范化绝对路径
    - 兄弟前缀逃逸(如 user-data vs user-data-evil)被拒
    - 解析落点为根自身被拒
    - 中段 .. 兄弟逃逸(resolve_path 端到端)被拒
    - validate_subdir 前缀缺失被拒
    - validate_subdir 含 .. (含非首段)被拒
    - validate_subdir 规范化路径放行
    - symlink 逃逸被拒(无权限平台 skip)

验证对象为纯函数 validate_path / validate_subdir / resolve_path,
路径用 tempfile.mkdtemp() 而非硬编码,保证 Windows / Linux 均可运行。
"""

import os
import tempfile
import unittest
from pathlib import Path

from caspian.sandbox.path_utils import (
    SecurityError,
    resolve_path,
    validate_path,
    validate_subdir,
)


def _env():
    import os

    os.environ.setdefault("JWT_SECRET", "test-secret")
    os.environ.setdefault("GITHUB_TOKEN", "test-token")
    os.environ.setdefault("OPENAI_API_KEY", "test-key")
    os.environ.setdefault("DASHSCOPE_API_KEY", "test-dashscope-key")


_env()


class SandboxPathSecurityTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="caspian-sandbox-")

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_valid_path_inside_root(self):
        root = self._tmp
        inside = os.path.join(root, "workspace", "a.py")
        result = validate_path(inside, root)
        self.assertEqual(result, str(Path(inside).resolve()))
        self.assertTrue(os.path.isabs(result))

    def test_reject_sibling_prefix_escape(self):
        root = self._tmp
        # root 名 /tmp/.../caspian-sandbox-xxx; 兄弟目录 root + "-evil" 与其共享字符串前缀
        evil = os.path.join(root + "-evil", "secret.txt")
        with self.assertRaises(SecurityError):
            validate_path(evil, root)

    def test_reject_root_itself(self):
        root = self._tmp
        # candidate.relative_to(root) 对 equal 返回 '.' 而非抛错,须显式拒绝根自身
        with self.assertRaises(SecurityError):
            validate_path(root, root)

    def test_reject_mid_dotdot_sibling_escape(self):
        # 中段 .. 让 validate_subdir 的首段检查(workspace)通过,再经 resolve_path 折叠成兄弟前缀逃逸端到端被拒
        with self.assertRaises(SecurityError):
            resolve_path(
                "/mnt/user-data/workspace/../../user-data-evil/secret.txt",
                "_test_u",
                "_test_t",
            )

    def test_validate_subdir_rejects_missing_virtual_prefix(self):
        with self.assertRaises(SecurityError):
            validate_subdir("XX/workspace/a.txt", {"workspace"})

    def test_validate_subdir_rejects_dotdot(self):
        # 非首段 .. 也必须被拒(workspace/../../...)
        with self.assertRaises(SecurityError):
            validate_subdir("/mnt/user-data/workspace/../../evil", {"workspace"})

    def test_validate_subdir_accepts_canonical(self):
        # 规范化路径应放行,不抛异常
        validate_subdir("/mnt/user-data/workspace/a.txt", {"workspace"})
        validate_subdir("/mnt/user-data/uploads/b.txt", {"uploads", "workspace"})
        # 不在 allowed 内仍应拒绝
        with self.assertRaises(SecurityError):
            validate_subdir("/mnt/user-data/other/c.txt", {"workspace"})

    def test_reject_symlink_escape(self):
        root = self._tmp
        link = os.path.join(root, "workspace")
        outside = tempfile.mkdtemp(prefix="caspian-outside-")
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"当前平台无 symlink 权限,跳过: {exc}")
        try:
            with self.assertRaises(SecurityError):
                validate_path(os.path.join(link, "x.txt"), root)
        finally:
            try:
                os.remove(link)
            except OSError:
                pass
            import shutil

            shutil.rmtree(outside, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
