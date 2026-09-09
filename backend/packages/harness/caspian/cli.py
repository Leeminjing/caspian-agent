"""
本文件对外提供全局 `caspian` 命令的入口（`caspian.cli:main`），基于 argparse 拆 `start` / `update` 子命令。

对外提供:
    main(argv=None) — CLI 入口

输入:
    argv: list[str] | None — 命令行参数；None 时取 sys.argv

输出:
    int — 进程退出码（0=成功，非 0=失败/被拦截）

具体工作流:
    (1) `caspian`（默认/start）：校验运行前置 → 首启 key 校验（缺 key 打印 setx 块）→ 生成随机
        JWT_SECRET（若无）写入最小 .env → 迁移旧仓库根 requirements → 幂等启动 PostgreSQL(pgvector)
        容器 → venv python 执行 `alembic upgrade head` → 以 cwd=~/caspian/app 运行 run_dev.py
    (2) `caspian update`：git fetch origin/main → 记 old → git reset --hard origin/main → 记 new →
        venv python `pip install -e .[runtime,postgres]` → 打印进度与 `old → new`

示例:
    python -m caspian.cli --help
    python -m caspian.cli          # 启动网关
    python -m caspian.cli update   # 更新到 main
"""

import argparse
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

# 中文 Windows 的 locale 是 GBK/cp936；alembic 会以编码 "locale" 读取 alembic.ini（UTF-8 含中文注释），
# 造成 UnicodeDecodeError。强制本 CLI 派生的 Python 子进程（alembic/pip/server）以 UTF-8 模式运行，
# 使配置/脚本按 UTF-8 读取与输出。
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from caspian.runtime.home import caspian_app, caspian_env_file, caspian_home, caspian_users, ensure_home

# 首启必需、缺失时提示 setx 的 provider key
_PROVIDER_KEYS = ("OPENAI_API_KEY", "DASHSCOPE_API_KEY")

_OWN_KEY = "JWT_SECRET"

_UPSTREAM = "origin/main"

_PG_IMAGE = "pgvector/pgvector:pg17"
_PG_NAME = "caspian-postgres"
_PG_PORT = 7221
_PG_USER = "caspian"
_PG_PASSWORD = "qweasdzxc123"
_PG_DB = "caspian"


def _venv_python() -> str:
    """当前解释器（调用方经 `~/.caspian/runtime/.venv/Scripts/python.exe` 运行 CLI，即 venv python）。"""
    return sys.executable


def _read_dotenv_values(path: Path) -> dict[str, str]:
    """读取 .env 文件为 dict（`KEY=VALUE`），未读取到或不存在的键忽略。"""
    result: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            result[(key.strip()).lstrip("$")] = value.strip()
    except OSError:
        return {}
    return result


def _merged_env() -> dict[str, str]:
    """把 home `.env` 合并进当前环境（`.env` 不覆盖已设的真环境变量）。"""
    merged = dict(os.environ)
    for key, value in _read_dotenv_values(caspian_env_file()).items():
        merged.setdefault(key, value)
    return merged


def _missing_provider_keys() -> list[str]:
    merged = _merged_env()
    return [key for key in _PROVIDER_KEYS if not merged.get(key)]


def _key_block_lines(platform: str | None = None) -> list[str]:
    """按平台返回 key 设置提示行：Windows 用 setx，Unix 用 export（写入 shell profile）。

    输入:
        platform: str | None — 目标平台（默认取当前 sys.platform），供测试注入
    """
    plat = platform if platform is not None else sys.platform
    if plat == "win32":
        return [
            "Set them once like this (then open a NEW terminal and re-run `caspian`):",
            'setx OPENAI_API_KEY      "<your DeepSeek key>"',
            'setx DASHSCOPE_API_KEY   "<your DashScope key>"',
            'setx OPENAI_BASE_URL     "https://api.deepseek.com"   # optional',
            'setx OPENAI_MODEL        "deepseek-v4-flash-vision-exp"   # optional',
        ]
    return [
        "Add them to your shell profile (e.g. ~/.zshrc) like this, then open a NEW terminal and re-run `caspian`:",
        'export OPENAI_API_KEY="<your DeepSeek key>"',
        'export DASHSCOPE_API_KEY="<your DashScope key>"',
        'export OPENAI_BASE_URL="https://api.deepseek.com"   # optional',
        'export OPENAI_MODEL="deepseek-v4-flash-vision-exp"   # optional',
    ]


def _print_setx_block(missing: list[str]) -> None:
    print("Missing required API key(s): " + ", ".join(missing))
    for line in _key_block_lines():
        print(line)
    print("")


