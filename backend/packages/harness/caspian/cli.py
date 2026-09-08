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


def _print_setx_block(missing: list[str]) -> None:
    print("Missing required API key(s): " + ", ".join(missing))
    print("Set them once like this (then open a NEW terminal and re-run `caspian`):")
    print('setx OPENAI_API_KEY      "<your DeepSeek key>"')
    print('setx DASHSCOPE_API_KEY   "<your DashScope key>"')
    print('setx OPENAI_BASE_URL     "https://api.deepseek.com"   # optional')
    print('setx OPENAI_MODEL        "deepseek-v4-flash-vision-exp"   # optional')
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


def _run(cmd: list[str], cwd: str | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, env=env, check=False)


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _ensure_postgres() -> None:
    """幂等启动 PostgreSQL(pgvector) 容器；返回 None（失败抛 RuntimeError）。"""
    if not _docker_available():
        raise RuntimeError(
            "未检测到 Docker。Caspian 需要 Docker 来运行 PostgreSQL(pgvector) 数据库。"
            "请安装 Docker 后重试，或自行启动一个位于 127.0.0.1:7221 且含 vector 扩展的 PostgreSQL。"
        )
    has_container = _run(["docker", "inspect", _PG_NAME]).returncode == 0
    if has_container:
        state = _run(["docker", "inspect", "-f", "{{.State.Running}}", _PG_NAME]).stdout.strip()
        if state == "true":
            _wait_postgres()
            return
        _run(["docker", "start", _PG_NAME], check=False)
    else:
        _run(
            [
                "docker", "run", "-d",
                "--name", _PG_NAME,
                "-e", f"POSTGRES_USER={_PG_USER}",
                "-e", f"POSTGRES_PASSWORD={_PG_PASSWORD}",
                "-e", f"POSTGRES_DB={_PG_DB}",
                "-p", f"127.0.0.1:{_PG_PORT}:5432",
                _PG_IMAGE,
            ],
            check=False,
        )
    _wait_postgres()
    # 启用 vector 扩展（幂等）
    _run(
        [
            "docker", "exec", _PG_NAME,
            "psql", "-U", _PG_USER, "-d", _PG_DB,
            "-c", "CREATE EXTENSION IF NOT EXISTS vector;",
        ],
        check=False,
    )


def _wait_postgres(timeout_seconds: int = 60) -> None:
    """等待 PostgreSQL 容器就绪（轮询 `pg_isready`）。"""
    import time

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _run(["docker", "exec", _PG_NAME, "pg_isready", "-U", _PG_USER, "-d", _PG_DB]).returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError(f"PostgreSQL 容器 {_PG_NAME} 在 {timeout_seconds}s 内未就绪")


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
    _run([_venv_python(), "run_dev.py"], cwd=str(app_dir))
    return 0


def _run_update() -> int:
    print("Updating Caspian...")
    app_dir = caspian_app()
    if not (app_dir / ".git").exists() or not os.path.isdir(app_dir):
        print("~/.caspian/app 不是已克隆的仓库；请先运行安装器或 `git clone`。", file=sys.stderr)
        return 1

    try:
        _run(["git", "-C", str(app_dir), "fetch", _UPSTREAM], check=True)
    except subprocess.CalledProcessError as exc:
        print("git fetch 失败:", exc, file=sys.stderr)
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
