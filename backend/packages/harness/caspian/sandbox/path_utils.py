"""
本文件对外提供：
    虚拟路径与真实路径之间的映射函数及越界校验：resolve_path / validate_path / validate_subdir / resolve_skill_path / SecurityError
    shell 命令路径安全校验：validate_shell_command / validate_local_bash_cd_target

常量：
    VRROOT: 虚拟路径前缀常量 "/mnt/user-data"
    SKILLS_VROOT: skills 虚拟路径前缀常量 "/mnt/skills"
    REAL_ROOT: 真实路径根模板 ".caspian/users/{user_id}/threads/{thread_id}/user-data"
    SUBDIRS: 预创子目录列表 ["workspace", "uploads", "outputs"]
    SKILLS_PUBLIC_REAL_ROOT: public skills 宿主机存储路径 "skills"
    SKILLS_CUSTOM_REAL_ROOT: custom skills 宿主机存储路径模板 ".caspian/users/{user_id}/skills"

shell 命令安全三维防护常量：
    _ABSOLUTE_PATH_PATTERN: 匹配 Unix 和 Windows 绝对路径
    _DOTDOT_PATH_SEGMENT_PATTERN: 匹配 .. 路径段
    _CD_IN_COMMAND_SUBSTITUTION_PATTERN: 匹配命令替换/scriptblock 中的 cd/pushd/popd
    _ABSOLUTE_PATH_WHITELIST: 通用绝对路径白名单（含系统路径）
    _CD_TARGET_WHITELIST: cd 专用白名单（排除系统路径）
    _CD_PATTERNS: 按 shell_type 的 cd/pushd/popd 语法映射

具体工作流:
    resolve_path:
    (1) 校验虚拟路径必须以 VRROOT 开头，否则抛 SecurityError
    (2) 根据 user_id + thread_id 构建真实根目录
    (3) 将虚拟路径去掉 VRROOT 前缀，拼接到真实根目录后
    (4) 调用 validate_path 二次确认未越界
    (5) 返回真实路径

    resolve_skill_path:
    (1) 校验虚拟路径必须以 SKILLS_VROOT 开头，否则抛 SecurityError
    (2) 去掉前缀后取第一段作为 category（public / custom）
    (3) public → 拼接到 SKILLS_PUBLIC_REAL_ROOT 后
    (4) custom → 用 user_id 填充 SKILLS_CUSTOM_REAL_ROOT 模板后拼接
    (5) 调用 validate_path 二次确认未越界
    (6) 返回真实路径

    validate_shell_command:
    (1) 防线 0: _CD_IN_COMMAND_SUBSTITUTION_PATTERN 检测命令替换/scriptblock 中的 cd/pushd
    (2) 防线 1: cd/pushd/popd 目标独立校验（若 shell_type 提供）
    (3) 防线 2: _ABSOLUTE_PATH_PATTERN 匹配所有绝对路径，逐一检查白名单
    (4) 防线 3: _DOTDOT_PATH_SEGMENT_PATTERN 检测 .. 路径段
    (5) 防线 4: "file://" 子串检测
    (6) 任一防线命中 → SecurityError；全部通过 → None

    validate_local_bash_cd_target:
    (1) 按 shell_type 查找 _CD_PATTERNS 对应的 cd/pushd/popd 正则
    (2) 解析命令中所有目录切换调用
    (3) 逐一检查目标: 空参数/-/~ /$ /反引号 → 拒绝，绝对路径 → _CD_TARGET_WHITELIST 检查，相对路径 → 放行

示例:
    resolve_path("/mnt/user-data/workspace/script.py", "uuid-xxx", "abc123")
    → ".caspian/users/uuid-xxx/threads/abc123/user-data/workspace/script.py"

    resolve_skill_path("/mnt/skills/public/pdf/SKILL.md", user_id="u-1")
    → "skills/public/pdf/SKILL.md" 的绝对路径

    resolve_skill_path("/mnt/skills/custom/my-skill/SKILL.md", user_id="u-1")
    → ".caspian/users/u-1/skills/custom/my-skill/SKILL.md" 的绝对路径

    validate_shell_command("cat /etc/passwd")  → SecurityError
    validate_shell_command("cat /mnt/user-data/workspace/foo.py")  → None
"""

