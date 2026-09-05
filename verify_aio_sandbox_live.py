"""
AioSandbox 真实容器生命周期端到端验证（需在可访问 Docker daemon 的环境运行）。

验证内容：
    1) acquire -> 真实创建容器（容器名 = {container_prefix}-local-{user}-{thread}）
    2) get -> AioSandbox 实例
    3) write_file / read_file（经容器内 AIO 服务）+ run_shell
    4) release -> stop + remove 容器
    5) 确认无残留容器（再跑一次 docker ps 校验）
    6) 跨用户隔离不复用（构造第二个 user/thread 得到不同容器）

前置：
    - Docker daemon 可达
    - 镜像 ghcr.io/agent-infra/sandbox:latest 已拉取（或脚本自动 pull）
    - 已安装 aiosandbox 依赖：pip install docker agent-sandbox
运行：python verify_aio_sandbox_live.py
"""

import os
import subprocess
import sys
from types import SimpleNamespace

from caspian.config.sandbox_config import SandboxConfig

SANDBOX_IMAGE = "ghcr.io/agent-infra/sandbox:latest"


def _docker_ps():
    r = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True)
    return r.stdout.split()


def _docker_ps_all():
    r = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True)
    return r.stdout.split()


def _provider():
    from caspian.community.aio_sandbox.aio_sandbox_provider import AioSandboxProvider

    sandbox_cfg = SandboxConfig(
        use="caspian.community.aio_sandbox.aio_sandbox:AioSandbox",
        image=SANDBOX_IMAGE,
        port=8080,
        replicas=3,
        container_prefix="caspian-sandbox",
    )
    return AioSandboxProvider(SimpleNamespace(sandbox=sandbox_cfg))


def main() -> int:
    try:
        import agent_sandbox  # noqa: F401
        import docker  # noqa: F401
    except ImportError as exc:
        print(f"[SKIP] 缺少依赖: {exc}. 请运行: pip install docker agent-sandbox")
        return 2

    provider = _provider()
    before_all = set(_docker_ps_all())

    # 1) 用户 A 的容器
    sid_a = provider.acquire("liveuser", "thread-a")
    sandbox_a = provider.get(sid_a)
    name_a = f"caspian-sandbox-local-liveuser-thread-a"
    print(f"[A] acquire -> {sid_a}")

    try:
        # 容器应已出现在 docker ps
        running = set(_docker_ps())
        assert name_a in running, f"容器未找到: {name_a}; running={running}"

        # 写读
        sandbox_a.write_file("/mnt/user-data/workspace/hello.txt", "hello-caspian")
        content = sandbox_a.read_file("/mnt/user-data/workspace/hello.txt")
        assert content == "hello-caspian", f"read 回读不一致: {content!r}"
        print(f"[A] write/read ok -> {content!r}")

        # shell
        out = sandbox_a.run_shell("echo hello-from-sandbox", "bash")
        assert "hello-from-sandbox" in out, f"run_shell 输出异常: {out!r}"
        print(f"[A] run_shell ok -> {out.strip()!r}")

        # 2) 用户 B 的容器应与 A 不同（不复用）
        sid_b = provider.acquire("liveuser", "thread-b")
        sandbox_b = provider.get(sid_b)
        print(f"[B] acquire -> {sid_b}")
        # B 容器名不同
        name_b = "caspian-sandbox-local-liveuser-thread-b"
        assert name_b in set(_docker_ps()), f"B 容器未找到: {name_b}"
        # A 写的文件在 B 不可见
        try:
            sandbox_b.read_file("/mnt/user-data/workspace/hello.txt")
            print("[WARN] B 读到了 A 的文件（隔离被破坏）")
            return 1
        except Exception:
            print("[B] 读不到 A 的文件（隔离 OK）")

        # 3) release A：容器应被 stop+remove，不再出现在 docker ps
        provider.release(sid_a)
        remaining = set(_docker_ps())
        assert name_a not in remaining, f"A 容器 release 后仍存在: {name_a}"
        print("[A] release 后容器已移除 OK")

        # 4) release B + 清理
        provider.release(sid_b)
        remaining = set(_docker_ps())
        assert name_b not in remaining, f"B 容器 release 后仍存在: {name_b}"
        print("[B] release 后容器已移除 OK")
    finally:
        # 兜底清理：确保没有残留的 caspian-sandbox 容器
        for name in ("caspian-sandbox-local-liveuser-thread-a", "caspian-sandbox-local-liveuser-thread-b"):
            if name in _docker_ps_all():
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)
                print(f"[cleanup] 强制移除残留容器 {name}")

    after_all = set(_docker_ps_all())
    leftovers = {c for c in after_all - before_all if c.startswith("caspian-sandbox")}
    if leftovers:
        print(f"[FAIL] 有残留容器: {leftovers}")
        return 1

    print("==========================================")
    print("SUCCESS: AioSandbox 生命周期验证通过（create/use/release/remove、跨用户隔离、无残留）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
