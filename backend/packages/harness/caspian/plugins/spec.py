"""
本文件定义插件系统的接口规范（InterfaceSpec）、插件实现契约（PluginImplementation / PluginBundle）
与实现校验函数，是"系统只提供接口"的落点。

对外提供:
    InterfaceKind — 接口语义类别枚举
    InterfaceSpec — 系统标准接口规范（含失败策略与默认超时）
    PluginImplementation — 插件提交的单个接口实现声明
    PluginBundle — 插件入口返回的声明清单（display_name / version / requires / implementations）
    resolve_interface — 按 id 解析接口规范（含 service:<name> 动态接口）
    validate_implementation — 校验单个实现声明，返回错误文本或 None

输入:
    resolve_interface(interface: str) — 接口 id；tool / before_agent / before_model / before_tool /
        after_model / after_tool 为系统固定接口，service:<name> 为按名注册的服务接口。
    validate_implementation(impl) — PluginImplementation 实例。

输出:
    resolve_interface → InterfaceSpec | None（未知接口返回 None，对应 Unsupported Extension Interface）
    validate_implementation → str | None（None 表示通过，str 为不兼容原因）

具体工作流:
    (1) v1 系统接口集: tool（能力加法）、before_agent / before_model / before_tool（有序链+可修改）、
        after_model / after_tool（有序链只读观察）、service:<name>（默认单实现，可声明多实现）
    (2) 校验顺序: 接口存在性 → provider 形态（tool 需含 name/description；Hook 需可调用；
        service 为任意对象）→ 观察接口强制 read_only=True
    (3) tool 与既有工具同名冲突在注入/汇集期检测（见 registry / tools），此处不做

示例:
    spec = resolve_interface("before_model")      # → InterfaceSpec(kind=ordered_mutator, ...)
    resolve_interface("service:attachment")       # → 默认服务接口规范
    resolve_interface("my-unknown-hook")          # → None
    error = validate_implementation(impl)         # → None 或 "provider 形态不符: ..."
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class InterfaceKind(str, Enum):
    """接口语义类别：能力加法 / 有序链可修改 / 有序链只读观察 / 服务注册表。"""

    TOOL = "tool"
    ORDERED_MUTATOR = "ordered_mutator"
    ORDERED_OBSERVER = "ordered_observer"
    SERVICE = "service"


class InterfaceSpec(BaseModel):
    """系统标准接口规范。失败策略与超时由系统定义，插件不可改写。

    超时保护仅对异步实现生效（asyncio.wait_for 无法中断同步调用）；
    同步实现被调用方直接执行，插件方应优先提供异步实现。
    """

    id: str
    kind: InterfaceKind
    failure_policy: str = "skip"  # skip=跳过该实现继续 / abort=终止当前操作
    timeout_seconds: float = 30.0


# v1 系统固定接口集（失败策略全部为 skip，超时默认 30s）
_SYSTEM_INTERFACES: dict[str, InterfaceSpec] = {
    "tool": InterfaceSpec(id="tool", kind=InterfaceKind.TOOL, timeout_seconds=0),
    "before_agent": InterfaceSpec(id="before_agent", kind=InterfaceKind.ORDERED_MUTATOR),
    "before_model": InterfaceSpec(id="before_model", kind=InterfaceKind.ORDERED_MUTATOR),
    "before_tool": InterfaceSpec(id="before_tool", kind=InterfaceKind.ORDERED_MUTATOR),
    "after_model": InterfaceSpec(id="after_model", kind=InterfaceKind.ORDERED_OBSERVER),
    "after_tool": InterfaceSpec(id="after_tool", kind=InterfaceKind.ORDERED_OBSERVER),
}

_SERVICE_PREFIX = "service:"

_MUTATOR_INTERFACES = frozenset({
    "before_agent",
    "before_model",
    "before_tool",
})
_OBSERVER_INTERFACES = frozenset({
    "after_model",
    "after_tool",
})


def resolve_interface(interface: str) -> InterfaceSpec | None:
    """按 id 解析接口规范；未知接口返回 None。"""
    if interface in _SYSTEM_INTERFACES:
        return _SYSTEM_INTERFACES[interface]
    if interface.startswith(_SERVICE_PREFIX) and len(interface) > len(_SERVICE_PREFIX):
        return InterfaceSpec(id=interface, kind=InterfaceKind.SERVICE)
    return None


class PluginImplementation(BaseModel):
    """插件提交的单个接口实现声明。"""

    interface: str
    provider: Any = None
    order: int = 0
    read_only: bool = False
    multi: bool = False
    timeout_seconds: float | None = None


class PluginBundle(BaseModel):
    """插件入口返回的声明清单。"""

    display_name: str = ""
    version: str = ""
    requires: list[str] = Field(default_factory=list)
    implementations: list[PluginImplementation] = Field(default_factory=list)


def validate_implementation(impl: PluginImplementation) -> str | None:
    """校验单个实现声明，返回不兼容原因或 None。"""
    spec = resolve_interface(impl.interface)
    if spec is None:
        return f"Unsupported Extension Interface: {impl.interface}"

    if spec.kind is InterfaceKind.TOOL:
        provider = impl.provider
        if provider is None or not (
            isinstance(getattr(provider, "name", None), str)
            and isinstance(getattr(provider, "description", None), str)
        ):
            return f"tool 接口的 provider 必须是含 name/description 的 Tool 对象"
    elif spec.kind in (InterfaceKind.ORDERED_MUTATOR, InterfaceKind.ORDERED_OBSERVER):
        if not callable(impl.provider):
            return f"{impl.interface} 接口的 provider 必须是可调用对象"
        if impl.interface in _OBSERVER_INTERFACES:
            impl.read_only = True
    return None
