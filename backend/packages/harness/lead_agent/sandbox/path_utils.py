"""
本文件提供虚拟路径与真实路径之间的映射函数及越界校验。

对外提供:
    VRROOT: 虚拟路径前缀常量 "/mnt/user-data"
    REAL_ROOT: 真实路径根模板 ".lead_agent/threads/{thread_id}/user-data"
    SUBDIRS: 预创子目录列表 ["workspace", "uploads", "outputs"]
    resolve_path: 输入虚拟路径 + user_id + thread_id，输出真实磁盘路径
    validate_path: 校验解析后的真实路径是否在沙箱根目录内，越界抛 SecurityError
    validate_subdir: 校验虚拟路径的第一级子目录是否在白名单内，越界抛 SecurityError

工作流:
    resolve_path:
    (1) 校验虚拟路径必须以 VRROOT 开头，否则抛 SecurityError
    (2) 根据 user_id + thread_id 构建真实根目录
    (3) 将虚拟路径去掉 VRROOT 前缀，拼接到真实根目录后
    (4) 调用 validate_path 二次确认未越界
    (5) 返回真实路径

示例:
    resolve_path("/mnt/user-data/workspace/script.py", "uuid-xxx", "abc123")
    → ".lead_agent/users/uuid-xxx/threads/abc123/user-data/workspace/script.py"
"""

import os


VRROOT = "/mnt/user-data"
REAL_ROOT = ".lead_agent/users/{user_id}/threads/{thread_id}/user-data"
SUBDIRS = ["workspace", "uploads", "outputs"]


class SecurityError(Exception):
    pass


def validate_subdir(vpath: str, allowed: set[str]) -> None:
    """校验虚拟路径的第一级子目录是否在白名单内，否则抛 SecurityError。

    输入:
        vpath: 完整虚拟路径，如 "/mnt/user-data/workspace/foo.py"
        allowed: 允许的子目录名集合，如 {"workspace", "outputs"}

    工作流:
        (1) 去掉 VRROOT 前缀，取剩余部分的第一级目录名
        (2) 若该目录名不在 allowed 中，抛 SecurityError

    示例:
        validate_subdir("/mnt/user-data/uploads/a.txt", {"uploads", "workspace"})  # 通过
        validate_subdir("/mnt/user-data/outputs/b.txt", {"uploads", "workspace"})  # 抛 SecurityError
    """
    relative = vpath[len(VRROOT):].lstrip("/")
    subdir = relative.split("/")[0]
    if subdir not in allowed:
        raise SecurityError(
            f"子目录 '{subdir}' 不在允许列表中: {sorted(allowed)}"
        )


def resolve_path(vpath: str, user_id: str, thread_id: str) -> str:
    if not vpath.startswith(VRROOT + "/"):
        raise SecurityError(
            f"虚拟路径越界: '{vpath}'，必须以 '{VRROOT}/' 开头"
        )

    real_root = REAL_ROOT.format(user_id=user_id, thread_id=thread_id)
    relative = vpath[len(VRROOT):]
    real_path = os.path.join(real_root, relative.lstrip("/"))

    return validate_path(real_path, real_root)


def validate_path(real_path: str, real_root: str) -> str:
    real_root_abs = os.path.realpath(real_root)
    real_path_abs = os.path.realpath(real_path)
    if not real_path_abs.startswith(real_root_abs):
        raise SecurityError(
            f"真实路径越界: '{real_path_abs}' 不在沙箱根目录 '{real_root_abs}' 内"
        )
    return real_path