import logging
import os
import re
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

VRROOT = "/mnt/user-data"
SKILLS_VROOT = "/mnt/skills"
REAL_ROOT = ".caspian/users/{user_id}/threads/{thread_id}/user-data"
SUBDIRS = ["workspace", "uploads", "outputs"]
SKILLS_PUBLIC_REAL_ROOT = "skills"
SKILLS_CUSTOM_REAL_ROOT = ".caspian/users/{user_id}/skills"


class SecurityError(Exception):
    pass


# ---------------------------------------------------------------------------
# shell 命令安全三维防护 — 常量
# ---------------------------------------------------------------------------

_ABSOLUTE_PATH_PATTERN: re.Pattern = re.compile(
    r"(?:[A-Za-z]:\\[^\s\"'`;|&<>$():]+)"        # Windows: C:\path\to\file (backslash only, exclude :// URLs)
    r"|"
    r"(?<!:/)(?:/[^\s\"'`;|&<>$():]+)"           # Unix: /path/to/file (exclude :// URLs and PATH separators)
)

_DOTDOT_PATH_SEGMENT_PATTERN: re.Pattern = re.compile(
    r"(?<!\w)\.\.(?:[/\\]|$)"                     # .. 作为路径段出现
)

_CD_IN_COMMAND_SUBSTITUTION_PATTERN: re.Pattern = re.compile(
    r"\$\([^)]*\b(?:cd|pushd|popd)\b[^)]*\)"     # $(cd ...) / $(pushd ...)
    r"|"
    r"`[^`]*\b(?:cd|pushd|popd)\b[^`]*`"          # `cd ...` / `pushd ...`
    r"|"
    r"&\s*\{[^}]*\b(?:cd|Set-Location|Push-Location|Pop-Location|sl)\b[^}]*\}"  # powershell & { cd ... }
)

_ABSOLUTE_PATH_WHITELIST: frozenset[str] = frozenset({
    "/mnt/",
    "/mnt/user-data/",
    "/mnt/skills/",
    "/mnt/acp-workspace/",
    "/bin/",
    "/usr/bin/",
    "/sbin/",
    "/opt/homebrew/bin/",
    "/dev/",
})

_CD_TARGET_WHITELIST: frozenset[str] = frozenset({
    "/mnt/",
    "/mnt/user-data/",
    "/mnt/skills/",
    "/mnt/acp-workspace/",
})

_CD_PATTERNS: dict[str, re.Pattern] = {
    "bash":       re.compile(r"\b(?:cd|pushd|popd)(?:\s+|$)"),
    "sh":         re.compile(r"\b(?:cd|pushd|popd)(?:\s+|$)"),
    "cmd":        re.compile(r"\b(?:cd|chdir)(?:\s+|$)"),
    "powershell": re.compile(r"\b(?:Set-Location|Push-Location|Pop-Location|sl|cd)(?:\s+|$)"),
}

# 匹配 PATH= 赋值中引用的绝对路径（如 PATH=/tmp:$PATH 中的 /tmp）
_PATH_ASSIGNMENT_ABS_PATH: re.Pattern = re.compile(
    r"PATH\s*=\s*([^\s;&|]+)"
)


# ---------------------------------------------------------------------------
# 受保护 helpers
# ---------------------------------------------------------------------------

def _find_path_assignment_paths(command: str) -> set[str]:
    """提取 PATH= 赋值中的路径 token，这些路径不参与绝对路径拦截。

    输入:
        command: str — shell 命令字符串

    输出:
        set[str] — PATH= 上下文中的路径字符串集合
    """
    result: set[str] = set()
    for match in _PATH_ASSIGNMENT_ABS_PATH.finditer(command):
        value = match.group(1)
        # 按 : 拆分 PATH 值
        for segment in value.split(":"):
            segment = segment.strip()
            if segment.startswith("/"):
                result.add(segment)
    return result


