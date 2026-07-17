"""
本文件对外提供 `SandboxAuditMiddleware` 类，作为 shell 命令安全审计中间件。

对外提供:
    SandboxAuditMiddleware(AgentMiddleware) — 覆盖 wrap_tool_call / awrap_tool_call 钩子，
    对 shell 工具（bash_tool / powershell_tool / cmd_tool / sh_tool）的命令进行安全审计

输入:
    wrap_tool_call / awrap_tool_call:
        request: ToolCallRequest — 即将执行的工具调用请求（含 tool_call、tool、state、runtime）
        handler: Callable — 下游处理器，调用 handler(request) 执行真实工具

输出:
    ToolMessage | Command — block 时返回拦截消息，warn 时返回执行结果+风险提示，pass 时原样返回

具体工作流:
    (1) 判断工具是否为 shell 工具（bash_tool / powershell_tool / cmd_tool / sh_tool）
    (2) 非 shell 工具 → 直接调用 handler(request) 放行
    (3) shell 工具 → 提取 command 参数 → _classify(shell_type, command) 判定风险等级
    (4) block → 返回 status="error" 的 ToolMessage，不执行命令
    (5) 命令执行后执行 PATH= 毒化检测（shell 类型无关）→ 命中追加专用告警
    (6) warn → 调用 handler(request) 执行，在结果中追加风险提示
    (7) pass → 调用 handler(request) 返回原始结果
    (8) 整个审计逻辑外裹 try/except，异常时 fallback 到 handler(request) 放行

示例:
    from lead_agent.agents.middlewares.sandbox_audit_middleware import SandboxAuditMiddleware

    middleware = SandboxAuditMiddleware()
    # 在 create_agent(middleware=[..., middleware]) 中使用
"""

import logging
import re
from typing import Awaitable, Callable, Literal

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

logger = logging.getLogger(__name__)

_SHELL_TOOLS: frozenset = frozenset({"bash_tool", "powershell_tool", "cmd_tool", "sh_tool"})

_PATH_PATTERN: re.Pattern = re.compile(r"PATH\s*=")

# ---------------------------------------------------------------------------
# bash/sh 高危规则 — block
# ---------------------------------------------------------------------------
_BASH_HIGH_RISK: list[re.Pattern] = [
    re.compile(r"rm\s+-rf\s+(/|/\*|~)"),                     # 递归强制删除根目录
    re.compile(r"dd\s+if=.*of=/dev/"),                        # 磁盘覆写
    re.compile(r"mkfs\."),                                     # 文件系统创建
    re.compile(r":\(\)\s*\{"),                                 # fork 炸弹
    re.compile(r">\s*/dev/(sd|nvme|hd|xvd)"),                  # 块设备覆写重定向
    re.compile(r"(curl|wget).*\|.*(sh|bash|zsh|dash)"),        # 管道到 shell 执行
    re.compile(r"chmod\s+(-R\s+)?777\s+/"),                    # 根目录权限变更
    re.compile(r"sudo\s+|su\s+"),                               # 权限提升
    re.compile(r"(shutdown|reboot|halt|poweroff|init\s+[06])"), # 系统关机/重启
    re.compile(r"kill\s+-9\s+-1|killall\s+"),                   # 批量进程终止
    re.compile(r"/etc/(shadow|passwd|sudoers)"),                # 关键系统文件操作
    re.compile(r"chattr\s+(-R|\+i)\s+/"),                       # 文件属性锁定
    re.compile(r"nc\s+.*-e|bash\s+-i\s+.*>&\s+/dev/tcp/"),     # 反向 shell
    re.compile(r"chown\s+-R\s+[^/]*\s+/[^s]"),                  # 根目录所有者变更
    re.compile(r">>\s*/etc/(shadow|passwd|sudoers)"),           # 追写关键系统文件
]

