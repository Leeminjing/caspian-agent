"""
本文件对外提供目标域的纯类型与 error code，供 GoalService、authority、tool、round driver 使用。

输入: 无 — 本文件为纯定义文件，不包含函数入口

输出:
    GoalPhase — 可持久生命周期（active / paused / blocked / complete / none）
    GoalRef — compare-and-set 标识（id + revision）
    GoalBlockReason — 机器可路由 + 人类可读的阻塞原因
    GoalRecord — 写入 store 的目标记录（含 durable 字段 + armed）
    GoalError / GOAL_* 常量 — 稳定错误码
"""


class GoalError(Exception):
    """目标域结构化错误，带稳定 error code。"""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


GOAL_STALE_REVISION = "GOAL_STALE_REVISION"
GOAL_NOT_FOUND = "GOAL_NOT_FOUND"
GOAL_ALREADY_EXISTS = "GOAL_ALREADY_EXISTS"
GOAL_INVALID_TRANSITION = "GOAL_INVALID_TRANSITION"
GOAL_INVALID_OBJECTIVE = "GOAL_INVALID_OBJECTIVE"
GOAL_INVALID_MAX_ROUNDS = "GOAL_INVALID_MAX_ROUNDS"
GOAL_INVALID_BLOCK_REASON = "GOAL_INVALID_BLOCK_REASON"
GOAL_INVALID_EDIT = "GOAL_INVALID_EDIT"
GOAL_TOOL_AGENT_REQUIRED = "GOAL_TOOL_AGENT_REQUIRED"
GOAL_TOOL_DRIVER_REQUIRED = "GOAL_TOOL_DRIVER_REQUIRED"
GOAL_TOOL_BLOCK_THRESHOLD = "GOAL_TOOL_BLOCK_THRESHOLD"
GOAL_TOOL_AUTHORITY_REQUIRED = "GOAL_TOOL_AUTHORITY_REQUIRED"

# 用作 store 中"已清除"墓碑的 phase 值（非真实生命周期阶段）
GOAL_PHASE_NONE = "none"

GoalPhase = str
# 'active' | 'paused' | 'blocked' | 'complete' | 'none'


class GoalBlockReason:
    """机器可路由 + 人类可读的阻塞原因。"""

    __slots__ = ("code", "message")

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}

    @staticmethod
    def from_dict(value: dict) -> "GoalBlockReason":
        return GoalBlockReason(str(value["code"]), str(value["message"]))


class GoalRef:
    """compare-and-set 标识：稳定 id + 正整数 revision。"""

    __slots__ = ("id", "revision")

    def __init__(self, goal_id: str, revision: int) -> None:
        self.id = goal_id
        self.revision = int(revision)

    def to_dict(self) -> dict:
        return {"id": self.id, "revision": self.revision}

    @staticmethod
    def from_dict(value: dict) -> "GoalRef":
        return GoalRef(str(value["id"]), int(value["revision"]))


class GoalRecord:
    """写入 store 的目标记录，含 durable 字段与 armed（进程内续跑授权）。"""

    __slots__ = (
        "id",
        "revision",
        "objective",
        "phase",
        "max_goal_rounds",
        "rounds_started",
        "armed",
        "blocked_reason",
        "created_at",
        "updated_at",
        "cleared",
        "cleared_at",
    )

    def __init__(
        self,
        *,
        goal_id: str,
        revision: int,
        objective: str,
        phase: GoalPhase,
        max_goal_rounds: int,
        rounds_started: int,
        armed: bool,
        blocked_reason: GoalBlockReason | None = None,
        created_at: int,
        updated_at: int,
        cleared: GoalRef | None = None,
        cleared_at: int | None = None,
    ) -> None:
        self.id = goal_id
        self.revision = int(revision)
        self.objective = objective
        self.phase = phase
        self.max_goal_rounds = int(max_goal_rounds)
        self.rounds_started = int(rounds_started)
        self.armed = bool(armed)
        self.blocked_reason = blocked_reason
        self.created_at = int(created_at)
        self.updated_at = int(updated_at)
        self.cleared = cleared
        self.cleared_at = cleared_at

    def is_none(self) -> bool:
        """墓碑/无目标占位。"""
        return self.phase == GOAL_PHASE_NONE

    def to_dict(self) -> dict:
        value: dict = {
            "id": self.id,
            "revision": self.revision,
            "objective": self.objective,
            "phase": self.phase,
            "max_goal_rounds": self.max_goal_rounds,
            "rounds_started": self.rounds_started,
            "armed": self.armed,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.blocked_reason is not None:
            value["blocked_reason"] = self.blocked_reason.to_dict()
        if self.cleared is not None:
            value["cleared"] = self.cleared.to_dict()
        if self.cleared_at is not None:
            value["cleared_at"] = self.cleared_at
        return value

    @staticmethod
    def from_dict(value: dict) -> "GoalRecord":
        blocked_reason = None
        raw_reason = value.get("blocked_reason")
        if isinstance(raw_reason, dict):
            blocked_reason = GoalBlockReason.from_dict(raw_reason)
        cleared = None
        raw_cleared = value.get("cleared")
        if isinstance(raw_cleared, dict):
            cleared = GoalRef.from_dict(raw_cleared)
        return GoalRecord(
            goal_id=str(value["id"]),
            revision=int(value["revision"]),
            objective=str(value.get("objective", "")),
            phase=str(value.get("phase", GOAL_PHASE_NONE)),
            max_goal_rounds=int(value.get("max_goal_rounds", 256)),
            rounds_started=int(value.get("rounds_started", 0)),
            armed=bool(value.get("armed", False)),
            blocked_reason=blocked_reason,
            created_at=int(value.get("created_at", 0)),
            updated_at=int(value.get("updated_at", 0)),
            cleared=cleared,
            cleared_at=value.get("cleared_at"),
        )
