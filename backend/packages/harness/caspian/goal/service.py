"""
本文件对外提供 GoalService：基于 LangGraph store 的目标域服务，负责目标的
get/create/edit/pause/resume/complete/block/clear 与 compare-and-set 校验。

输入:
    store: BaseStore — LangGraph Store 实例（app.state.store 或 runtime.store）
    user_id: str — 用户标识，参与 namespace 隔离
    thread_id: str — 会话标识，目标按 (user_id, thread_id) 隔离
    default_max_goal_rounds: int — create 省略 max_goal_rounds 时的默认值

输出:
    GoalService 实例，各方法返回 GoalRecord 或 None

示例:
    service = GoalService(store=store, user_id=user_id, thread_id=thread_id)
    record = await service.create("Ship the api")
"""

import time
import uuid

from langgraph.store.base import BaseStore

from caspian.goal.domain import (
    GOAL_ALREADY_EXISTS,
    GOAL_INVALID_BLOCK_REASON,
    GOAL_INVALID_EDIT,
    GOAL_INVALID_MAX_ROUNDS,
    GOAL_INVALID_OBJECTIVE,
    GOAL_INVALID_TRANSITION,
    GOAL_NOT_FOUND,
    GOAL_PHASE_NONE,
    GOAL_STALE_REVISION,
    GoalBlockReason,
    GoalError,
    GoalRecord,
    GoalRef,
)


def _namespace(user_id: str, thread_id: str) -> tuple[str, str, str]:
    return ("goal", str(user_id), str(thread_id))


_GOAL_KEY = "goal"


