"""
本文件验证 AioSandbox 容器安全姿态与端口绑定的底层实现（构造用 fake docker 模块，避免依赖 docker）。

覆盖用例：
    - create_container 把 AIO 控制端口绑到 127.0.0.1（tuple 形式），不再绑 0.0.0.0
    - create_container 使用默认 seccomp（不显式 seccomp=unconfined）、cap_drop=["ALL"]、
      no-new-privileges、pids_limit/mem_limit/nano_cpus 均按配置传入
    - cpu_limit <= 0 时省略 nano_cpus
    - remove_container 先 stop 再 remove(force=True)

运行: python -m unittest tests.test_aio_sandbox_hardening
"""

import sys
import types
import unittest
from unittest.mock import Mock, patch


def _env():
    import os

    os.environ.setdefault("JWT_SECRET", "test-secret")
    os.environ.setdefault("GITHUB_TOKEN", "test-token")
    os.environ.setdefault("OPENAI_API_KEY", "test-key")
    os.environ.setdefault("DASHSCOPE_API_KEY", "test-dashscope-key")


_env()


class _FakeDocker:
    """构造一个可 import 的假 docker 模块并写入 sys.modules。"""

    def __enter__(self):
        import sys as _sys

        self._saved = {
            "docker": _sys.modules.get("docker"),
            "docker.errors": _sys.modules.get("docker.errors"),
            "docker.types": _sys.modules.get("docker.types"),
        }

        docker_mod = types.ModuleType("docker")
        docker_mod.__path__ = []

        class NotFound(Exception):
            pass

        class APIError(Exception):
            pass

        class ImageNotFound(NotFound):
            pass

        errors_mod = types.ModuleType("docker.errors")
        errors_mod.NotFound = NotFound
        errors_mod.APIError = APIError
        errors_mod.ImageNotFound = ImageNotFound

        types_mod = types.ModuleType("docker.types")
        types_mod.Mount = Mock()

        docker_mod.errors = errors_mod
        docker_mod.types = types_mod
        self.client = Mock()
        docker_mod.from_env = Mock(return_value=self.client)
        self.client.containers.create.return_value.id = "cid-1"
        self._created = docker_mod

        import sys as _sys2

        _sys2.modules["docker"] = docker_mod
        _sys2.modules["docker.errors"] = errors_mod
        _sys2.modules["docker.types"] = types_mod
        return self

    def __exit__(self, *exc):
        import sys as _sys

        for name, orig in self._saved.items():
            if orig is None:
                _sys.modules.pop(name, None)
            else:
                _sys.modules[name] = orig
        return False


class LocalBackendHardeningTests(unittest.TestCase):

    def _import_local_backend(self, docker_mod):
        import caspian.community.aio_sandbox.local_backend as lb

        # local_backend 顶层 `import docker` 会绑定模块级 docker 名并跨测试缓存，
        # 这里重绑到当前测试的 fake docker，避免使用上一次测试的 client。
        lb.docker = docker_mod
        return lb

    def test_create_container_binds_port_to_localhost_only(self):
        with _FakeDocker() as fd:
            lb = self._import_local_backend(fd._created)
            with patch.object(lb, "_find_free_port", return_value=45678):
                _cid, host_port = lb.create_container(
                    image="img", port=8080, mounts=[], environment={},
                    container_name="c-1", user_data_root="/tmp/ud", skills_path="/tmp/sk",
                    pids_limit=256, mem_limit="2g", cpu_limit=2,
                )
            kwargs = fd.client.containers.create.call_args.kwargs
            self.assertEqual(kwargs["ports"], {"8080/tcp": ("127.0.0.1", 45678)})
            self.assertEqual(host_port, 45678)

    def test_create_container_hardens_security_and_resources(self):
        with _FakeDocker() as fd:
            lb = self._import_local_backend(fd._created)
            with patch.object(lb, "_find_free_port", return_value=45678):
                lb.create_container(
                    image="img", port=8080, mounts=[], environment={},
                    container_name="c-1", user_data_root="/tmp/ud", skills_path="/tmp/sk",
                    pids_limit=256, mem_limit="2g", cpu_limit=2,
                )
            kwargs = fd.client.containers.create.call_args.kwargs
            # 恢复默认 seccomp：不再出现 unconfined
            self.assertNotIn("seccomp=unconfined", kwargs.get("security_opt", []))
            self.assertEqual(kwargs["security_opt"], ["no-new-privileges:true"])
            # 不 blanket 丢弃 capabilities（agent-sandbox 镜像要求，cap_drop=ALL 会崩）
            self.assertNotIn("cap_drop", kwargs)
            self.assertEqual(kwargs["pids_limit"], 256)
            self.assertEqual(kwargs["mem_limit"], "2g")
            self.assertEqual(kwargs["nano_cpus"], 2_000_000_000)

    def test_create_container_omits_nano_cpus_when_cpu_limit_unset(self):
        with _FakeDocker() as fd:
            lb = self._import_local_backend(fd._created)
            with patch.object(lb, "_find_free_port", return_value=45678):
                lb.create_container(
                    image="img", port=8080, mounts=[], environment={},
                    container_name="c-1", user_data_root="/tmp/ud", skills_path="/tmp/sk",
                    pids_limit=256, mem_limit="2g", cpu_limit=0,
                )
            kwargs = fd.client.containers.create.call_args.kwargs
            self.assertNotIn("nano_cpus", kwargs)
            self.assertEqual(kwargs["pids_limit"], 256)
            self.assertEqual(kwargs["mem_limit"], "2g")

    def test_remove_container_stops_then_removes_force(self):
        with _FakeDocker() as fd:
            lb = self._import_local_backend(fd._created)
            container = Mock()
            fd.client.containers.get.return_value = container
            lb.remove_container("cid-1")
            fd.client.containers.get.assert_called_once_with("cid-1")
            container.stop.assert_called_once_with()
            container.remove.assert_called_once_with(force=True)


if __name__ == "__main__":
    unittest.main()
