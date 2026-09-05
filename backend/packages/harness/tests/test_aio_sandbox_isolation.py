"""
本文件验证容器/本机沙箱的跨用户隔离契约（aio-sandbox capability）。

覆盖两组：
    1) ContainerIsolationProviderTests —— mock local_backend（注入 fake 模块，避免依赖 docker），
       验证 provider 的"一个 (user_id, thread_id) 独占一个容器"：不同 (user,thread) 各自 create、
       user_data_root 互不重叠、同一 (user,thread) 二次 acquire 幂等不重建、release 调 remove_container 移除 A 的容器。
    2) SandboxDataIsolationTests —— 用 LocalSandbox（无 docker 依赖，仅路径隔离）验证跨用户文件不可见：
       A 写 marker、B 读不到；反向同理；A 释放（数据移除）后 B 依旧读不到。

说明：本机沙箱/平台临时目录在部分受限沙箱下拒绝 os.makedirs 深层建目录，因此测试把临时根
      放在当前工作区（CWD）下，tearDown 递归清理，保证可移植。
运行: python -m unittest tests.test_aio_sandbox_isolation
"""

import os
import shutil
import sys
import types
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch


def _env():
    os.environ.setdefault("JWT_SECRET", "test-secret")
    os.environ.setdefault("GITHUB_TOKEN", "test-token")
    os.environ.setdefault("OPENAI_API_KEY", "test-key")
    os.environ.setdefault("DASHSCOPE_API_KEY", "test-dashscope-key")


_env()

# 与 caspian.sandbox.path_utils.REAL_ROOT 相同的格式（占位格式串）
_REAL_ROOT_FORMAT = ".caspian/users/{user_id}/threads/{thread_id}/user-data"


def _workspace_tmp(prefix: str) -> str:
    """在当前工作区下创建一次性临时根目录（避免受限沙箱拒绝 temp 深层 makedirs）。"""
    path = os.path.join(os.getcwd(), f".{prefix}-{uuid.uuid4().hex[:10]}")
    os.makedirs(path, exist_ok=True)
    return path


class ContainerIsolationProviderTests(unittest.TestCase):
    """provider 生命周期隔离：不依赖 docker（注入 fake local_backend 模块）。"""

    def setUp(self):
        self._tmp = _workspace_tmp("caspian-aio")
        self._created = []  # (cid, user_data_root)
        self._counter = [0]

        # fake local_backend 模块：注入 sys.modules，provider 的惰性 import 会取到这些 mock
        fake = types.ModuleType("caspian.community.aio_sandbox.local_backend")
        fake.__package__ = "caspian.community.aio_sandbox"
        fake.__name__ = "caspian.community.aio_sandbox.local_backend"
        fake.pull_image = Mock()
        fake.start_container = Mock()
        fake.health_check = Mock(return_value=True)
        fake.remove_container = Mock()

        def _fake_create(**kwargs):
            self._counter[0] += 1
            cid = f"container-{self._counter[0]}"
            self._created.append((cid, kwargs.get("user_data_root")))
            return cid, 45678

        fake.create_container = Mock(side_effect=_fake_create)
        self._fake_backend = fake
        self._orig_backend = sys.modules.get("caspian.community.aio_sandbox.local_backend")
        sys.modules["caspian.community.aio_sandbox.local_backend"] = fake

    def tearDown(self):
        if self._orig_backend is None:
            sys.modules.pop("caspian.community.aio_sandbox.local_backend", None)
        else:
            sys.modules["caspian.community.aio_sandbox.local_backend"] = self._orig_backend
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _make_provider(self):
        import caspian.community.aio_sandbox.aio_sandbox_provider as prov_mod

        prov_mod.REAL_ROOT = os.path.join(self._tmp, _REAL_ROOT_FORMAT)
        from caspian.community.aio_sandbox.aio_sandbox_provider import AioSandboxProvider

        from caspian.config.sandbox_config import SandboxConfig

        sandbox_cfg = SandboxConfig(use="caspian.community.aio_sandbox.aio_sandbox:AioSandbox")
        app_config = SimpleNamespace(sandbox=sandbox_cfg)
        return prov_mod, AioSandboxProvider(app_config)

    def test_different_user_thread_get_distinct_roots_and_no_cross_reuse(self):
        prov_mod, provider = self._make_provider()
        with patch.object(prov_mod.AioSandboxProvider, "_sdk_connect", return_value=Mock()):
            sid_a = provider.acquire("userA", "t1")
            sid_b = provider.acquire("userB", "t2")
            sid_b_again = provider.acquire("userB", "t2")

        # 两个不同的 (user,thread) 各 create 一次（B 二次 acquire 幂等，不重建）
        self.assertEqual(len(self._created), 2)
        roots = [root for _, root in self._created]
        self.assertNotEqual(roots[0], roots[1])
        # A 的根目录不是 B 的根目录子路径（反之亦然）
        self.assertNotEqual(os.path.commonpath([roots[0], roots[1]]), roots[0])
        self.assertNotEqual(os.path.commonpath([roots[0], roots[1]]), roots[1])
        # 幂等：同一 (user,thread) 再次 acquire 返回同一 sid，且不新 create
        self.assertEqual(sid_b, sid_b_again)
        self.assertEqual(self._fake_backend.create_container.call_count, 2)

    def test_release_stops_and_removes_own_container(self):
        prov_mod, provider = self._make_provider()
        with patch.object(prov_mod.AioSandboxProvider, "_sdk_connect", return_value=Mock()):
            sid_a = provider.acquire("userA", "t1")
            provider.acquire("userB", "t2")
            provider.release(sid_a)

        # release 仅移除 A 的容器（_created[0]），不触碰 B
        self.assertEqual(self._fake_backend.remove_container.call_count, 1)
        removed_cid = self._fake_backend.remove_container.call_args[0][0]
        self.assertEqual(removed_cid, self._created[0][0])
        # A 释放后，A 的容器记录不再在 _active 中（后续 re-acquire 应重新 create）
        with patch.object(prov_mod.AioSandboxProvider, "_sdk_connect", return_value=Mock()):
            sid_a2 = provider.acquire("userA", "t1")
        self.assertIn(sid_a2, provider._active)
        self.assertEqual(self._fake_backend.create_container.call_count, 3)


