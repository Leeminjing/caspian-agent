"""
本文件对外提供 Docker 容器操作的底层函数，作为 AioSandboxProvider 的 Docker 后端。

对外提供:
    pull_image(image: str) -> None     — 拉取 Docker 镜像
    create_container(...) -> tuple[str, int] — 创建容器并返回 (container_id, host_port)
    start_container(container_id: str) -> None — 启动容器主进程
    health_check(host: str, port: int) -> bool  — HTTP 探测 AIO 服务可用性

输入:
    pull_image: image: str — Docker 镜像名
    create_container: image, port, mounts, environment, container_name, user_data_root, skills_path
    start_container: container_id: str
    health_check: host: str, port: int

输出:
    pull_image → None
    create_container → (container_id: str, host_port: int)
        host_port 为 _find_free_port 实际分配的宿主机端口（可能大于起始 port）
    start_container → None
    health_check → bool

具体工作流:
    pull_image: (1) client.images.get() 检查 (2) 不存在则 pull
    create_container: (1) 端口搜索 (2) 组装 bind mount (3) 创建容器 (4) 返回容器 id 与实际端口
    health_check: (1) GET http://{host}:{port}/ (2) 重试至 200 或超时

示例:
    from caspian.community.aio_sandbox.local_backend import pull_image, create_container, start_container, health_check

    pull_image("ghcr.io/agent-infra/sandbox:latest")
    cid, host_port = create_container(image="...", port=8080, mounts=[], environment={},
                                      container_name="caspian-sandbox-...",
                                      user_data_root=".caspian/users/.../user-data",
                                      skills_path="/mnt/skills")
    start_container(cid)
    assert health_check("localhost", host_port)
"""

import logging
import socket
import time
import urllib.request
from contextlib import closing
from typing import Any

import docker
from docker.errors import ImageNotFound

logger = logging.getLogger(__name__)

_CONTAINER_AIO_PORT = 8080
_HEALTH_CHECK_TIMEOUT = 60
_HEALTH_CHECK_INTERVAL = 2


def _docker_used_host_ports(client) -> set[int]:
    """收集全部容器(含未启动)已映射的宿主机端口。

    输入:
        client — docker.from_env() 客户端

    输出:
        set[int] — 已分配宿主机端口集合

    工作流:
        (1) 遍历所有容器 attrs 的 NetworkSettings.Ports
        (2) 收集每个绑定的 HostPort 为 int
    """
    used: set[int] = set()
    for container in client.containers.list(all=True):
        ports = container.attrs.get("NetworkSettings", {}).get("Ports") or {}
        for bindings in ports.values():
            if not bindings:
                continue
            for binding in bindings:
                host_port = binding.get("HostPort")
                if host_port:
                    try:
                        used.add(int(host_port))
                    except ValueError:
                        pass
    return used


def _find_free_port(start: int) -> int:
    """从 start 起找空闲宿主机端口:先排除 Docker 已分配端口,再 socket bind 兜底。

    Docker Desktop (Windows) 下端口经转发代理,socket bind 探测不到已被容器占用的
    端口,因此必须先查 Docker 端口映射,否则会出现容器 start 时端口冲突。
    """
    client = docker.from_env()
    try:
        used = _docker_used_host_ports(client)
    finally:
        client.close()

    port = start
    while True:
        if port in used:
            port += 1
            continue
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            try:
                s.bind(("", port))
                return port
            except OSError:
                port += 1


def pull_image(image: str) -> None:
    client = docker.from_env()
    try:
        client.images.get(image)
        logger.info("Docker 镜像已存在: %s", image)
    except ImageNotFound:
        logger.info("正在拉取 Docker 镜像: %s ...", image)
        client.images.pull(image)
        logger.info("Docker 镜像拉取完成: %s", image)
    finally:
        client.close()


