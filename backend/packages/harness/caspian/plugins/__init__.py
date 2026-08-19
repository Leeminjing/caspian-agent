"""
本文件为插件子系统包入口，重导出对外核心对象。

对外提供:
    PluginRuntime — 插件运行时（注入、生命周期、状态、撤销）
    PluginImplementation / PluginBundle — 插件实现契约与清单
    get_plugin_runtime / set_plugin_runtime — 运行时进程级单例访问
    PluginError 及其子类 — 插件错误模型
"""