def _positive_int(value: int, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise GoalError(f"{label} 必须为正整数", GOAL_INVALID_MAX_ROUNDS)
    return int(value)


def _clamp_updated_at(updated_at: int, current: int) -> int:
    # 跨墙钟回拨时单调不减
    return max(int(updated_at), int(current))


def _validate_objective(objective: str) -> str:
    if not isinstance(objective, str) or not objective.strip():
        raise GoalError("目标 objective 必须为非空字符串", GOAL_INVALID_OBJECTIVE)
    return objective.strip()


def _validate_block_reason(code: str, message: str) -> GoalBlockReason:
    if not isinstance(code, str) or not code or not isinstance(message, str) or not message.strip():
        raise GoalError("blocked reason 需要 code 与 message", GOAL_INVALID_BLOCK_REASON)
    return GoalBlockReason(code, message.strip())


class GoalService:
    """目标域服务：store 持久化的 CAS 读改写。"""

    _RESUMABLE = frozenset({"active", "paused", "blocked"})

    def __init__(
        self,
        store: BaseStore | None,
        user_id: str,
        thread_id: str,
        default_max_goal_rounds: int = 256,
    ) -> None:
        if store is None:
            raise GoalError("目标模式需要 LangGraph store 支持", GOAL_INVALID_TRANSITION)
        self._store = store
        self._namespace = _namespace(user_id, thread_id)
        self._default_max_goal_rounds = _positive_int(default_max_goal_rounds, "default_max_goal_rounds")

    async def _load(self) -> GoalRecord | None:
        item = await self._store.aget(self._namespace, _GOAL_KEY)
        if item is None:
            return None
        return GoalRecord.from_dict(item.value)

    async def _save(self, record: GoalRecord) -> None:
        await self._store.aput(self._namespace, _GOAL_KEY, record.to_dict())

    async def _expect_current(self, ref: dict) -> GoalRecord:
        current = await self._load()
        if current is None or current.is_none():
            raise GoalError("当前无目标", GOAL_NOT_FOUND)
        if current.id != str(ref["id"]) or current.revision != int(ref["revision"]):
            raise GoalError(
                f"陈旧 goal ref {ref['id']} revision {ref['revision']}; 当前是 {current.id} revision {current.revision}",
                GOAL_STALE_REVISION,
            )
        return current

    async def get(self) -> GoalRecord | None:
        """读取当前目标；无目标或已清除返回 None。"""
        record = await self._load()
        if record is None or record.is_none():
            return None
        return record

    async def disarm(self) -> GoalRecord | None:
        """只撤销进程内自动续跑授权（armed=False），不改 durable 阶段与 revision。"""
        record = await self._load()
        if record is None or record.is_none():
            return record
        if record.armed:
            record.armed = False
            await self._save(record)
        return record

    async def advance_round(self, ref: dict) -> GoalRecord:
        """驱动注入下一轮时推进 rounds_started（不改变 revision/updated_at，非目标变更）。"""
        current = await self._expect_current(ref)
        if current.phase != "active":
            raise GoalError(
                f"cannot advance goal \"{current.id}\" when phase is {current.phase}; expected active",
                GOAL_INVALID_TRANSITION,
            )
        next_round = current.rounds_started + 1
        if next_round > current.max_goal_rounds:
            raise GoalError(
                f"goal \"{current.id}\" 已达到 {current.max_goal_rounds} 轮; 无法再推进",
                GOAL_INVALID_TRANSITION,
            )
        record = GoalRecord(
            goal_id=current.id,
            revision=current.revision,
            objective=current.objective,
            phase=current.phase,
            max_goal_rounds=current.max_goal_rounds,
            rounds_started=next_round,
            armed=current.armed,
            blocked_reason=current.blocked_reason,
            created_at=current.created_at,
            updated_at=current.updated_at,
        )
        await self._save(record)
        return record

    async def create(self, objective: str, max_goal_rounds: int | None = None) -> GoalRecord:
        """创建并武装目标；仅当无目标或目标为 complete 时允许。"""
        current = await self._load()
        if current is not None and not current.is_none() and current.phase != "complete":
            raise GoalError(
                f"目标 \"{current.id}\" 已存在且为 {current.phase}; 需先 clear 或 resume",
                GOAL_ALREADY_EXISTS,
            )
        objective = _validate_objective(objective)
        cap = _positive_int(
            max_goal_rounds if max_goal_rounds is not None else self._default_max_goal_rounds,
            "max_goal_rounds",
        )
        now = int(time.time() * 1000)
        record = GoalRecord(
            goal_id=f"goal-{uuid.uuid4().hex}",
            revision=1,
            objective=objective,
            phase="active",
            max_goal_rounds=cap,
            rounds_started=0,
            armed=True,
            created_at=now,
            updated_at=now,
        )
        await self._save(record)
        return record

    async def edit(self, ref: dict, objective: str | None = None, max_goal_rounds: int | None = None) -> GoalRecord:
        """局部替换 objective / max_goal_rounds，不改变 phase；需至少提供一个替换字段。"""
        current = await self._expect_current(ref)
        if objective is None and max_goal_rounds is None:
            raise GoalError("edit 需要 objective 和/或 max_goal_rounds", GOAL_INVALID_EDIT)
        if max_goal_rounds is not None:
            max_goal_rounds = _positive_int(max_goal_rounds, "max_goal_rounds")
        if objective is not None:
            objective = _validate_objective(objective)
        goal_id = current.id
        revision = current.revision + 1
        now = _clamp_updated_at(int(time.time() * 1000), current.updated_at)
        record = GoalRecord(
            goal_id=goal_id,
            revision=revision,
            objective=objective if objective is not None else current.objective,
            phase=current.phase,
            max_goal_rounds=max_goal_rounds if max_goal_rounds is not None else current.max_goal_rounds,
            rounds_started=current.rounds_started,
            armed=current.armed,
            blocked_reason=current.blocked_reason,
            created_at=current.created_at,
            updated_at=now,
        )
        await self._save(record)
        return record

    async def pause(self, ref: dict) -> GoalRecord:
        return await self._transition(ref, "pause", {"active"}, "paused", armed=False)

    async def resume(self, ref: dict) -> GoalRecord:
        current = await self._expect_current(ref)
        if current.phase not in self._RESUMABLE:
            raise GoalError(
                f"cannot resume goal \"{current.id}\" from phase \"{current.phase}\"; expected one of {sorted(self._RESUMABLE)}",
                GOAL_INVALID_TRANSITION,
            )
        if current.phase == "active" and current.armed:
            raise GoalError(f"goal \"{current.id}\" 已是 active 且 armed", GOAL_INVALID_TRANSITION)
        if current.rounds_started >= current.max_goal_rounds:
            raise GoalError(
                f"goal \"{current.id}\" 已耗尽 {current.max_goal_rounds} 轮; 先提高 max_goal_rounds 再 resume",
                GOAL_INVALID_TRANSITION,
            )
        return await self._save_new(current, phase="active", armed=True)

    async def complete(self, ref: dict) -> GoalRecord:
        return await self._transition(ref, "complete", {"active", "paused", "blocked"}, "complete", armed=False)

    async def block(self, ref: dict, code: str, message: str) -> GoalRecord:
        current = await self._expect_current(ref)
        if current.phase != "active":
            raise GoalError(
                f"cannot block goal \"{current.id}\" from phase \"{current.phase}\"; expected active",
                GOAL_INVALID_TRANSITION,
            )
        reason = _validate_block_reason(code, message)
        return await self._save_new(current, phase="blocked", armed=False, blocked_reason=reason)

    async def clear(self, ref: dict) -> GoalRef:
        """清除目标，保留墓碑；返回 revision 为快照 revision+1 的 ref。"""
        current = await self._expect_current(ref)
        tombstone = GoalRef(current.id, current.revision + 1)
        now = int(time.time() * 1000)
        record = GoalRecord(
            goal_id=current.id,
            revision=tombstone.revision,
            objective=current.objective,
            phase=GOAL_PHASE_NONE,
            max_goal_rounds=current.max_goal_rounds,
            rounds_started=0,
            armed=False,
            created_at=current.created_at,
            updated_at=now,
            cleared=tombstone,
            cleared_at=now,
        )
        await self._save(record)
        return tombstone

    async def _transition(
        self,
        ref: dict,
        operation: str,
        allowed: frozenset[str] | set[str],
        phase: str,
        armed: bool,
    ) -> GoalRecord:
        current = await self._expect_current(ref)
        if current.phase not in allowed:
            raise GoalError(
                f"cannot {operation} goal \"{current.id}\" from phase \"{current.phase}\"; expected {' or '.join(sorted(allowed))}",
                GOAL_INVALID_TRANSITION,
            )
        return await self._save_new(current, phase=phase, armed=armed)

    async def _save_new(
        self,
        current: GoalRecord,
        *,
        phase: str,
        armed: bool,
        blocked_reason: GoalBlockReason | None = None,
    ) -> GoalRecord:
        now = _clamp_updated_at(int(time.time() * 1000), current.updated_at)
        record = GoalRecord(
            goal_id=current.id,
            revision=current.revision + 1,
            objective=current.objective,
            phase=phase,
            max_goal_rounds=current.max_goal_rounds,
            rounds_started=current.rounds_started,
            armed=armed,
            blocked_reason=blocked_reason,
            created_at=current.created_at,
            updated_at=now,
        )
        await self._save(record)
        return record
