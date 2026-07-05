"""
本文件对外提供 RunStore 抽象基类，声明 run 元数据存储的统一接口。

对外提供:
    RunStore(ABC) — run 元数据存储的抽象基类，定义写入、读取、更新、删除、聚合五类共九个抽象方法

输入: 无 — 本文件为纯接口定义
输出: RunStore 抽象基类

抽象方法:
    写入:  put(run_id, **fields) → None
    读取:  get(run_id, user_id=None) → dict | None
           list_by_thread(thread_id, user_id=None, limit=100) → list[dict]
           list_pending(before=None) → list[dict]
    更新:  update_status(run_id, status, error=None) → None
           update_model_name(run_id, model_name) → None
           update_run_completion(run_id, **stats) → None
    删除:  delete(run_id) → None
    聚合:  aggregate_tokens_by_thread(thread_id) → dict

示例:
    class MyStore(RunStore):
        def put(self, run_id, **fields): ...
        # ... 实现其余 8 个方法
"""

from abc import ABC, abstractmethod


class RunStore(ABC):
    """run 元数据存储的抽象基类。子类必须实现全部九个抽象方法。"""

    # === 写入 ===

    @abstractmethod
    def put(self, run_id: str, **fields) -> None:
        """新建一条 run 记录。

        输入:
            run_id: str — run 的唯一标识
            **fields — 完整元数据（thread_id、assistant_id、user_id、status 等）

        输出:
            None
        """
        ...

    # === 读取 ===

    @abstractmethod
    def get(self, run_id: str, user_id: str | None = None) -> dict | None:
        """按 run_id 读取单条记录。

        输入:
            run_id: str — run 的唯一标识
            user_id: str | None — 可选，权限过滤

        输出:
            dict | None — 命中返回完整记录，未命中返回 None
        """
        ...

    @abstractmethod
    def list_by_thread(
        self, thread_id: str, user_id: str | None = None, limit: int = 100
    ) -> list[dict]:
        """列出指定 thread 下的所有 run。

        输入:
            thread_id: str — 线程标识
            user_id: str | None — 可选，权限过滤
            limit: int — 最大返回条数，默认 100

        输出:
            list[dict] — 该 thread 下的 run 列表
        """
        ...

    @abstractmethod
    def list_pending(self, before: str | None = None) -> list[dict]:
        """列出所有未完成的 run，用于系统启动时恢复。

        输入:
            before: str | None — 可选时间点（ISO 8601），仅返回此时间之前的记录

        输出:
            list[dict] — 所有未完成的 run
        """
        ...

    # === 更新 ===

    @abstractmethod
    def update_status(self, run_id: str, status: str, error: str | None = None) -> None:
        """轻量更新：仅修改状态字段。

        输入:
            run_id: str — run 的唯一标识
            status: str — 新状态值
            error: str | None — 可选，错误信息

        输出:
            None
        """
        ...

    @abstractmethod
    def update_model_name(self, run_id: str, model_name: str) -> None:
        """轻量更新：仅修改模型名。

        输入:
            run_id: str — run 的唯一标识
            model_name: str — 模型名称

        输出:
            None
        """
        ...

    @abstractmethod
    def update_run_completion(self, run_id: str, **stats) -> None:
        """重量更新：run 完成时一次性写入所有统计信息。

        输入:
            run_id: str — run 的唯一标识
            **stats — 统计字段（token 用量、调用次数、首末消息时间等）

        输出:
            None
        """
        ...

    # === 删除 ===

    @abstractmethod
    def delete(self, run_id: str) -> None:
        """删除一条 run 记录。

        输入:
            run_id: str — run 的唯一标识

        输出:
            None
        """
        ...

    # === 聚合 ===

    @abstractmethod
    def aggregate_tokens_by_thread(self, thread_id: str) -> dict:
        """统计指定 thread 下的 token 用量。

        输入:
            thread_id: str — 线程标识

        输出:
            dict — 包含总 token、按模型分组、按调用方分组的统计
        """
        ...
