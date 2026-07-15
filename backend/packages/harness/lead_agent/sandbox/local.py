"""
本文件定义 LocalSandbox 类。

对外提供:
    LocalSandbox(Sandbox): 本机文件系统下的沙箱实现

输入:
    LocalSandbox.__init__(user_id, thread_id): 用户标识+线程标识，用于按用户隔离沙箱目录

工作流:
    __init__ (初始化即建目录):
    (1) 根据 user_id + thread_id 构建真实路径根目录 .lead_agent/users/<user_id>/threads/<thread_id>/user-data/
    (2) 调用 os.makedirs 创建根目录及 workspace/uploads/outputs 三个子目录

    _resolve_path (受保护 helper):
    调用 path_utils.resolve_path 完成虚拟路径 → 真实路径映射

    read_file:
    (1) 调用 _resolve_path 解析路径
    (2) 根据扩展名分发至 readers 模块: .pdf → _read_pdf / .docx → _read_docx / .doc → _read_doc / 其他 → UTF-8
    (3) 所有异常通过 _sanitize_error 清洗真实路径后重新抛出

    _sanitize_error (受保护 helper):
    捕获异常，将异常信息中的真实路径替换为虚拟路径后重新抛出，不泄露磁盘路径

    write_file:
    调用 _resolve_path 解析路径后写入文件内容

    run_shell:
    在 workspace 子目录下以指定 shell 类型执行命令。
    (1) 校验 shell_type 是否在 SHELL_MAP 中
    (2) 调用 validate_shell_command(command) 做四维全局安全扫描（维度①③④）
    (3) 调用 validate_local_bash_cd_target(command, shell_type) 做 cd/pushd/popd 目标校验（维度②）
    (4) 通过 shutil.which 查找目标 shell 可执行文件
    (5) 以显式调用方式（shell=False）执行命令，cwd=workspace
    (6) 若 validate_shell_command 返回 warn 字符串，追加到命令输出末尾

    支持 bash / powershell / cmd / sh，通过 SHELL_MAP 查找对应可执行文件。
    找不到目标 shell 时抛出 RuntimeError。

示例:
    sandbox = LocalSandbox(user_id="uuid-xxx", thread_id="abc123")
    content = sandbox.read_file("/mnt/user-data/workspace/hello.py")
    text = sandbox.read_file("/mnt/user-data/uploads/report.pdf")
    sandbox.run_shell("ls -la", shell_type="bash")
"""

import logging
import os
import re
import shutil
import subprocess

from lead_agent.sandbox.base import Sandbox
from lead_agent.sandbox.path_utils import (
    REAL_ROOT,
    SUBDIRS,
    resolve_path,
    validate_shell_command,
)
from lead_agent.sandbox.readers import _read_pdf, _read_docx, _read_doc

logger = logging.getLogger(__name__)

SHELL_MAP = {
    "bash":       ("bash",            ["-c"]),
    "sh":         ("sh",              ["-c"]),
    "cmd":        ("cmd.exe",         ["/c"]),
    "powershell": ("powershell.exe",  ["-Command"]),
}

class LocalSandbox(Sandbox):

    def __init__(self, user_id: str, thread_id: str):
        self._user_id = user_id
        self._thread_id = thread_id
        self._real_root = os.path.abspath(REAL_ROOT.format(user_id=user_id, thread_id=thread_id))
        for sub in SUBDIRS:
            os.makedirs(os.path.join(self._real_root, sub), exist_ok=True)

    def _resolve_path(self, vpath: str) -> str:
        return resolve_path(vpath, self._user_id, self._thread_id)

    def read_file(self, path: str) -> str:
        real_path = self._resolve_path(path)
        ext = os.path.splitext(real_path)[1].lower()

        try:
            if ext == ".pdf":
                return _read_pdf(real_path)
            elif ext == ".docx":
                return _read_docx(real_path)
            elif ext == ".doc":
                return _read_doc(real_path)
            else:
                with open(real_path, "r", encoding="utf-8") as f:
                    return f.read()
        except Exception as e:
            raise self._sanitize_error(e, path, real_path) from None

    def write_file(self, path: str, content: str) -> None:
        real_path = self._resolve_path(path)
        os.makedirs(os.path.dirname(real_path), exist_ok=True)
        with open(real_path, "w", encoding="utf-8") as f:
            f.write(content)

    def run_shell(self, command: str, shell_type: str) -> str:
        entry = SHELL_MAP.get(shell_type)
        if entry is None:
            raise ValueError(
                f"不支持的 shell 类型: '{shell_type}'，"
                f"有效值: {sorted(SHELL_MAP.keys())}"
            )

        # shell 命令四维路径安全检查（防线 0→1→2→3→4，维度③ warn）
        warn_msg = validate_shell_command(command, shell_type)

        exe_name, args = entry
        exe_path = shutil.which(exe_name)
        if exe_path is None:
            raise RuntimeError(f"Shell '{shell_type}' not found in PATH (looked for: {exe_name})")
        result = subprocess.run(
            [exe_path, *args, command],
            shell=False,
            capture_output=True,
            text=True,
            cwd=os.path.join(self._real_root, "workspace"),
        )
        output = result.stdout
        if result.stderr:
            output += result.stderr

        # 追加 PATH= 风险提示
        if warn_msg:
            output += f"\n{warn_msg}"

        return output

    def _sanitize_error(self, error: Exception, vpath: str, real_path: str) -> Exception:
        """清洗异常信息中的真实路径，替换为虚拟路径。"""
        msg = str(error)
        # 统一用 os.path.normpath 归一化斜杠方向后再替换
        norm_path = os.path.normpath(real_path)
        norm_root = os.path.normpath(self._real_root)
        for candidate in (real_path, norm_path, self._real_root, norm_root):
            if candidate in msg:
                msg = msg.replace(candidate, vpath)
        # 兜底：正则替换含 ".lead_agent" 的路径片段
        msg = re.sub(r'\S*\.lead_agent\S*', '[sandbox]', msg)
        return type(error)(f"读取文件失败: {vpath}: {msg}")