# ---------------------------------------------------------------------------
# bash/sh 中危规则 — warn
# ---------------------------------------------------------------------------
_BASH_MEDIUM_RISK: list[re.Pattern] = [
    re.compile(r"rm\s+-rf\s+"),                               # 递归删除（非根目录）
    re.compile(r"pip\d*\s+install|pip3\s+install|npm\s+install\s+-g"),  # 全局包安装
    re.compile(r"systemctl\s+(stop|disable|mask)"),            # 系统服务控制
    re.compile(r"iptables\s+-|nft\s+"),                         # 防火墙修改
    re.compile(r"crontab\s+-"),                                 # 定时任务修改
    re.compile(r"nmap\s+|tcpdump\s+"),                          # 网络扫描
    re.compile(r"git\s+push\s+.*--force"),                      # 强制推送
    re.compile(r"scp\s+|rsync\s+.*:"),                          # 远程文件传输
    re.compile(r"(curl|wget)\s+.*https?://"),                   # curl/wget 下载
    re.compile(r"openssl\s+genrsa|openssl\s+rsa"),              # 私钥生成
    re.compile(r"mount\s+|umount\s+"),                          # 挂载操作
    re.compile(r"chmod\s+(-R\s+)?777\s+"),                      # 权限变更
    re.compile(r"useradd\s+|usermod\s+|passwd\s+"),             # 用户账户变更
    re.compile(r"docker\s+run.*--privileged"),                  # Docker 特权模式
    re.compile(r"find\s+.*-exec\s+"),                           # find 执行
    re.compile(r"xargs\s+.*(sh|bash)"),                         # xargs 执行
    re.compile(r"eval\s+"),                                     # 动态求值
    re.compile(r"source\s+.*/(dev|tmp)"),                       # source 外部脚本
]

# ---------------------------------------------------------------------------
# cmd 高危规则 — block
# ---------------------------------------------------------------------------
_CMD_HIGH_RISK: list[re.Pattern] = [
    re.compile(r"del\s+/f\s+/s\s+[A-Z]:\\"),                   # 系统盘递归删除
    re.compile(r"format\s+[A-Z]:"),                             # 格式化系统盘
    re.compile(r"shutdown\s+/s|shutdown\s+/r|shutdown\s+/p"),   # 系统关机
    re.compile(r"diskpart"),                                    # 磁盘分区
    re.compile(r"icacls\s+[A-Z]:\\\s+/grant\s+Everyone:F"),     # 注册表权限变更
    re.compile(r"reg\s+delete\s+HKLM"),                         # 注册表删除
    re.compile(r"net\s+user\s+administrator\s+/active"),        # 用户账户创建/提权
]

# ---------------------------------------------------------------------------
# cmd 中危规则 — warn
# ---------------------------------------------------------------------------
_CMD_MEDIUM_RISK: list[re.Pattern] = [
    re.compile(r"del\s+/f\s+/s"),                              # 递归删除（非系统盘）
    re.compile(r"rmdir\s+/s"),                                  # 目录递归删除
    re.compile(r"reg\s+add"),                                   # 注册表修改
    re.compile(r"net\s+stop|sc\s+stop"),                        # 服务控制
    re.compile(r"taskkill\s+"),                                 # 进程终止
]

# ---------------------------------------------------------------------------
# powershell 高危规则 — block
# ---------------------------------------------------------------------------
_PWSH_HIGH_RISK: list[re.Pattern] = [
    re.compile(r"Remove-Item\s+.*-Recurse.*-Force\s+[A-Z]:\\"),  # 系统盘递归删除
    re.compile(r"Stop-Computer|Restart-Computer"),                # 系统关机/重启
    re.compile(r"Format-Volume\s+-DriveLetter\s+[A-Z]"),          # 磁盘格式化
    re.compile(r"Set-ItemProperty\s+.*HKLM"),                     # 注册表系统策略修改
    re.compile(r"Invoke-Expression\s+|iex\s+"),                   # 动态执行不可信代码
    re.compile(r"\[System\.Diagnostics\.Process\]::Start\(.*cmd"), # 进程启动危险命令
    re.compile(r"Get-Process\s+.*\|\s*Stop-Process"),             # 批量进程终止
    re.compile(r"Disable-WindowsOptionalFeature.*-Online"),       # Windows 功能禁用
]

