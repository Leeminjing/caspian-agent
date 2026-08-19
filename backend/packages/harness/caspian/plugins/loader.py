"""
本文件提供插件目录发现与入口模块加载，是插件接入的唯一系统侧通道。

对外提供:
    PLUGINS_PUBLIC_REAL_ROOT — public 插件目录 "plugins"
    PLUGINS_CUSTOM_REAL_ROOT — custom 插件目录模板 ".caspian/users/{user_id}/plugins"
    iter_plugin_dirs — 扫描插件目录，返回 (名称, 目录, 归属) 列表（按名称排序，顺序稳定）
    load_plugin_module — 加载插件目录下的 plugin.py 入口模块
    call_entry — 调用入口 create_implementations(config)，返回 PluginBundle

输入:
    iter_plugin_dirs(user_id: str | None) — user_id 非 None 时同时扫描该用户的 custom 目录
    load_plugin_module(plugin_dir: Path) — 插件目录路径
    call_entry(module, config: dict) — 已加载入口模块与该插件的专属配置

输出:
    iter_plugin_dirs → list[tuple[str, Path, str | None]]（归属为 None=public，否则为 user_id）
    load_plugin_module → ModuleType
    call_entry → PluginBundle（awaitable 时自动 await）

具体工作流:
    (1) 发现: 遍历 public + custom 目录的直接子目录，存在 plugin.py（进程内入口）或
        plugin.json（远程入口）的视为插件；双入口并存由 runtime 拒绝
    (2) 名称仅允许安全段字符，非法名称跳过并记日志
    (3) 加载: importlib 按文件路径加载入口模块，模块名带 plugin_ 前缀避免与包冲突
    (4) 调用: 取 create_implementations 函数，传入插件专属配置；同步/异步入口都支持
    (5) 入口缺失/执行异常统一转为 PluginEnvironmentError / PluginConfigError 语义上抛

示例:
    for name, dir, owner in iter_plugin_dirs():
        module = load_plugin_module(dir)
        bundle = await call_entry(module, {"api_key": "xxx"})
"""

import importlib.util
import inspect
import logging
import re
from pathlib import Path
from types import ModuleType

from caspian.plugins.errors import PluginConfigError, PluginEnvironmentError
from caspian.plugins.spec import PluginBundle

logger = logging.getLogger(__name__)

PLUGINS_PUBLIC_REAL_ROOT = "plugins"
PLUGINS_CUSTOM_REAL_ROOT = ".caspian/users/{user_id}/plugins"

_ENTRY_FILE = "plugin.py"
_MANIFEST_FILE = "plugin.json"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def _iter_dir(root: Path, owner: str | None) -> list[tuple[str, Path, str | None]]:
    if not root.is_dir():
        return []
    result: list[tuple[str, Path, str | None]] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        if not _SAFE_NAME.fullmatch(child.name):
            logger.warning("插件目录名包含非法字符，跳过: %s", child)
            continue
        # 入口二选一: plugin.py（进程内）或 plugin.json（远程）；双入口冲突由 runtime 拒绝
        if (child / _ENTRY_FILE).is_file() or (child / _MANIFEST_FILE).is_file():
            result.append((child.name, child, owner))
    return result


def iter_plugin_dirs(user_id: str | None = None) -> list[tuple[str, Path, str | None]]:
    """扫描 public（及可选 custom）插件目录，返回按名称排序的插件目录列表。"""
    entries = _iter_dir(Path(PLUGINS_PUBLIC_REAL_ROOT), None)
    if user_id:
        entries.extend(_iter_dir(Path(PLUGINS_CUSTOM_REAL_ROOT.format(user_id=user_id)), user_id))
    return sorted(entries, key=lambda item: (item[2] is not None, item[0]))


def load_plugin_module(plugin_dir: Path) -> ModuleType:
    """按文件路径加载插件入口模块。"""
    entry = plugin_dir / _ENTRY_FILE
    module_name = f"caspian_plugin_{plugin_dir.name}"
    spec = importlib.util.spec_from_file_location(module_name, entry)
    if spec is None or spec.loader is None:
        raise PluginEnvironmentError("plugin.py 无法加载")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def call_entry(module: ModuleType, config: dict) -> PluginBundle:
    """调用插件入口 create_implementations(config)，返回 PluginBundle。"""
    create = getattr(module, "create_implementations", None)
    if create is None or not callable(create):
        raise PluginEnvironmentError("缺少 create_implementations 入口函数")
    try:
        result = create(config)
        if inspect.isawaitable(result):
            result = await result
    except (PluginConfigError, PluginEnvironmentError):
        raise
    except Exception as exc:
        raise PluginEnvironmentError(f"入口执行失败: {exc}") from None
    if not isinstance(result, PluginBundle):
        raise PluginEnvironmentError(
            f"create_implementations 必须返回 PluginBundle，实际为 {type(result).__name__}"
        )
    return result
