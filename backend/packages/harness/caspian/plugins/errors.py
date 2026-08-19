"""
本文件定义插件系统的错误模型，所有插件相关错误统一归因到插件或接口，不进入系统异常分支。

对外提供:
    PluginError — 插件错误基类
    UnsupportedExtensionInterface — 接口不存在（携带接口名）
    InjectionConflict — 单实现接口冲突（携带接口名/当前实现/新实现）
    ExtensionTimeout — 插件实现超时（携带插件名/接口名/超时秒数）
    PluginEnvironmentError — 插件自身运行环境不可用
    PluginConfigError — 插件配置错误

输入:
    各异常构造参数见类定义；PluginEnvironmentError / PluginConfigError 为插件入口抛出的
    语义化异常，系统据此把插件状态置为 Unavailable。

输出:
    异常对象，message 为可读中文描述，供插件状态视图与日志直接展示。

示例:
    raise UnsupportedExtensionInterface("my-unknown-hook")
    raise InjectionConflict("service:primary-storage", "storage-a", "storage-b")
"""


class PluginError(Exception):
    """插件系统错误基类。"""


class UnsupportedExtensionInterface(PluginError):
    """插件声明了系统不存在的接口。"""

    def __init__(self, interface: str) -> None:
        self.interface = interface
        super().__init__(f"Unsupported Extension Interface: {interface}")


class InjectionConflict(PluginError):
    """单实现接口已存在实现，新实现不得静默覆盖。"""

    def __init__(self, interface: str, current: str, new: str) -> None:
        self.interface = interface
        self.current = current
        self.new = new
        super().__init__(
            f"Injection Conflict: interface={interface}, current={current}, new={new}"
        )


class ExtensionTimeout(PluginError):
    """插件实现超过接口允许的响应时间。"""

    def __init__(self, plugin: str, interface: str, timeout: float) -> None:
        self.plugin = plugin
        self.interface = interface
        self.timeout = timeout
        super().__init__(
            f"Extension Timeout: plugin={plugin}, interface={interface}, "
            f"timeout={timeout}s"
        )


class PluginEnvironmentError(PluginError):
    """插件自身运行环境不可用（由插件入口抛出）。"""


class PluginConfigError(PluginError):
    """插件配置无效（由插件入口抛出）。"""