# ---------------------------------------------------------------------------
# powershell 中危规则 — warn
# ---------------------------------------------------------------------------
_PWSH_MEDIUM_RISK: list[re.Pattern] = [
    re.compile(r"Remove-Item\s+.*-Recurse.*-Force"),              # 递归删除（非系统盘）
    re.compile(r"Stop-Process\s+"),                               # 进程终止
    re.compile(r"Set-ExecutionPolicy"),                           # 执行策略变更
    re.compile(r"New-LocalUser|Set-LocalUser"),                   # 用户管理
    re.compile(r"Enable-PSRemoting|Disable-PSRemoting"),          # 远程管理
    re.compile(r"Invoke-WebRequest|Invoke-RestMethod|iwr\s+|irm\s+"),  # 网络下载
    re.compile(r"Set-Service\s+"),                                # 服务配置
    re.compile(r"Start-Process\s+.*(\.ps1|\.bat|\.cmd|\.vbs)"),   # 脚本进程启动
]

# ---------------------------------------------------------------------------
# 风险等级 → 正则列表 映射
# ---------------------------------------------------------------------------
_RISK_PATTERNS: dict[str, dict[str, list[re.Pattern]]] = {
    "bash":       {"high": _BASH_HIGH_RISK, "medium": _BASH_MEDIUM_RISK},
    "sh":         {"high": _BASH_HIGH_RISK, "medium": _BASH_MEDIUM_RISK},
    "cmd":        {"high": _CMD_HIGH_RISK,  "medium": _CMD_MEDIUM_RISK},
    "powershell": {"high": _PWSH_HIGH_RISK, "medium": _PWSH_MEDIUM_RISK},
}


def _classify(shell_type: str, command: str) -> Literal["block", "warn", "pass"]:
    """判定 shell 命令的风险等级。

    输入:
        shell_type: str — shell 类型（bash / sh / cmd / powershell）
        command: str — 待执行的 shell 命令字符串

    输出:
        Literal["block", "warn", "pass"] — 风险等级

    工作流:
        (1) 按 shell_type 查找对应的风险规则组
        (2) 先匹配高风险规则 → 命中返回 "block"
        (3) 再匹配中风险规则 → 命中返回 "warn"
        (4) 均未命中 → 返回 "pass"
    """
    if not isinstance(command, str) or not command.strip():
        return "block"

    group = _RISK_PATTERNS.get(shell_type)
    if group is None:
        return "pass"

    # 先高风险
    for pattern in group["high"]:
        if pattern.search(command):
            logger.info("SandboxAudit: shell=%s BLOCK → %r", shell_type, pattern.pattern)
            return "block"

    # 后中风险
    for pattern in group["medium"]:
        if pattern.search(command):
            logger.info("SandboxAudit: shell=%s WARN → %r", shell_type, pattern.pattern)
            return "warn"

    return "pass"


