"""
本文件定义 LocalSandbox 类。

对外提供:
    LocalSandbox(Sandbox): 本机文件系统下的沙箱实现

输入:
    LocalSandbox.__init__(thread_id): 线程标识，用于隔离不同 thread 的沙箱目录

工作流:
    __init__ (初始化即建目录):
    (1) 根据 thread_id 构建真实路径根目录 .lead_agent/threads/<thread_id>/user-data/
    (2) 调用 os.makedirs 创建根目录及 workspace/uploads/outputs 三个子目录

    _resolve_path (受保护 helper):
    调用 path_utils.resolve_path 完成虚拟路径 → 真实路径映射

    read_file / write_file:
    各自调用 _resolve_path 解析路径后执行对应操作

    run_shell:
    在 workspace 子目录下执行 shell 命令并返回结果

示例:
    sandbox = LocalSandbox(thread_id="abc123")
    content = sandbox.read_file("/mnt/user-data/workspace/hello.py")
"""

import os
import subprocess

from lead_agent.sandbox.base import Sandbox
from lead_agent.sandbox.path_utils import REAL_ROOT, SUBDIRS, resolve_path


class LocalSandbox(Sandbox):

    def __init__(self, thread_id: str):
        self._thread_id = thread_id
        self._real_root = os.path.abspath(REAL_ROOT.format(thread_id=thread_id))
        for sub in SUBDIRS:
            os.makedirs(os.path.join(self._real_root, sub), exist_ok=True)

    def _resolve_path(self, vpath: str) -> str:
        return resolve_path(vpath, self._thread_id)

    def read_file(self, path: str) -> str:
        real_path = self._resolve_path(path)
        with open(real_path, "r", encoding="utf-8") as f:
            return f.read()

    def write_file(self, path: str, content: str) -> None:
        real_path = self._resolve_path(path)
        os.makedirs(os.path.dirname(real_path), exist_ok=True)
        with open(real_path, "w", encoding="utf-8") as f:
            f.write(content)

    def run_shell(self, command: str) -> str:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=os.path.join(self._real_root, "workspace"),
        )
        output = result.stdout
        if result.stderr:
            output += result.stderr
        return output
