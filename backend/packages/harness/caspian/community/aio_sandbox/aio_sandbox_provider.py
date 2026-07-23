"""
本文件对外提供 AioSandboxProvider 类，负责 Docker 容器的创建、获取与释放。

对外提供:
    AioSandboxProvider — Docker 沙箱容器生命周期管理器

输入:
    __init__(app_config: AppConfig) — 应用配置对象（从中取 sandbox 段）

输出:
    AioSandboxProvider 实例

具体工作流:
    acquire(user_id, thread_id) → str:
    (1) warm-pool 中存在已释放连接的容器 → 复用
    (2) 否则 active < replicas → 创建新容器
    (3) 返回 sandbox_id（格式 local:{user_id}:{thread_id}）

    get(sandbox_id) → AioSandbox:
    (1) 从内部字典取回 AioSandbox 实例，不存在抛 KeyError

    release(sandbox_id) → None:
    (1) 断开 SDK 连接，容器移入 warm-pool 待复用

容器名遵循 {container_prefix}-{sandbox_id} 格式。replicas 计为 active + warm 总数。

示例:
    from caspian.community.aio_sandbox.aio_sandbox_provider import AioSandboxProvider
    from caspian.config import get_app_config

    app_config = get_app_config("config.yaml")
    provider = AioSandboxProvider(app_config)
    sid = provider.acquire("uuid-xxx", "thread-1")
    sandbox = provider.get(sid)
    provider.release(sid)
"""

import logging
import os
from dataclasses import dataclass, field

from caspian.config.app_config import AppConfig
from caspian.sandbox.base import Sandbox
from caspian.sandbox.path_utils import REAL_ROOT

logger = logging.getLogger(__name__)


@dataclass
class _ContainerRecord:
    sandbox_id: str
    container_id: str = ""
    host_port: int = 0
    sandbox: Sandbox | None = None
    sdk_client: object | None = field(default=None, repr=False)


class AioSandboxProvider:

    def __init__(self, app_config: AppConfig) -> None:
        self._app_config = app_config
        self._active: dict[str, _ContainerRecord] = {}
        self._warm: list[_ContainerRecord] = []

    def _container_name(self, sandbox_id: str) -> str:
        # sandbox_id 含冒号 (local:user:thread)，Docker 容器名不允许冒号
        safe = sandbox_id.replace(":", "-")
        return f"{self._app_config.sandbox.container_prefix}-{safe}"

    def _sdk_connect(self, host: str, port: int) -> object:
        from agent_sandbox import Sandbox as SandboxClient
        return SandboxClient(base_url=f"http://{host}:{port}")

    def _sdk_disconnect(self, sdk_client: object) -> None:
        if hasattr(sdk_client, "close"):
            sdk_client.close()

    def acquire(self, user_id: str, thread_id: str) -> str:
        sandbox_id = f"local:{user_id}:{thread_id}"
        if sandbox_id in self._active:
            return sandbox_id

        if self._warm:
            record = self._warm.pop()
            record.sandbox_id = sandbox_id
            record.sdk_client = self._sdk_connect("localhost", record.host_port)
            self._active[sandbox_id] = record
            logger.info("warm-pool 复用容器 → sandbox_id='%s'", sandbox_id)
            return sandbox_id

        total = len(self._active) + len(self._warm)
        if total >= self._app_config.sandbox.replicas:
            raise RuntimeError(
                f"容器已达上限 (replicas={self._app_config.sandbox.replicas}, active={len(self._active)}, warm={len(self._warm)})"
            )

        record = _ContainerRecord(sandbox_id=sandbox_id)
        self._active[sandbox_id] = record
        self._create_container(user_id, thread_id, sandbox_id, record)
        return sandbox_id

    def _create_container(self, user_id: str, thread_id: str, sandbox_id: str, record: _ContainerRecord) -> None:
        from caspian.community.aio_sandbox.local_backend import (
            pull_image, create_container, start_container, health_check,
        )

        pull_image(self._app_config.sandbox.image)

        container_name = self._container_name(sandbox_id)
        user_data_root = REAL_ROOT.format(user_id=user_id, thread_id=thread_id)
        user_data_root = os.path.abspath(user_data_root)

        cid = create_container(
            image=self._app_config.sandbox.image,
            port=self._app_config.sandbox.port,
            mounts=[{"host_path": m.host_path, "container_path": m.container_path, "read_only": m.read_only}
                     for m in self._app_config.sandbox.mounts],
            environment=dict(self._app_config.sandbox.environment),
            container_name=container_name,
            user_data_root=user_data_root,
            skills_path=os.path.abspath(".caspian/skills"),
        )
        record.container_id = cid

        start_container(cid)
        if not health_check("localhost", self._app_config.sandbox.port):
            raise RuntimeError(f"AIO 服务健康检查失败: container_id={cid}")

        record.sdk_client = self._sdk_connect("localhost", self._app_config.sandbox.port)
        logger.info("AioSandbox 容器就绪: sandbox_id='%s' container_id=%s", sandbox_id, cid)

    def get(self, sandbox_id: str) -> Sandbox:
        record = self._active.get(sandbox_id)
        if record is None:
            raise KeyError(f"沙箱不存在: '{sandbox_id}'，请先调用 acquire 创建")
        if record.sandbox is None:
            from caspian.community.aio_sandbox.aio_sandbox import AioSandbox
            user_id, thread_id = self._parse_sandbox_id(sandbox_id)
            record.sandbox = AioSandbox(user_id=user_id, thread_id=thread_id, sdk_client=record.sdk_client)
        return record.sandbox

    @staticmethod
    def _parse_sandbox_id(sandbox_id: str) -> tuple[str, str]:
        # sandbox_id 格式: local:{user_id}:{thread_id}
        _, user_id, thread_id = sandbox_id.split(":", 2)
        return user_id, thread_id

    def release(self, sandbox_id: str) -> None:
        record = self._active.pop(sandbox_id, None)
        if record is None:
            logger.warning("release: sandbox_id '%s' 不在 active 中", sandbox_id)
            return
        if record.sdk_client is not None:
            self._sdk_disconnect(record.sdk_client)
            record.sdk_client = None
        record.sandbox = None
        self._warm.append(record)
        logger.info("容器已释放至 warm-pool: sandbox_id='%s' warm=%d", sandbox_id, len(self._warm))
