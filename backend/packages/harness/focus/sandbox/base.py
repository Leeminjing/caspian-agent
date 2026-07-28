"""
本文件定义 Sandbox 抽象基类。

对外提供:
    Sandbox(ABC): 沙箱环境抽象基类，声明 read_file / write_file / run_shell 三个抽象方法

输入:
    read_file(path): 虚拟路径，根据扩展名自动分发解析器（PDF/DOCX/DOC/文本）
    write_file(path, content): 虚拟路径 + 文件内容
    run_shell(command, shell_type): shell 命令字符串 + shell 类型（bash/powershell/cmd/sh）

工作流:
    子类必须实现三个抽象方法，以提供具体的沙箱执行能力。
    read_file 返回提取的文本内容，格式差异由子类实现处理。

示例:
    sandbox = LocalSandbox(thread_id="abc123")
    sandbox.run_shell("ls -la", shell_type="bash")
    text = sandbox.read_file("/mnt/user-data/uploads/report.pdf")
"""

from abc import ABC, abstractmethod


class Sandbox(ABC):

    @abstractmethod
    def read_file(self, path: str) -> str:
        ...

    @abstractmethod
    def write_file(self, path: str, content: str) -> None:
        ...

    @abstractmethod
    def run_shell(self, command: str, shell_type: str) -> str:
        ...