def _check_absolute_paths(command: str, whitelist: frozenset[str]) -> None:
    """提取命令中所有绝对路径，逐一检查是否在白名单内。

    输入:
        command: str — shell 命令字符串
        whitelist: frozenset[str] — 允许的绝对路径前缀集合

    输出:
        None — 全部通过；不在白名单内则抛出 SecurityError

    工作流:
        跳过 URL（如 https://）和 PATH= 赋值上下文中的路径
    """
    # 收集 PATH= 上下文中的路径（PATH 变量值而非文件访问路径，不 block；
    # PATH= 毒化检测由 SandboxAuditMiddleware 统一处理）
    path_assignment_paths = _find_path_assignment_paths(command)

    for match in _ABSOLUTE_PATH_PATTERN.finditer(command):
        path = match.group(0)
        start = match.start()

        # 跳过 URL 路径（如 https://example.com/...）
        if path.startswith("//"):
            continue

        # 跳过 PATH= 赋值中的路径
        if path in path_assignment_paths:
            continue

        # 归一化:补尾斜杠匹配前缀,使裸挂载根 "/mnt" 命中 "/mnt/" 前缀,
        # 且 "/mntfoo" 之类仍不命中
        normalized = path if path.endswith("/") else path + "/"
        if not any(normalized.startswith(prefix) for prefix in whitelist):
            raise SecurityError(
                f"绝对路径 '{path}' 不在白名单中，拒绝执行"
            )




# ---------------------------------------------------------------------------
# 对外函数
# ---------------------------------------------------------------------------

def validate_subdir(vpath: str, allowed: set[str]) -> None:
    """校验虚拟路径的第一级子目录是否在白名单内，否则抛 SecurityError。

    输入:
        vpath: 完整虚拟路径，如 "/mnt/user-data/workspace/foo.py"
        allowed: 允许的子目录名集合，如 {"workspace", "outputs"}

    工作流:
        (1) 校验 vpath 必须以 VRROOT + "/" 开头，否则抛 SecurityError
            （虚拟路径必须规范化；不依赖下游 resolve_path 二次兜底）
        (2) 拒绝任何含 ".." 路径段的虚拟路径（虚拟路径里不允许 ".."，不做智能解析）
        (3) 去掉 VRROOT 前缀，取剩余部分的第一级目录名
        (4) 若该目录名不在 allowed 中，抛 SecurityError

    示例:
        validate_subdir("/mnt/user-data/uploads/a.txt", {"uploads", "workspace"})  # 通过
        validate_subdir("/mnt/user-data/outputs/b.txt", {"uploads", "workspace"})  # 抛 SecurityError
        validate_subdir("/mnt/user-data/workspace/../evil", {"workspace"})          # 抛 SecurityError (含 ..)
    """
    if not vpath.startswith(VRROOT + "/"):
        raise SecurityError(
            f"虚拟路径越界: '{vpath}'，必须以 '{VRROOT}/' 开头"
        )
    if ".." in PurePosixPath(vpath).parts:
        raise SecurityError(
            f"虚拟路径不允许包含 '..': '{vpath}'"
        )
    relative = vpath[len(VRROOT):].lstrip("/")
    subdir = relative.split("/", 1)[0]
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
    """校验真实路径是否在沙箱根目录内（目录层级判断），返回规范化路径。

    输入:
        real_path: str — 待校验的真实路径（含 .. / symlink 等未规范化成分）
        real_root: str — 沙箱真实根目录

    输出:
        str — 已 resolve 的规范化绝对路径

    工作流:
        (1) root = Path(real_root).resolve(strict=False)：绝对化 + 消除 .. + 跟随 symlink
        (2) candidate = Path(real_path).resolve(strict=False)：同上
        (3) candidate.relative_to(root)：目录层级判断——root 必须是 candidate 的祖先，
            否则抛 ValueError（兄弟前缀逃逸、非后代 symlink 均被拒）。
            注意: relative_to 对"candidate == root"返回空相对路径 '.' 而非抛错，
            故需再显式拒绝"等于根自身"（要求严格在根内，而非解到根目录本体）。
        (4) 越界 → SecurityError；落界内 → 返回 str(candidate) 规范化路径

    为什么用 relative_to 而非字符串前缀:
        startswith 会把 /a/user-data 误判为 /a/user-data-evil 的"前缀包含"；
        relative_to 判断的是目录层级（祖先-后代关系），机制上正确。

    示例:
        validate_path("/srv/user-data/a.py", "/srv/user-data")   → "/srv/user-data/a.py"
        validate_path("/srv/user-data-evil/x", "/srv/user-data") → SecurityError
        validate_path("/srv/user-data", "/srv/user-data")        → SecurityError
    """
    root = Path(real_root).resolve(strict=False)
    candidate = Path(real_path).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError:
        raise SecurityError(
            f"真实路径越界: '{candidate}' 不在沙箱根目录 '{root}' 内"
        )
    if candidate == root:
        raise SecurityError(
            f"真实路径越界: '{candidate}' 即沙箱根目录本身，必须位于其子路径内"
        )
    return str(candidate)