def _ensure_jwt_secret() -> None:
    """若 JWT_SECRET 不在 env/.env，生成随机值写入 home `.env`（best-effort）。"""
    if _merged_env().get(_OWN_KEY):
        return
    env_file = caspian_env_file()
    env_file.parent.mkdir(parents=True, exist_ok=True)
    new_value = secrets.token_hex(32)
    lines: list[str] = []
    if env_file.exists():
        try:
            lines = [ln for ln in env_file.read_text(encoding="utf-8").splitlines() if not ln.strip().startswith(_OWN_KEY)]
        except OSError:
            lines = []
    lines.append(f"{_OWN_KEY}={new_value}")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _migrate_legacy_requirements() -> int:
    """把旧仓库根 `requirements/{thread_id|user_id/thread_id}` best-effort 迁移到 `~/.caspian/users/requirements/`。

    输入: 无

    输出: int — 迁移的线程目录数；源不存在或无真实数据时返回 0；
                 目标已存在同名线程时不覆盖（保留已有用户数据）。
    """
    src = caspian_app() / "requirements"
    if not src.is_dir():
        return 0
    dest_root = caspian_users() / "requirements"
    migrated = 0
    for entry in src.iterdir():
        if not entry.is_dir():
            continue
        # 只迁移含决策表或任务合同的“真实线程”，跳过 scratch
        has_data = (entry / "decision-table.md").exists() or (entry / "task-contract.md").exists()
        if not has_data:
            continue
        target = dest_root / entry.name
        if target.exists():
            continue  # 不覆盖 home 已有数据
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(entry, target)
            migrated += 1
        except OSError:
            continue
    return migrated


def _run(cmd: list[str], cwd: str | None = None, env: dict[str, str] | None = None, check: bool = False, capture: bool = True) -> subprocess.CompletedProcess:
    """运行子进程；默认捕获 stdout/stderr（便于读取 .stdout/.stderr），capture=False 时继承 stdio。"""
    if capture:
        return subprocess.run(cmd, cwd=cwd, env=env, check=check, capture_output=True, text=True)
    return subprocess.run(cmd, cwd=cwd, env=env, check=check)


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _docker_daemon_running() -> bool:
    """检测 Docker 守护进程（daemon）是否真的在运行（`docker info` 成功）。"""
    return _run(["docker", "info"], check=False).returncode == 0


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """检测 host:port 是否已有服务在监听（用于复用已有数据库 / 避免端口冲突）。"""
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _start_container_or_fail(cmd: list[str]) -> None:
    """`docker start/run` 并检查失败，给出可读原因（不静默吞掉）。"""
    result = _run(cmd, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"启动 PostgreSQL 容器失败：{detail or '未知错误（请检查 Docker 是否运行）'}"
        )


def _ensure_postgres() -> None:
    """幂等确保 PostgreSQL(pgvector) 可用；优先复用已有服务，避免端口冲突。

    工作流:
        (1) 无 Docker CLI → 抛 RuntimeError
        (2) Docker daemon 未运行 → 抛 RuntimeError（提示启动 Docker Desktop）
        (3) 已有 `caspian-postgres` 托管容器 → 管理它：运行则等待；停止但端口被外部占用则清掉改用
            现有服务；否则 start（失败给可读报错）
        (4) 无托管容器但 127.0.0.1:7221 已有服务监听（如用户本机已有 postgres）→ 复用，不新建容器
        (5) 否则创建容器（失败给可读报错）
        (6) 等待就绪 + （容器存在时）启用 vector 扩展
    """
    if not _docker_available():
        raise RuntimeError(
            "未检测到 Docker。Caspian 需要 Docker 来运行 PostgreSQL(pgvector) 数据库。"
            "请安装 Docker Desktop 后重试（https://www.docker.com/products/docker-desktop/）。"
        )
    if not _docker_daemon_running():
        raise RuntimeError(
            "Docker 守护进程（daemon）未运行。请先启动 Docker Desktop，再重试 `caspian`。"
        )
    has_container = _run(["docker", "inspect", _PG_NAME]).returncode == 0
    if has_container:
        state = _run(["docker", "inspect", "-f", "{{.State.Running}}", _PG_NAME]).stdout.strip()
        if state == "true":
            _wait_postgres()
        elif _port_open("127.0.0.1", _PG_PORT):
            # 容器存在但没运行，且端口被其它服务占用 → 这个容器起不来，清掉改用现有服务
            _run(["docker", "rm", "-f", _PG_NAME], check=False)
            print(f"127.0.0.1:{_PG_PORT} 已有服务在监听，移除旧的 caspian-postgres 容器并复用现有服务。")
            return
        else:
            _start_container_or_fail(["docker", "start", _PG_NAME])
    elif _port_open("127.0.0.1", _PG_PORT):
        print(f"127.0.0.1:{_PG_PORT} 已有服务在监听，直接复用作为数据库（不新建容器）。")
        return
    else:
        print("正在启动 PostgreSQL 容器（首次可能需拉取镜像，请稍候）...")
        _start_container_or_fail(
            [
                "docker", "run", "-d",
                "--name", _PG_NAME,
                "-e", f"POSTGRES_USER={_PG_USER}",
                "-e", f"POSTGRES_PASSWORD={_PG_PASSWORD}",
                "-e", f"POSTGRES_DB={_PG_DB}",
                "-p", f"127.0.0.1:{_PG_PORT}:5432",
                _PG_IMAGE,
            ]
        )
    _wait_postgres()
    # 启用 vector 扩展（幂等；仅当托管容器存在时执行）
    if _run(["docker", "inspect", _PG_NAME]).returncode == 0:
        _run(
            [
                "docker", "exec", _PG_NAME,
                "psql", "-U", _PG_USER, "-d", _PG_DB,
                "-c", "CREATE EXTENSION IF NOT EXISTS vector;",
            ],
            check=False,
        )


