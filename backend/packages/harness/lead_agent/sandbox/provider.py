"""
本文件定义 SandboxProvider 沙箱管理器及全局单例获取函数。

对外提供:
    SandboxProvider: 沙箱全局管理中心，负责沙箱实例的创建与分发
    get_sandbox_provider: 返回 SandboxProvider 全局单例

SandboxProvider 工作流:
    (1) 外部代码调用 get_sandbox_provider() 获取全局 provider
    (2) 调用 provider.acquire(user_id, thread_id) 获取 sandbox_id（(user_id, thread_id) 复合键）
    (3) 如果该 (user_id, thread_id) 的沙箱不存在，就创建沙箱
    (4) 调用 provider.get(sandbox_id) 取回沙箱实例

SandboxProvider 创建沙箱时的类型由 SandboxConfig.use 决定，通过 resolve_class 动态导入。

示例:
    provider = get_sandbox_provider()
    sid = provider.acquire("uuid-xxx", "abc123")
    sb = provider.get(sid)
    content = sb.read_file("/mnt/user-data/workspace/main.py")
"""

from lead_agent.config.sandbox_config import SandboxConfig
from lead_agent.reflection.resolvers import resolve_class
from lead_agent.sandbox.base import Sandbox


class SandboxProvider:

    def __init__(self, sandbox_config: SandboxConfig):
        self._sandbox_config = sandbox_config
        self._sandboxes: dict[str, Sandbox] = {}

    def _create_sandbox(self, user_id: str, thread_id: str) -> Sandbox:
        sandbox_cls = resolve_class(self._sandbox_config.use)
        return sandbox_cls(user_id=user_id, thread_id=thread_id)

    def acquire(self, user_id: str, thread_id: str) -> str:
        sandbox_id = (user_id, thread_id)
        if sandbox_id not in self._sandboxes:
            self._sandboxes[sandbox_id] = self._create_sandbox(user_id, thread_id)
        return sandbox_id

    def get(self, sandbox_id: str) -> Sandbox:
        if sandbox_id not in self._sandboxes:
            raise KeyError(f"沙箱不存在: '{sandbox_id}'，请先调用 acquire 创建")
        return self._sandboxes[sandbox_id]


_sandbox_provider: SandboxProvider | None = None


def get_sandbox_provider() -> SandboxProvider:
    global _sandbox_provider
    if _sandbox_provider is None:
        from lead_agent.config import get_app_config

        app_config = get_app_config("config.yaml")
        _sandbox_provider = SandboxProvider(app_config.sandbox)
    return _sandbox_provider
