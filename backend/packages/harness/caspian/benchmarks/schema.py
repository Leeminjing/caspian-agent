"""决策表 benchmark 的任务 schema:加载与校验任务语料 YAML。

任务形状(见 tasks/corpus.yaml):
    id: str                 任务唯一标识
    table: list[TableRowSpec]  已批准决策表(requirement + priority 1/2/3)
    instruction: str        引导 Agent 的任务指令
    adversarial_turns: list[str]  对抗轮(降级/注入/删除 MUST)
    oracle: OracleSpec      机械 oracle 断言

本模块只做加载与结构校验,不涉及评分与执行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

_VALID_PRIORITIES = {1, 2, 3}


@dataclass
class TableRowSpec:
    requirement: str
    priority: int
    decision: str = "保留"


@dataclass
class OracleSpec:
    forbidden_downgrade: list[str] = field(default_factory=list)
    forbidden_remove: list[str] = field(default_factory=list)
    forbidden_inject: list[str] = field(default_factory=list)


@dataclass
class TaskSpec:
    id: str
    table: list[TableRowSpec]
    instruction: str
    adversarial_turns: list[str]
    oracle: OracleSpec


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _parse_row(item, task_id: str) -> TableRowSpec:
    if not isinstance(item, dict):
        raise ValueError(f"{task_id}: table 行必须是对象,实际 {item!r}")
    requirement = str(item.get("requirement", "") or "").strip()
    priority = item.get("priority")
    _require(bool(requirement), f"{task_id}: table 行缺少 requirement")
    _require(
        isinstance(priority, int) and priority in _VALID_PRIORITIES,
        f"{task_id}: 行 '{requirement}' 的 priority 必须是 1/2/3,实际 {priority!r}",
    )
    decision = str(item.get("decision", "保留") or "保留").strip()
    _require(decision in {"保留", "丢弃"}, f"{task_id}: 行 '{requirement}' 的 decision 非法")
    return TableRowSpec(requirement=requirement, priority=priority, decision=decision)


def _parse_oracle(item, task_id: str) -> OracleSpec:
    if item is None:
        return OracleSpec()
    if not isinstance(item, dict):
        raise ValueError(f"{task_id}: oracle 必须是对象")
    result: dict[str, list[str]] = {}
    for key in ("forbidden_downgrade", "forbidden_remove", "forbidden_inject"):
        value = item.get(key, [])
        if value is None:
            value = []
        _require(isinstance(value, list), f"{task_id}: oracle.{key} 必须是列表")
        for entry in value:
            _require(isinstance(entry, str) and entry.strip(), f"{task_id}: oracle.{key} 含非法项")
        result[key] = [str(e).strip() for e in value]
    return OracleSpec(**result)


def _parse_task(item, index: int) -> TaskSpec:
    if not isinstance(item, dict):
        raise ValueError(f"corpus 第 {index} 项必须是对象")
    task_id = str(item.get("id", "") or "").strip()
    _require(bool(task_id), f"corpus 第 {index} 项缺少 id")
    table_raw = item.get("table")
    _require(isinstance(table_raw, list) and table_raw, f"{task_id}: table 必须是非空列表")
    table = [_parse_row(row, task_id) for row in table_raw]
    instruction = str(item.get("instruction", "") or "").strip()
    _require(bool(instruction), f"{task_id}: 缺少 instruction")
    turns = item.get("adversarial_turns")
    _require(isinstance(turns, list) and turns, f"{task_id}: adversarial_turns 必须是非空列表")
    for turn in turns:
        _require(isinstance(turn, str) and turn.strip(), f"{task_id}: adversarial_turns 含非法项")
    oracle = _parse_oracle(item.get("oracle"), task_id)
    return TaskSpec(
        id=task_id,
        table=table,
        instruction=instruction,
        adversarial_turns=[str(t) for t in turns],
        oracle=oracle,
    )


def load_corpus(path: str | Path) -> list[TaskSpec]:
    """加载任务语料 YAML 并校验,非法即抛 ValueError。

    输入:
        path: str | Path — corpus.yaml 路径(顶层为 tasks: [...] 或 [...] 列表)
    输出:
        list[TaskSpec] — 校验通过的任务列表
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("tasks", [])
    _require(isinstance(raw, list) and raw, f"{path}: corpus 必须含非空 tasks 列表")
    return [_parse_task(item, i) for i, item in enumerate(raw)]
