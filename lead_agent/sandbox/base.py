"""
本文件定义 Sandbox 抽象基类。

对外提供:
    Sandbox(ABC): 沙箱环境抽象基类，声明 read_file / write_file / run_shell 三个抽象方法

子类必须实现三个抽象方法，以提供具体的沙箱执行能力。
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
    def run_shell(self, command: str) -> str:
        ...