class SandboxAuditMiddleware(AgentMiddleware):

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _shell_type_from_name(tool_name: str) -> str | None:
        """从工具名提取 shell 类型。

        输入:
            tool_name: str — 工具名称（如 "bash_tool"）

        输出:
            str | None — shell 类型（"bash" / "powershell" / "cmd" / "sh"），非 shell 工具返回 None
        """
        if tool_name not in _SHELL_TOOLS:
            return None
        # "bash_tool" → "bash", "powershell_tool" → "powershell"
        return tool_name.rsplit("_tool", 1)[0]

    @staticmethod
    def _make_block_message(request: ToolCallRequest) -> ToolMessage:
        """构造拦截消息。

        输入:
            request: ToolCallRequest — 被拦截的工具调用请求

        输出:
            ToolMessage — status="error" 的拦截消息
        """
        command = request.tool_call.get("args", {}).get("command", "")
        tool_name = request.tool_call.get("name", "unknown")
        return ToolMessage(
            content=(
                f"[SandboxAudit] 高危命令已拦截 (status=error)\n"
                f"工具: {tool_name}\n"
                f"命令: {command}\n"
                f"原因: 该命令命中高风险安全规则，已阻止执行"
            ),
            tool_call_id=request.tool_call.get("id", ""),
            name=tool_name,
        )

    @staticmethod
    def _append_warning(result, tool_name: str, command: str) -> ToolMessage | Command:
        """在工具执行结果中追加风险提示。

        输入:
            result: ToolMessage | Command — 原始执行结果
            tool_name: str — 工具名称
            command: str — 执行的命令

        输出:
            ToolMessage | Command — 追加了风险提示的结果
        """
        warning = (
            f"\n\n[SandboxAudit] ⚠️ 风险提示: 该命令命中中风险安全规则。\n"
            f"命令: {command}\n"
            f"请确认操作的安全性。"
        )
        if isinstance(result, ToolMessage):
            return ToolMessage(
                content=(result.content or "") + warning,
                tool_call_id=result.tool_call_id,
                name=getattr(result, "name", None),
            )
        # Command 类型不追加 warning（避免破坏控制流语义），直接返回
        return result

    @staticmethod
    def _append_path_warning(result, command: str) -> ToolMessage | Command:
        """在工具执行结果中追加 PATH 毒化风险提示。

        输入:
            result: ToolMessage | Command — 原始执行结果
            command: str — 执行的命令

        输出:
            ToolMessage | Command — 追加了 PATH 毒化风险提示的结果
        """
        warning = (
            f"\n\n[SandboxAudit] ⚠️ PATH 毒化风险: 检测到 PATH= 环境变量修改。\n"
            f"命令: {command}\n"
            f"请确认操作的安全性。"
        )
        if isinstance(result, ToolMessage):
            return ToolMessage(
                content=(result.content or "") + warning,
                tool_call_id=result.tool_call_id,
                name=getattr(result, "name", None),
            )
        # Command 类型不追加 warning（避免破坏控制流语义），直接返回
        return result

    # ------------------------------------------------------------------
    # 核心审计逻辑（sync + async 共用）
    # ------------------------------------------------------------------

    def _audit_and_execute(self, request: ToolCallRequest, handler: Callable):
        """审计 shell 命令并执行/拦截。

        输入:
            request: ToolCallRequest — 工具调用请求
            handler: Callable — 下游处理器（同步或异步）

        输出:
            ToolMessage | Command — 执行结果或拦截消息
        """
        tool_name = request.tool_call.get("name", "")
        shell_type = self._shell_type_from_name(tool_name)

        # 非 shell 工具 → 放行
        if shell_type is None:
            return handler(request)

        command = request.tool_call.get("args", {}).get("command", "")

        # 校验 command 输入
        if not isinstance(command, str) or not command.strip():
            return self._make_block_message(request)

        level = _classify(shell_type, command)

        if level == "block":
            return self._make_block_message(request)

        result = handler(request)

        # PATH= 毒化检测（shell 类型无关，warn 级别）
        if _PATH_PATTERN.search(command):
            result = self._append_path_warning(result, command)

        if level == "warn":
            result = self._append_warning(result, tool_name, command)

        return result

    # ------------------------------------------------------------------
    # public hooks
    # ------------------------------------------------------------------

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """同步钩子：包裹工具调用，对 shell 命令进行安全审计。

        输入:
            request: ToolCallRequest — 即将执行的工具调用
            handler: Callable — 下游处理器

        输出:
            ToolMessage | Command — 审计后的执行结果
        """
        try:
            return self._audit_and_execute(request, handler)
        except Exception:
            logger.error("SandboxAudit: wrap_tool_call 审计异常，fallback 放行", exc_info=True)
            return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """异步钩子：包裹工具调用，对 shell 命令进行安全审计。

        输入:
            request: ToolCallRequest — 即将执行的工具调用
            handler: Callable — 下游异步处理器

        输出:
            ToolMessage | Command — 审计后的执行结果
        """
        try:
            return await self._audit_and_execute(request, handler)
        except Exception:
            logger.error("SandboxAudit: awrap_tool_call 审计异常，fallback 放行", exc_info=True)
            return await handler(request)