def create_container(
    *,
    image: str,
    port: int,
    mounts: list[dict],
    environment: dict[str, str],
    container_name: str,
    user_data_root: str,
    skills_path: str,
    pids_limit: int | None = None,
    mem_limit: str | None = None,
    cpu_limit: float | None = None,
) -> tuple[str, int]:
    host_port = _find_free_port(port)
    logger.info("容器 '%s' 端口映射: host 127.0.0.1:%d → container %d", container_name, host_port, _CONTAINER_AIO_PORT)

    all_mounts = [
        docker.types.Mount(target="/mnt/user-data/", source=user_data_root, type="bind"),
        docker.types.Mount(target="/mnt/skills/", source=skills_path, type="bind", read_only=True),
    ]
    for m in mounts:
        all_mounts.append(docker.types.Mount(
            target=m["container_path"], source=m["host_path"],
            type="bind", read_only=m.get("read_only", False),
        ))

    # 安全姿态：恢复 Docker 默认 seccomp（不再显式 unconfined）、阻止提权
    # （no-new-privileges）；资源上限来自配置（cpu_limit <= 0 表示不限 CPU，不传 nano_cpus）。
    # 注：不 blanket 丢弃 capabilities——agent-sandbox 为 all-in-one 镜像（browser/VNC/
    # jupyter/code-server），cap_drop=ALL 会导致其 init 建 gem 用户失败（groupadd 写 /etc/gshadow
    # 失败），容器启动即 Exited(10)。故保留默认能力集，靠 seccomp + no-new-privileges +
    # 资源上限 + 端口回环收窄。
    host_kwargs: dict[str, Any] = {
        "security_opt": ["no-new-privileges:true"],
    }
    if pids_limit is not None:
        host_kwargs["pids_limit"] = pids_limit
    if mem_limit is not None:
        host_kwargs["mem_limit"] = mem_limit
    if cpu_limit is not None and cpu_limit > 0:
        host_kwargs["nano_cpus"] = int(cpu_limit * 1_000_000_000)

    client = docker.from_env()
    try:
        container = client.containers.create(
            image=image, name=container_name,
            ports={f"{_CONTAINER_AIO_PORT}/tcp": ("127.0.0.1", host_port)},
            mounts=all_mounts, environment=environment, detach=True,
            **host_kwargs,
        )
        logger.info("容器已创建: id=%s name=%s", container.id, container_name)
        return container.id, host_port
    finally:
        client.close()


def remove_container(container_id: str) -> None:
    """停止并移除容器（终态清理，best-effort：任一失败仅记日志，不向上抛）。

    输入:
        container_id: str — 容器 ID

    输出:
        None — 成功移除；容器已不存在/已停止时按成功处理
    """
    client = docker.from_env()
    try:
        container = client.containers.get(container_id)
        try:
            container.stop()
        except docker.errors.APIError as exc:
            logger.info("容器 '%s' stop 已忽略（可能已停止）: %s", container_id, exc)
        container.remove(force=True)
        logger.info("容器已移除: id=%s", container_id)
    except docker.errors.NotFound:
        logger.info("容器 '%s' 不存在，移除忽略", container_id)
    finally:
        client.close()


def start_container(container_id: str) -> None:
    client = docker.from_env()
    try:
        container = client.containers.get(container_id)
        container.start()
        logger.info("容器已启动: id=%s", container_id)
    finally:
        client.close()


def health_check(host: str, port: int) -> bool:
    """HTTP 探测容器内 AIO 服务是否可用。GET 请求收到任意响应（含 401）即表示服务就绪。"""
    url = f"http://{host}:{port}/"
    deadline = time.monotonic() + _HEALTH_CHECK_TIMEOUT
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            urllib.request.urlopen(req, timeout=5)
            logger.info("AIO 服务就绪: %s", url)
            return True
        except urllib.error.HTTPError:
            # 401/403 等也是服务在运行
            logger.info("AIO 服务就绪 (HTTP error but reachable): %s", url)
            return True
        except Exception:
            pass
        time.sleep(_HEALTH_CHECK_INTERVAL)
    logger.warning("AIO 服务健康检查超时: %s (%.0fs)", url, _HEALTH_CHECK_TIMEOUT)
    return False
