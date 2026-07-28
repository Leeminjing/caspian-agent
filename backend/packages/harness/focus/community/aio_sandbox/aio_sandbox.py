"""
本文件对外提供 AioSandbox 类，实现 Sandbox 抽象基类，通过 agent-sandbox SDK 与容器内 AIO 服务通信。

对外提供:
    AioSandbox(Sandbox) — Docker 容器沙箱实现

输入:
    __init__(user_id: str, thread_id: str)

输出:
    AioSandbox 实例

具体工作流:
    read_file(path) → str:
    (1) 虚拟路径直接传给 SDK（bind mount 使其在容器内直接可用）
    (2) SDK 向容器内 AIO 服务发送 HTTP 请求，返回文件内容

    write_file(path, content) → None:
    (1) 同上，SDK 将内容写入容器内文件

    run_shell(command, shell_type) → str:
    (1) 拼接 cd /mnt/user-data/workspace && 前缀
    (2) 不调用 validate_shell_command（Docker 提供文件系统隔离）
    (3) SDK 向容器内 AIO 服务发送 HTTP 请求，返回执行结果

三个方法均为纯同步。LangChain BaseTool._arun() 默认 run_in_executor 自动线程池。

示例:
    from focus.community.aio_sandbox.aio_sandbox import AioSandbox

    sandbox = AioSandbox(user_id="uuid-xxx", thread_id="thread-1")
    content = sandbox.read_file("/mnt/user-data/workspace/hello.py")
    sandbox.run_shell("ls -la", shell_type="bash")
"""

import logging

from focus.sandbox.base import Sandbox

logger = logging.getLogger(__name__)


class AioSandbox(Sandbox):

    def __init__(self, user_id: str, thread_id: str, sdk_client: object) -> None:
        self._user_id = user_id
        self._thread_id = thread_id
        self._sdk_client = sdk_client

    @staticmethod
    def _cd_prefix(command: str) -> str:
        return f"cd /mnt/user-data/workspace && {command}"

    def read_file(self, path: str) -> str:
        result = self._sdk_client.file.read_file(file=path)
        if hasattr(result, 'data') and hasattr(result.data, 'content'):
            return result.data.content
        return str(result)

    def write_file(self, path: str, content: str) -> None:
        self._sdk_client.file.write_file(file=path, content=content)

    def run_shell(self, command: str, shell_type: str) -> str:
        cd_command = self._cd_prefix(command)
        result = self._sdk_client.shell.exec_command(command=cd_command)
        if hasattr(result, 'stdout'):
            output = result.stdout or ''
            if hasattr(result, 'stderr') and result.stderr:
                output += result.stderr
            return output
        return str(result)
