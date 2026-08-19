"""
本文件提供 PluginRuntime 插件运行时，作为组合根资源管理插件的发现、激活、状态与撤销。

对外提供:
    PluginStatus — 插件状态档案（state / reason / 注入清单 / 依赖）
    PluginRuntime — 插件运行时（注入、生命周期、状态、撤销、trace）
    get_plugin_runtime / set_plugin_runtime — 进程级单例访问（装配层经此取快照）

输入:
    PluginRuntime(extensions_config) — ExtensionsConfig 实例（enabled 开关与插件专属配置）
    load_public() — 加载 public 目录中声明 enabled 的插件
    ensure_user(user_id) — 惰性加载该用户 custom 目录中声明 enabled 的插件
    revoke(name, owner) — 撤销插件全部实现（远程插件进程被 shutdown 并终止）
    close() — 关闭全部远程插件进程（进程退出路径）
    status_payload(user_id) / snapshot(user_id) — 状态与装配快照

输出:
    load_public / ensure_user → None（状态写入 statuses）
    status_payload → list[dict]（REST 展示结构）
    snapshot → registry.snapshot（hooks / tools / services）

具体工作流:
    (1) 激活: 按入口类型分派——plugin.json → 远程进程（清单 → spawn → 握手 → 声明校验 →
        代理注入，v1 远程不支持 service 声明）；plugin.py → 进程内（调用
        create_implementations(专属配置)）；依赖检查（requires 缺失则 Unavailable 且不注入）→
        逐实现注入 → 状态置 Active（含 issues）
    (2) 失败语义: 入口抛 PluginConfigError → Invalid configuration；PluginEnvironmentError /
        其他异常 / 目录缺失 / 进程启动失败 → 插件自身运行环境不可用；均不影响系统与其他插件
    (3) 撤销: registry.revoke 移除全部实现并重解析受影响接口，状态移除；远程进程 shutdown

示例:
    runtime = PluginRuntime(get_extensions_config("extensions_config.json"))
    await runtime.load_public()
    await runtime.ensure_user(user_id)
    runtime.status_payload(user_id)
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from caspian.config.extensions_config import ExtensionsConfig
from caspian.plugins.errors import PluginConfigError, PluginEnvironmentError
from caspian.plugins.loader import call_entry, iter_plugin_dirs, load_plugin_module
from caspian.plugins.registry import PluginRegistry
from caspian.plugins.spec import PluginImplementation
from caspian.plugins.trace import TraceBuffer

logger = logging.getLogger(__name__)


@dataclass
class PluginStatus:
    """插件状态档案。"""

    name: str
    owner: str | None
    display_name: str = ""
    version: str = ""
    state: str = "active"  # active / unavailable
    reason: str = ""
    requires: list[str] = field(default_factory=list)
    missing_dependencies: list[str] = field(default_factory=list)
    injected: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "scope": "public" if self.owner is None else "custom",
            "display_name": self.display_name,
            "version": self.version,
            "state": self.state,
            "reason": self.reason,
            "requires": list(self.requires),
            "missing_dependencies": list(self.missing_dependencies),
            "injected": list(self.injected),
            "issues": list(self.issues),
        }


class PluginRuntime:
    """插件运行时：发现、激活、状态、撤销与装配快照。"""

    def __init__(self, extensions_config: ExtensionsConfig) -> None:
        self._config = extensions_config
        self.registry = PluginRegistry()
        self.trace = TraceBuffer()
        self._statuses: dict[tuple[str | None, str], PluginStatus] = {}
        self._remotes: dict[tuple[str | None, str], Any] = {}

    @staticmethod
    def _key(owner: str | None, name: str) -> tuple[str | None, str]:
        return (owner, name)

    async def _activate(self, name: str, plugin_dir, owner: str | None) -> None:
        """按入口类型分派激活：plugin.json → 远程进程，plugin.py → 进程内。"""
        key = self._key(owner, name)
        has_py = (plugin_dir / "plugin.py").is_file()
        has_json = (plugin_dir / "plugin.json").is_file()
        if has_py and has_json:
            self._statuses[key] = PluginStatus(
                name, owner, state="unavailable",
                reason="插件目录同时含 plugin.py 与 plugin.json，配置错误",
            )
            return
        if has_json:
            await self._activate_remote(name, plugin_dir, owner)
            return
        plugin_cfg = self._config.get_enabled_plugins().get(name)
        try:
            module = load_plugin_module(plugin_dir)
            bundle = await call_entry(module, dict(plugin_cfg.config) if plugin_cfg else {})
        except PluginConfigError as exc:
            self._statuses[key] = PluginStatus(
                name, owner, state="unavailable", reason=f"Invalid configuration: {exc}"
            )
            return
        except PluginEnvironmentError as exc:
            self._statuses[key] = PluginStatus(
                name, owner, state="unavailable", reason=f"插件自身运行环境不可用: {exc}"
            )
            return
        except Exception as exc:
            logger.error("插件 %s 加载失败: %s", name, exc, exc_info=True)
            self._statuses[key] = PluginStatus(
                name, owner, state="unavailable", reason=f"插件自身运行环境不可用: {exc}"
            )
            return

        missing = self.registry.requires_missing(list(bundle.requires))
        if missing:
            self._statuses[key] = PluginStatus(
                name,
                owner,
                display_name=bundle.display_name,
                version=bundle.version,
                state="unavailable",
                reason=f"Missing dependency: {', '.join(missing)}",
                requires=list(bundle.requires),
                missing_dependencies=missing,
            )
            logger.info("插件 %s 依赖缺失，未注入: %s", name, missing)
            return

        results = self.registry.inject(name, owner, list(bundle.implementations))
        injected = [
            impl.interface
            for impl, error in zip(bundle.implementations, results)
            if error is None
        ]
        issues = [error for error in results if error is not None]
        self._statuses[key] = PluginStatus(
            name,
            owner,
            display_name=bundle.display_name,
            version=bundle.version,
            state="active",
            requires=list(bundle.requires),
            injected=injected,
            issues=issues,
        )
        if issues:
            logger.warning("插件 %s 部分实现被拒绝: %s", name, issues)
        logger.info("插件 %s 已激活，注入 %d 个实现", name, len(injected))

    async def _activate_remote(self, name: str, plugin_dir, owner: str | None) -> None:
        """激活远程插件：清单 → spawn → 握手 → 声明校验 → 代理注入。"""
        from caspian.plugins.remote import (
            RemotePlugin,
            build_remote_hook,
            build_remote_tool,
            load_manifest,
        )
        from caspian.plugins.spec import InterfaceKind, resolve_interface

        key = self._key(owner, name)
        plugin_cfg = self._config.get_enabled_plugins().get(name)
        try:
            manifest = load_manifest(plugin_dir)
        except PluginConfigError as exc:
            self._statuses[key] = PluginStatus(
                name, owner, state="unavailable", reason=f"Invalid configuration: {exc}"
            )
            return

        plugin = RemotePlugin(manifest, plugin_dir, dict(plugin_cfg.config) if plugin_cfg else {})
        try:
            declarations = await plugin.start()
        except Exception as exc:
            logger.error("远程插件 %s 启动失败: %s", name, exc, exc_info=True)
            self._statuses[key] = PluginStatus(
                name, owner, state="unavailable",
                reason=f"插件自身运行环境不可用: {exc}",
            )
            await plugin.shutdown()
            return

        missing = self.registry.requires_missing(list(manifest.requires))
        if missing:
            await plugin.shutdown()
            self._statuses[key] = PluginStatus(
                name, owner, display_name=manifest.display_name,
                version=manifest.version, state="unavailable",
                reason=f"Missing dependency: {', '.join(missing)}",
                requires=list(manifest.requires), missing_dependencies=missing,
            )
            return

        implementations = []
        issues: list[str] = []
        for decl in declarations:
            spec = resolve_interface(decl.interface)
            if spec is None:
                issues.append(f"Unsupported Extension Interface: {decl.interface}")
                continue
            if spec.kind is InterfaceKind.SERVICE:
                issues.append(
                    f"远程插件不支持 service 接口: {decl.interface}（v1 范围限制）"
                )
                continue
            if decl.interface == "tool":
                if decl.tool is None or not decl.tool.name:
                    issues.append("tool 声明缺少 name/description/parameters")
                    continue
                provider = build_remote_tool(plugin, decl)
            else:
                provider = build_remote_hook(plugin, decl)
            implementations.append(PluginImplementation(
                interface=decl.interface,
                provider=provider,
                order=decl.order,
                read_only=decl.read_only,
                multi=decl.multi,
                timeout_seconds=decl.timeout_seconds,
            ))

        results = self.registry.inject(name, owner, implementations)
        injected = [
            impl.interface
            for impl, error in zip(implementations, results)
            if error is None
        ]
        issues.extend(error for error in results if error is not None)
        self._remotes[key] = plugin
        self._statuses[key] = PluginStatus(
            name, owner, display_name=manifest.display_name,
            version=manifest.version, state="active",
            requires=list(manifest.requires), injected=injected, issues=issues,
        )
        if issues:
            logger.warning("远程插件 %s 部分声明被拒绝: %s", name, issues)
        logger.info("远程插件 %s 已激活，注入 %d 个实现", name, len(injected))

    async def load_public(self) -> None:
        """加载 public 目录中声明 enabled 的插件。"""
        enabled = self._config.get_enabled_plugins()
        dirs = {name: dir for name, dir, _ in iter_plugin_dirs()}
        for name in sorted(enabled):
            if name in dirs:
                await self._activate(name, dirs[name], None)
            else:
                self._statuses[self._key(None, name)] = PluginStatus(
                    name, None, state="unavailable", reason="插件目录不存在"
                )

    async def ensure_user(self, user_id: str) -> None:
        """惰性加载该用户 custom 目录中声明 enabled 的插件（幂等）。"""
        enabled = self._config.get_enabled_plugins()
        dirs = {
            name: dir
            for name, dir, owner in iter_plugin_dirs(user_id)
            if owner == user_id
        }
        for name in sorted(enabled):
            if name not in dirs or self._key(user_id, name) in self._statuses:
                continue
            await self._activate(name, dirs[name], user_id)

    async def revoke(self, name: str, owner: str | None = None) -> None:
        """撤销插件全部实现并移除状态；远程插件的进程 SHALL 被 shutdown 并终止。"""
        self.registry.revoke(name, owner)
        self._statuses.pop(self._key(owner, name), None)
        remote = self._remotes.pop(self._key(owner, name), None)
        if remote is not None:
            await remote.shutdown()
        logger.info("插件 %s 已撤销 (scope=%s)", name, "public" if owner is None else owner)

    async def close(self) -> None:
        """关闭全部远程插件进程（进程退出路径）。"""
        for remote in list(self._remotes.values()):
            await remote.shutdown()
        self._remotes.clear()

    def report_issue(self, owner: str | None, name: str, message: str) -> None:
        """向插件状态 issues 追加一条问题（幂等，已存在同文案时跳过）。"""
        key = self._key(owner, name)
        status = self._statuses.get(key)
        if status is None:
            return
        if message not in status.issues:
            status.issues.append(message)

    def status_payload(self, user_id: str | None = None) -> list[dict[str, Any]]:
        """返回 public + 指定用户 custom 插件的状态列表（REST 展示结构）。"""
        result = []
        for (owner, name), status in sorted(self._statuses.items()):
            if owner is not None and owner != user_id:
                continue
            result.append(status.payload())
        return result

    def status_for(self, name: str, user_id: str | None = None) -> dict[str, Any] | None:
        """按名查单个插件状态；同名时 public 优先，其次当前用户 custom。"""
        for owner in (None, user_id):
            status = self._statuses.get(self._key(owner, name))
            if status is not None:
                return status.payload()
        return None

    def snapshot(self, user_id: str | None = None) -> dict[str, Any]:
        """装配快照：public + 指定用户 custom 实现的 hooks / tools / services。"""
        return self.registry.snapshot(user_id)


_runtime: PluginRuntime | None = None


def set_plugin_runtime(runtime: PluginRuntime | None) -> None:
    """设置进程级插件运行时单例（组合根 deps 调用）。"""
    global _runtime
    _runtime = runtime


def get_plugin_runtime() -> PluginRuntime | None:
    """获取进程级插件运行时单例；未初始化返回 None（消费层视为无插件）。"""
    return _runtime
