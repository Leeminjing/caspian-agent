"""
本文件对外提供 SandboxConfig 和 MountConfig 两个 Pydantic 配置模型，SandboxConfig 使用 Pydantic 的 extra="allow" 以兼容新增字段。

对外提供:
    SandboxConfig(BaseModel) — 沙箱配置段的数据模型
    MountConfig(BaseModel) — Docker bind mount 配置项（AioSandbox 专用）

输入: config.yaml 中 sandbox 段的原始数据
输出: SandboxConfig 实例

字段:
    use: str                    — 沙箱实现类路径
    image: str                  — Docker 镜像名（默认 "all-in-one-sandbox:latest"）
    port: int                   — 宿主机端口搜索起始值（默认 8080）
    replicas: int               — 活动容器与 warm-pool 数量上限（默认 3）
    container_prefix: str       — 容器名前缀（默认 "focus-sandbox"）
    mounts: list[MountConfig]   — 额外 bind mount 列表（默认 []）
    environment: dict[str,str]  — 容器环境变量（默认 {}）

示例:
    from focus.config.sandbox_config import SandboxConfig

    cfg = SandboxConfig(use="focus.sandbox.local:LocalSandbox")
    cfg = SandboxConfig(
        use="focus.community.aio_sandbox:AioSandbox",
        image="ghcr.io/agent-infra/sandbox:latest",
        port=8080,
        replicas=3,
    )
"""

from pydantic import BaseModel


class MountConfig(BaseModel):
    """Docker bind mount 配置项。"""

    host_path: str
    container_path: str
    read_only: bool = False


class SandboxConfig(BaseModel):
    use: str
    image: str = "ghcr.io/agent-infra/sandbox:latest"
    port: int = 8080
    replicas: int = 3
    container_prefix: str = "focus-sandbox"
    mounts: list[MountConfig] = []
    environment: dict[str, str] = {}