class SandboxDataIsolationTests(unittest.TestCase):
    """跨用户文件不可见：用 LocalSandbox（无 docker）验证 per-(user,thread) 根目录隔离。"""

    def setUp(self):
        self._tmp = _workspace_tmp("caspian-fs")
        import caspian.sandbox.path_utils as path_utils
        import caspian.sandbox.local as local_mod

        self._orig_path_utils_real_root = path_utils.REAL_ROOT
        self._orig_local_real_root = local_mod.REAL_ROOT
        path_utils.REAL_ROOT = os.path.join(self._tmp, _REAL_ROOT_FORMAT)
        local_mod.REAL_ROOT = os.path.join(self._tmp, _REAL_ROOT_FORMAT)

    def tearDown(self):
        import caspian.sandbox.path_utils as path_utils
        import caspian.sandbox.local as local_mod

        path_utils.REAL_ROOT = self._orig_path_utils_real_root
        local_mod.REAL_ROOT = self._orig_local_real_root
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _root(self, user_id, thread_id):
        return os.path.join(self._tmp, _REAL_ROOT_FORMAT.format(user_id=user_id, thread_id=thread_id))

    def test_a_writes_b_cannot_read_and_reverse(self):
        from caspian.sandbox.local import LocalSandbox

        a = LocalSandbox("userA", "t1")
        b = LocalSandbox("userB", "t2")

        a.write_file("/mnt/user-data/workspace/marker-a.txt", "secret-A")
        self.assertTrue(os.path.exists(os.path.join(self._root("userA", "t1"), "workspace", "marker-a.txt")))
        with self.assertRaises(OSError):
            b.read_file("/mnt/user-data/workspace/marker-a.txt")

        b.write_file("/mnt/user-data/workspace/marker-b.txt", "secret-B")
        self.assertTrue(os.path.exists(os.path.join(self._root("userB", "t2"), "workspace", "marker-b.txt")))
        with self.assertRaises(OSError):
            a.read_file("/mnt/user-data/workspace/marker-b.txt")

    def test_after_a_released_b_still_cannot_read(self):
        from caspian.sandbox.local import LocalSandbox

        a = LocalSandbox("userA", "t1")
        b = LocalSandbox("userB", "t2")
        a.write_file("/mnt/user-data/workspace/marker-a.txt", "secret-A")
        a_root = self._root("userA", "t1")
        self.assertTrue(os.path.exists(os.path.join(a_root, "workspace", "marker-a.txt")))

        # 模拟 A 释放：沙箱数据被移除（容器被 stop/remove 后其挂载数据不再可达）
        shutil.rmtree(a_root, ignore_errors=True)

        # B 读不到 A 的 marker（B 的根目录从无该文件，且 A 数据已移除）
        with self.assertRaises(OSError):
            b.read_file("/mnt/user-data/workspace/marker-a.txt")
        self.assertFalse(os.path.exists(os.path.join(self._root("userB", "t2"), "workspace", "marker-a.txt")))


class SandboxBackendSelectionTests(unittest.TestCase):
    """容器沙箱为默认后端且可经环境变量切回本机沙箱（spec 需求 6 的两个场景）。"""

    def _reset_provider(self):
        import caspian.sandbox.provider as prov_mod

        self._orig_provider = prov_mod._sandbox_provider
        prov_mod._sandbox_provider = None
        return prov_mod

    def tearDown(self):
        import caspian.sandbox.provider as prov_mod

        prov_mod._sandbox_provider = getattr(self, "_orig_provider", None)

    def _config(self, use):
        return SimpleNamespace(sandbox=SimpleNamespace(use=use))

    def test_default_value_resolves_to_aio_sandbox_provider(self):
        # 场景「默认解析为容器沙箱」：sandbox.use 为容器沙箱路径 → AioSandboxProvider
        prov_mod = self._reset_provider()
        from caspian.community.aio_sandbox.aio_sandbox_provider import AioSandboxProvider
        from caspian.sandbox.provider import get_sandbox_provider

        with patch(
            "caspian.config.get_app_config",
            return_value=self._config("caspian.community.aio_sandbox.aio_sandbox:AioSandbox"),
        ):
            provider = get_sandbox_provider()
        self.assertIsInstance(provider, AioSandboxProvider)

    def test_local_value_resolves_to_local_sandbox_provider(self):
        # 场景「环境变量切到本机沙箱」：sandbox.use 为本机沙箱路径 → SandboxProvider (Local)
        prov_mod = self._reset_provider()
        from caspian.sandbox.provider import SandboxProvider, get_sandbox_provider

        with patch(
            "caspian.config.get_app_config",
            return_value=self._config("caspian.sandbox.local:LocalSandbox"),
        ):
            provider = get_sandbox_provider()
        self.assertIsInstance(provider, SandboxProvider)


if __name__ == "__main__":
    unittest.main()