def resolve_skill_path(vpath: str, user_id: str) -> str:
    """将 /mnt/skills/ 虚拟路径映射为宿主机真实路径。

    输入:
        vpath: str — skills 虚拟路径，如 "/mnt/skills/public/pdf/SKILL.md"
        user_id: str — 用户标识，用于 custom skills 的 per-user 路径填充

    输出:
        str — 宿主机真实路径的绝对路径

    工作流:
        (1) 校验虚拟路径必须以 SKILLS_VROOT 开头，否则抛 SecurityError
        (2) 去掉前缀后取第一段作为 category
        (3) category == "public" → 拼接到 SKILLS_PUBLIC_REAL_ROOT 后
        (4) category == "custom" → 用 user_id 填充 SKILLS_CUSTOM_REAL_ROOT 模板后拼接
        (5) 未知 category → 抛 SecurityError
        (6) 调用 validate_path 二次确认未越界
        (7) 返回真实路径

    示例:
        resolve_skill_path("/mnt/skills/public/pdf/SKILL.md", user_id="u-1")
        → "<cwd>/skills/public/pdf/SKILL.md"
        resolve_skill_path("/mnt/skills/custom/my-skill/SKILL.md", user_id="u-1")
        → "<cwd>/.caspian/users/u-1/skills/custom/my-skill/SKILL.md"
    """
    if not vpath.startswith(SKILLS_VROOT + "/"):
        raise SecurityError(
            f"虚拟路径越界: '{vpath}'，必须以 '{SKILLS_VROOT}/' 开头"
        )

    relative = vpath[len(SKILLS_VROOT):].lstrip("/")
    parts = relative.split("/", 1)
    if len(parts) < 2:
        raise SecurityError(
            f"skills 路径格式无效: '{vpath}'，需为 /mnt/skills/<category>/<path>"
        )

    category, rest = parts[0], parts[1]

    if category == "public":
        real_root = os.path.abspath(SKILLS_PUBLIC_REAL_ROOT)
        real_path = os.path.join(real_root, rest)
        return validate_path(real_path, real_root)
    elif category == "custom":
        real_root = os.path.abspath(SKILLS_CUSTOM_REAL_ROOT.format(user_id=user_id))
        real_path = os.path.join(real_root, rest)
        return validate_path(real_path, real_root)
    else:
        raise SecurityError(
            f"未知的 skill 类别: '{category}'，仅支持 public/custom"
        )


