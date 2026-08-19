"""
本文件提供 PluginRegistry 注入注册表：实现注入、单实现冲突检测、稳定排序、依赖解析与撤销重解析。

对外提供:
    PluginRegistry — 插件实现注册表（进程内唯一）
    _Entry — 注册条目（插件名、归属、实现声明、注入序号）

输入:
    inject(plugin, owner, implementations) — 插件名、归属（None=public，否则 user_id）、实现列表
    revoke(plugin, owner) — 要撤销的插件名与归属
    requires_missing(requires) — 依赖接口名列表
    hook_chain(interface, user_id) / tool_entries(user_id) / snapshot(user_id) — 装配快照

输出:
    inject → list[str | None]（与实现列表等长，None 表示注入成功，str 为拒绝原因）
    revoke → set[str]（受影响接口 id，供上层触发状态重算）
    requires_missing → list[str]（缺失的依赖接口名）
    hook_chain / tool_entries / snapshot → 按稳定顺序（注入序号）排列的实现视图

具体工作流:
    (1) 注入: 逐实现校验（validate_implementation）→ tool/Hook 直接按序登记；
        service 按单/多语义处理（见下）
    (2) service 语义: 首个实现 multi=True 时接口进入多实现模式，后续实现共存；
        否则为单实现，已有实现时新实现进入待定队列并报告 Injection Conflict
    (3) 撤销: 移除该插件全部条目，对受影响接口重解析（单实现空位由最早待定实现补入）
    (4) 排序: 全局自增注入序号保证顺序稳定（插件名排序加载 + 实现声明顺序）

示例:
    registry = PluginRegistry()
    errors = registry.inject("vision", None, [impl1, impl2])   # → [None, None]
    registry.requires_missing(["service:attachment"])          # → ["service:attachment"]
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from caspian.plugins.spec import (
    InterfaceKind,
    PluginImplementation,
    resolve_interface,
    validate_implementation,
)

logger = logging.getLogger(__name__)


@dataclass
class _Entry:
    """注册条目：注入序号 seq 决定接口内的稳定顺序。"""

    plugin: str
    owner: str | None  # None=public，否则为 user_id
    impl: PluginImplementation
    seq: int


@dataclass
class PluginRegistry:
    """插件实现注册表。owner 过滤使 public + 指定用户的 custom 实现形成装配快照。"""

    _entries: dict[str, list[_Entry]] = field(default_factory=dict)
    _pending: dict[str, list[_Entry]] = field(default_factory=dict)  # service 冲突待定队列
    _seq: int = 0

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _interface_entries(self, interface: str) -> list[_Entry]:
        return self._entries.setdefault(interface, [])

    def _service_multi_mode(self, interface: str) -> bool:
        return any(e.impl.multi for e in self._interface_entries(interface))

    def _re_resolve_service(self, interface: str) -> None:
        """撤销后重解析：单实现空位由最早待定实现补入，多实现模式下待定全部补入。"""
        pending = self._pending.pop(interface, [])
        if not pending:
            return
        entries = self._interface_entries(interface)
        if self._service_multi_mode(interface):
            for entry in sorted(pending, key=lambda e: e.seq):
                entries.append(entry)
                logger.info("服务接口 %s 多实现补入: %s", interface, entry.plugin)
        elif not entries:
            first = min(pending, key=lambda e: e.seq)
            entries.append(first)
            logger.info("服务接口 %s 单实现补入: %s", interface, first.plugin)

    def inject(
        self,
        plugin: str,
        owner: str | None,
        implementations: list[PluginImplementation],
    ) -> list[str | None]:
        """逐实现校验并注入；返回与实现列表等长的结果（None=成功，str=拒绝原因）。"""
        results: list[str | None] = []
        for impl in implementations:
            error = validate_implementation(impl)
            if error is not None:
                results.append(error)
                continue
            spec = resolve_interface(impl.interface)
            entry = _Entry(plugin, owner, impl, self._next_seq())
            if spec.kind is InterfaceKind.SERVICE:
                entries = self._interface_entries(impl.interface)
                if entries and not self._service_multi_mode(impl.interface):
                    self._pending.setdefault(impl.interface, []).append(entry)
                    current = ", ".join(e.plugin for e in entries)
                    results.append(
                        f"Injection Conflict: interface={impl.interface}, "
                        f"current={current}, new={plugin}"
                    )
                    continue
                entries.append(entry)
            else:
                self._interface_entries(impl.interface).append(entry)
            results.append(None)
        return results

    def revoke(self, plugin: str, owner: str | None) -> set[str]:
        """撤销插件的全部实现，返回受影响接口集合并触发重解析。"""
        affected: set[str] = set()
        for interface, entries in self._entries.items():
            kept = [e for e in entries if not (e.plugin == plugin and e.owner == owner)]
            if len(kept) != len(entries):
                self._entries[interface] = kept
                affected.add(interface)
        for interface, pending in self._pending.items():
            self._pending[interface] = [
                e for e in pending if not (e.plugin == plugin and e.owner == owner)
            ]
        for interface in list(affected):
            if interface.startswith("service:"):
                self._re_resolve_service(interface)
        return affected

    def requires_missing(self, requires: list[str]) -> list[str]:
        """按接口解析依赖：service 依赖看是否有已注入实现，系统接口恒满足。"""
        missing: list[str] = []
        for interface in requires:
            spec = resolve_interface(interface)
            if spec is None or spec.kind is not InterfaceKind.SERVICE:
                continue
            if not self._interface_entries(interface):
                missing.append(interface)
        return missing

    def _visible(self, user_id: str | None) -> list[_Entry]:
        """public 实现 + 指定用户 custom 实现。"""
        entries = [e for lst in self._entries.values() for e in lst]
        return [e for e in entries if e.owner is None or e.owner == user_id]

    def hook_chain(self, interface: str, user_id: str | None = None) -> list[tuple[str, PluginImplementation]]:
        """返回接口的有序实现链（稳定顺序 = 注入序号）。"""
        entries = [e for e in self._interface_entries(interface) if e.owner is None or e.owner == user_id]
        return [(e.plugin, e.impl) for e in sorted(entries, key=lambda e: e.seq)]

    def tool_entries(
        self, user_id: str | None = None
    ) -> list[tuple[str, str | None, PluginImplementation]]:
        """返回 tool 接口的实现列表（稳定顺序）：(插件名, 归属, 实现声明)。"""
        entries = [e for e in self._interface_entries("tool") if e.owner is None or e.owner == user_id]
        return [(e.plugin, e.owner, e.impl) for e in sorted(entries, key=lambda e: e.seq)]

    def snapshot(self, user_id: str | None = None) -> dict[str, Any]:
        """装配快照：hooks（接口 → 有序实现）、tools、services。"""
        entries = self._visible(user_id)
        hooks: dict[str, list[tuple[str, PluginImplementation]]] = {}
        tools: list[tuple[str, PluginImplementation]] = []
        services: dict[str, list[tuple[str, PluginImplementation]]] = {}
        for entry in sorted(entries, key=lambda e: e.seq):
            spec = resolve_interface(entry.impl.interface)
            if spec is None:
                continue
            if spec.kind is InterfaceKind.TOOL:
                tools.append((entry.plugin, entry.impl))
            elif spec.kind is InterfaceKind.SERVICE:
                services.setdefault(entry.impl.interface, []).append((entry.plugin, entry.impl))
            else:
                hooks.setdefault(entry.impl.interface, []).append((entry.plugin, entry.impl))
        return {"hooks": hooks, "tools": tools, "services": services}
