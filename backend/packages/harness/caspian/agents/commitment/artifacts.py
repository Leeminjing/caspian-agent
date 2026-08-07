"""
本文件对外提供知识文件、任务合同和最终 HumanMessage 内容的持久化函数。

对外提供:
    _write_knowledge — 阶段六 knowledge 结果写入 knowledge 目录，返回相对路径列表
    _write_contract — 阶段七合同写入 requirements/{thread_id}/task-contract.md，
                      并在提供阶段2/3结果时同步写入决策等级表（best-effort）
    _build_final_message — 组装交给 lead agent 的最终合同消息

输入:
    _write_knowledge:
        result: dict — 阶段六 knowledge 结果
    _write_contract:
        thread_id: str — 线程标识
        result: dict — 阶段七合同结果（contract_markdown）
        stage_two_result: dict | None — 阶段2 artifacts（requirements/discarded_requirements）
        stage_three_result: dict | None — 阶段3 artifacts（逐条优先级）
    _build_final_message:
        contract: str — 合同正文
        knowledge_files: list[str] — 已写入的知识文件相对路径

输出:
    _write_knowledge → list[str] — 已写入的 knowledge 文件相对路径
    _write_contract → tuple[str, str] — 合同正文及 task-contract.md 相对路径
    _build_final_message → str — 包含 task_contract 和 theoretical foundation 标签的最终消息

具体工作流:
    (1) 校验技术名、版本和 thread_id 的安全路径片段。
    (2) 将官方知识写入根目录 knowledge。
    (3) 将合同写入 requirements/{thread_id}/task-contract.md。
    (4) 合同写入成功后，若提供阶段2/3结果，同步写入决策等级表（失败仅日志，不阻断）。
    (5) 读取已确认知识并组装交给 lead agent 的第一条 HumanMessage 内容。

示例:
    contract, path = _write_contract(thread_id, stage_seven_result)
    contract, path = _write_contract(thread_id, stage_seven_result, stage_two, stage_three)
"""

from pathlib import Path
from typing import Any

from caspian.agents.commitment.decision_table import write_decision_table
from caspian.agents.commitment.stage_rules import _safe_segment, _slug_segment

_PROJECT_ROOT = Path(__file__).resolve().parents[6]

def _write_knowledge(result: dict[str, Any]) -> list[str]:
    files: list[str] = []
    for item in result.get("knowledge", []):
        if not isinstance(item, dict):
            raise ValueError("knowledge 项必须是对象")
        technology = str(item.get("technology", "")).strip()
        technology_slug = _slug_segment(technology, "technology")
        version = _safe_segment(str(item.get("version", "")), "version")
        source = str(item.get("source_url", "")).strip()
        content = str(item.get("content", "")).strip()
        if not source.startswith("http") or not content:
            raise ValueError("knowledge 项必须包含官方 source_url 和 content")
        relative_path = Path("knowledge") / f"{technology_slug}-{version}.md"
        path = _PROJECT_ROOT / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# {technology} {version}\n\nSource: {source}\n\n{content}\n",
            encoding="utf-8",
        )
        files.append(relative_path.as_posix())
    if not files:
        raise ValueError("阶段6没有产生知识文件")
    return files

def _write_contract(
    thread_id: str,
    result: dict[str, Any],
    stage_two_result: dict[str, Any] | None = None,
    stage_three_result: dict[str, Any] | None = None,
) -> tuple[str, str]:
    safe_thread_id = _safe_segment(thread_id, "thread_id")
    contract = str(result.get("contract_markdown", "")).strip()
    if not contract:
        raise ValueError("合同内容为空")
    relative_path = Path("requirements") / safe_thread_id / "task-contract.md"
    path = _PROJECT_ROOT / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contract + "\n", encoding="utf-8")
    if stage_two_result is not None and stage_three_result is not None:
        write_decision_table(
            safe_thread_id,
            stage_two_result,
            stage_three_result,
            root=_PROJECT_ROOT,
        )
    return contract, relative_path.as_posix()

def _build_final_message(contract: str, knowledge_files: list[str]) -> str:
    sections = [f"<task_contract>\n{contract}\n</task_contract>"]
    for name in knowledge_files:
        content = (_PROJECT_ROOT / name).read_text(encoding="utf-8")
        sections.append(
            f'<theoretical foundation source="{name}">\n'
            f"{content}\n"
            "</theoretical foundation>"
        )
    return "\n\n".join(sections)
