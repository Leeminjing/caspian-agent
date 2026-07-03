"""
本文件对外提供 MemoryRunStore 类，作为 RunStore 抽象接口的内存实现。

对外提供:
    MemoryRunStore(RunStore) — RunStore 的内存实现

内部存储结构:
    self._runs: dict[str, dict] — key 为 run_id，value 为该 run 的所有字段
    内层 dict 字段结构与 RunRecord 属性一一对应

输入:
    MemoryRunStore.__init__(): 无参数，初始化空的 _runs 字典

输出:
    MemoryRunStore 实例

具体工作流:
    各方法直接操作 self._runs 字典，读写均为内存操作，无 I/O。

示例:
    store = MemoryRunStore()
    store.put("run-001", thread_id="th-001", status="pending")
    record = store.get("run-001")
    runs = store.list_by_thread("th-001")
"""

import logging
from datetime import datetime, timezone

from lead_agent.runtime.runs.store.base import RunStore

logger = logging.getLogger(__name__)


class MemoryRunStore(RunStore):
    """RunStore 的内存实现，所有数据存储在进程内 dict 中。"""

    def __init__(self) -> None:
        self._runs: dict[str, dict] = {}

    # === 写入 ===

    def put(self, run_id: str, **fields) -> None:
        self._runs[run_id] = fields

    # === 读取 ===

    def get(self, run_id: str, user_id: str | None = None) -> dict | None:
        record = self._runs.get(run_id)
        if record is None:
            return None
        if user_id is not None and record.get("user_id") != user_id:
            return None
        return dict(record)

    def list_by_thread(
        self, thread_id: str, user_id: str | None = None, limit: int = 100
    ) -> list[dict]:
        result: list[dict] = []
        for record in self._runs.values():
            if record.get("thread_id") != thread_id:
                continue
            if user_id is not None and record.get("user_id") != user_id:
                continue
            result.append(dict(record))
            if len(result) >= limit:
                break
        return result

    def list_pending(self, before: str | None = None) -> list[dict]:
        pending_statuses = {"pending", "running"}
        result: list[dict] = []
        for record in self._runs.values():
            if record.get("status") not in pending_statuses:
                continue
            if before is not None:
                created_at = record.get("created_at", "")
                if created_at and created_at >= before:
                    continue
            result.append(dict(record))
        return result

    # === 更新 ===

    def update_status(self, run_id: str, status: str, error: str | None = None) -> None:
        record = self._runs.get(run_id)
        if record is None:
            logger.warning("MemoryRunStore.update_status: run_id '%s' 不存在，跳过", run_id)
            return
        record["status"] = status
        if error is not None:
            record["error"] = error
        record["updated_at"] = datetime.now(timezone.utc).isoformat()

    def update_model_name(self, run_id: str, model_name: str) -> None:
        record = self._runs.get(run_id)
        if record is None:
            logger.warning("MemoryRunStore.update_model_name: run_id '%s' 不存在，跳过", run_id)
            return
        record["model_name"] = model_name
        record["updated_at"] = datetime.now(timezone.utc).isoformat()

    def update_run_completion(self, run_id: str, **stats) -> None:
        record = self._runs.get(run_id)
        if record is None:
            logger.warning("MemoryRunStore.update_run_completion: run_id '%s' 不存在，跳过", run_id)
            return
        record.update(stats)
        record["updated_at"] = datetime.now(timezone.utc).isoformat()

    # === 删除 ===

    def delete(self, run_id: str) -> None:
        self._runs.pop(run_id, None)

    # === 聚合 ===

    def aggregate_tokens_by_thread(self, thread_id: str) -> dict:
        total_tokens = 0
        by_model: dict[str, int] = {}
        by_caller: dict[str, int] = {}

        for record in self._runs.values():
            if record.get("thread_id") != thread_id:
                continue
            tokens = record.get("total_tokens", 0) or 0
            total_tokens += tokens

            model_name = record.get("model_name", "unknown") or "unknown"
            by_model[model_name] = by_model.get(model_name, 0) + tokens

            caller = record.get("assistant_id", "unknown") or "unknown"
            by_caller[caller] = by_caller.get(caller, 0) + tokens

        return {
            "thread_id": thread_id,
            "total_tokens": total_tokens,
            "by_model": by_model,
            "by_caller": by_caller,
        }