def validate_shell_command(command: str, shell_type: str | None = None) -> None:
    """shell 命令全局安全扫描（三维路径防护）。

    输入:
        command: str — 待执行的 shell 命令字符串
        shell_type: str | None — shell 类型，提供时在防线 0 之后、防线 2 之前执行防线 1（cd 目标校验）

    输出:
        None — 全部通过；任一防线命中 → SecurityError

    工作流:
        (0) 防线 0: 命令替换/scriptblock 中 cd/pushd 拦截 → SecurityError
        (1) 防线 1: cd/pushd/popd 目标独立校验（若 shell_type 提供）→ SecurityError
        (2) 防线 2: 全局绝对路径白名单检查 → SecurityError
        (3) 防线 3: .. 路径段检测 → SecurityError
        (4) 防线 4: file:// 子串检测 → SecurityError

    示例:
        validate_shell_command("cat /etc/passwd")            → SecurityError
        validate_shell_command("ls -la")                      → None
        validate_shell_command("cd /tmp && ls", "bash")       → SecurityError (防线 1)
    """
    # 防线 0: 命令替换/scriptblock 中 cd/pushd 拦截
    if _CD_IN_COMMAND_SUBSTITUTION_PATTERN.search(command):
        raise SecurityError(
            f"命令替换/scriptblock 中包含 cd/pushd 命令，拒绝执行: {command[:80]}"
        )

    # 防线 1: cd/pushd/popd 目标独立校验（维度②）
    if shell_type is not None:
        validate_local_bash_cd_target(command, shell_type)

    # 防线 2: 全局绝对路径白名单检查
    _check_absolute_paths(command, _ABSOLUTE_PATH_WHITELIST)

    # 防线 3: .. 路径段检测
    if _DOTDOT_PATH_SEGMENT_PATTERN.search(command):
        raise SecurityError(
            f"路径中包含 '..' 穿越段，拒绝执行: {command[:80]}"
        )

    # 防线 4: file:// URL 拦截
    if "file://" in command:
        raise SecurityError(
            f"禁止使用 file:// URL: {command[:80]}"
        )


def validate_local_bash_cd_target(command: str, shell_type: str) -> None:
    """对 cd/pushd/popd 及等价命令的目标路径进行独立安全校验（维度②）。

    输入:
        command: str — shell 命令字符串
        shell_type: str — shell 类型（bash / sh / cmd / powershell）

    输出:
        None — 全部通过；校验失败抛出 SecurityError

    工作流:
        (1) 按 shell_type 查找对应的 cd/pushd/popd 正则
        (2) 解析命令中所有目录切换调用
        (3) 逐一检查目标:
            - 空参数（裸 cd）→ 拒绝
            - "-"（OLDPWD）→ 拒绝
            - startswith("~") → 拒绝
            - startswith("$") → 拒绝
            - startswith("`") → 拒绝
            - 绝对路径 → _CD_TARGET_WHITELIST 白名单检查
            - 相对路径 → 放行（由 validate_shell_command 全局 .. 扫描兜底）

    示例:
        validate_local_bash_cd_target("cd /mnt/user-data/workspace && ls", "bash")  # 通过
        validate_local_bash_cd_target("cd /tmp && ls", "bash")                       # SecurityError
        validate_local_bash_cd_target("cd ~ && ls", "bash")                          # SecurityError
        validate_local_bash_cd_target("pushd /tmp && ls", "bash")                    # SecurityError
    """
    pattern = _CD_PATTERNS.get(shell_type)
    if pattern is None:
        return

    for match in pattern.finditer(command):
        start = match.end()
        # 提取 target（到下一个命令分隔符）
        rest = command[start:]
        target = rest.strip()

        if not target:
            raise SecurityError("cd 无参数（默认跳转到 HOME），拒绝执行")

        # 取第一个空白/分隔符前的 token
        m = re.match(r"([^\s;&|]+)", target)
        if m is None:
            raise SecurityError("cd 无参数（默认跳转到 HOME），拒绝执行")
        target = m.group(1)

        if target == "-":
            raise SecurityError("cd '-'（OLDPWD）可能指向沙箱外，拒绝执行")
        if target.startswith("~"):
            raise SecurityError(f"cd '{target}'（HOME 展开）不可预测，拒绝执行")
        if target.startswith("$"):
            raise SecurityError(f"cd '{target}'（变量展开）不可预测，拒绝执行")
        if target.startswith("`"):
            raise SecurityError(f"cd '{target}'（命令替换）不可预测，拒绝执行")
        if target.startswith("/") or (len(target) >= 2 and target[1] == ":"):
            # 绝对路径 → cd 专用白名单检查（归一化：补尾斜杠匹配前缀）
            normalized = target if target.endswith("/") else target + "/"
            if not any(normalized.startswith(prefix) for prefix in _CD_TARGET_WHITELIST):
                raise SecurityError(
                    f"cd 目标 '{target}' 不在 cd 白名单中，拒绝执行"
                )
        # 相对路径 → 放行