def _wait_postgres(timeout_seconds: int = 180) -> None:
    """等待 PostgreSQL 容器就绪（轮询 `pg_isready`）；超时给出排查指引。"""
    import time

    print(f"等待 PostgreSQL 容器 {_PG_NAME} 就绪（最多 {timeout_seconds}s）...")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _run(["docker", "exec", _PG_NAME, "pg_isready", "-U", _PG_USER, "-d", _PG_DB]).returncode == 0:
            return
        if not _docker_daemon_running():
            raise RuntimeError("等待期间 Docker 守护进程停止工作，请检查 Docker Desktop。")
        time.sleep(2)
    raise RuntimeError(
        f"PostgreSQL 容器 {_PG_NAME} 在 {timeout_seconds}s 内未就绪。请排查："
        f"1) 确认 Docker 正在运行；2) 首次使用可先手动拉取镜像 `docker pull {_PG_IMAGE}`；"
        f"3) 查看容器日志 `docker logs {_PG_NAME}` 以定位原因。"
    )


def _run_start() -> int:
    ensure_home()
    missing = _missing_provider_keys()
    if missing:
        _print_setx_block(missing)
        return 1
    _ensure_jwt_secret()

    migrated = _migrate_legacy_requirements()
    if migrated:
        print(f"Migrated {migrated} legacy requirement/contract folder(s) to ~/.caspian/users/")

    _ensure_postgres()

    app_dir = caspian_app()
    migrations_dir = app_dir / "backend" / "packages" / "harness" / "caspian" / "persistence" / "migrations"
    alembic_ini = migrations_dir / "alembic.ini"
    if alembic_ini.exists():
        print("Running database migrations (alembic upgrade head)...")
        result = _run(
            [_venv_python(), "-m", "alembic", "-c", str(alembic_ini), "upgrade", "head"],
            cwd=str(migrations_dir),
        )
        if result.returncode != 0:
            print("Database migration failed:", (result.stderr or result.stdout).strip(), file=sys.stderr)
            return 1

    print("Starting Caspian Gateway at http://127.0.0.1:8000  (Ctrl+C to stop)")
    _run([_venv_python(), "run_dev.py"], cwd=str(app_dir), capture=False)
    return 0


def _run_update() -> int:
    print("Updating Caspian...")
    app_dir = caspian_app()
    if not (app_dir / ".git").exists() or not os.path.isdir(app_dir):
        print("~/.caspian/app 不是已克隆的仓库；请先运行安装器或 `git clone`。", file=sys.stderr)
        return 1

    try:
        # fetch 需要 远程名 + 分支（或只远程名）；传 `origin/main` 会被当作远程名而报
        # `'origin/main' does not appear to be a git repository`。
        _run(["git", "-C", str(app_dir), "fetch", "origin", "main"], check=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() or str(exc)
        print("git fetch 失败:", detail, file=sys.stderr)
        return 1
    old = _run(["git", "-C", str(app_dir), "rev-parse", "HEAD"]).stdout.strip()
    _run(["git", "-C", str(app_dir), "reset", "--hard", _UPSTREAM], check=False)
    new = _run(["git", "-C", str(app_dir), "rev-parse", "HEAD"]).stdout.strip()
    print(f"{old[:7]} → {new[:7]}")

    harness = app_dir / "backend" / "packages" / "harness"
    print("Updating dependencies...")
    result = _run(
        [_venv_python(), "-m", "pip", "install", "-e", f"{harness}[runtime,postgres]"],
        cwd=str(app_dir),
    )
    if result.returncode != 0:
        print("Dependency install failed:", (result.stderr or result.stdout).strip(), file=sys.stderr)
        return 1
    print("Caspian is up to date.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="caspian",
        description="Caspian — 启动网关或更新到最新 main。",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("start", help="启动 Caspian Gateway（默认）")
    sub.add_parser("update", help="更新到 GitHub main 并同步依赖")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "start"
    if command == "update":
        return _run_update()
    return _run_start()


if __name__ == "__main__":
    sys.exit(main())
