"""
本文件对外提供 Caspian 家目录（`~/.caspian`）的一次性解析函数，作为「程序代码 / 用户数据」
硬分隔与 cwd 无关配置加载的根。

对外提供:
    caspian_home()      → 家目录根（`CASPIAN_HOME` 环境变量优先，否则 `~/.caspian`）
    caspian_app()       → 托管程序代码目录（家目录 / app）
    caspian_users()     → 用户数据目录（家目录 / users）
    caspian_env_file()  → 运行时 .env 路径（家目录 / config / .env）
    ensure_home()       → best-effort 创建家目录与各子路径

输入: 无

输出:
    Path — 各路径的绝对 Path；`caspian_home()` 依 `CASPIAN_HOME`（或用户主目录）解析

具体工作流:
    (1) caspian_home 读 `CASPIAN_HOME`，已设则 `Path(raw).expanduser()`，否则 `Path.home()/".caspian"`
    (2) 其余函数基于 caspian_home() 派生
    (3) ensure_home 创建 home 及 app/users/config/runtime 子目录（幂等，失败仅日志）

示例:
    from caspian.runtime.home import caspian_home, caspian_users, caspian_env_file

    home = caspian_home()
    users = caspian_users()
    env = caspian_env_file()
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

ENV_HOME = "CASPIAN_HOME"

_DEFAULT_HOME = Path(".caspian")


def caspian_home() -> Path:
    """家目录根：`CASPIAN_HOME` 优先，未设则 `Path.home()/".caspian"`。"""
    raw = os.environ.get(ENV_HOME)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / _DEFAULT_HOME


def caspian_app() -> Path:
    """托管程序代码目录（含 git 仓库与 config.yaml）。"""
    return caspian_home() / "app"


def caspian_users() -> Path:
    """用户数据目录（沙箱数据、每线程决策表/任务合同、知识归档）。"""
    return caspian_home() / "users"


def caspian_env_file() -> Path:
    """运行时 .env 文件路径（`.env` 优先于进程工作目录）。"""
    return caspian_home() / "config" / ".env"


def ensure_home() -> None:
    """best-effort 创建家目录与 app/users/config/runtime 子路径（幂等）。

    输入: 无

    输出: None — 创建失败仅记录日志，不抛出（调用方自行处理缺失目录的后续错误）
    """
    for sub in ("", "app", "users", "config", "runtime"):
        try:
            (caspian_home() / sub).mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - best effort
            logger.warning("创建家目录失败 %s: %s", caspian_home() / sub, exc)
