"""本文件对外提供承诺层公共类型与兼容辅助函数的惰性导出。

输入为调用方请求的公共名称；输出为对应职责模块中的对象。
工作流只在实际访问名称时导入模块，避免文件解析、治理服务和工作流之间的初始化环。
示例：`from caspian.agents.commitment import CommitmentMiddleware, TaskEnvelope`。
"""

from importlib import import_module

_EXPORTS = {
    "_build_final_message": "artifacts", "_write_contract": "artifacts", "_write_knowledge": "artifacts",
    "ReviewedDelegator": "delegation",
    "CommitmentMiddleware": "middleware", "_commit_instruction": "middleware", "_extract_uploads_tag": "middleware",
    "_SearchResultParser": "references",
    "CommitmentState": "schemas", "ReviewOutput": "schemas", "TaskEnvelope": "schemas", "WorkerOutput": "schemas",
    "_contains_unresolved_versions": "stage_rules", "_context7_candidate_version": "stage_rules",
    "_context7_stable_version": "stage_rules", "_extract_structured": "stage_rules",
    "_filter_stage_four_result": "stage_rules", "_has_open_conflicts": "stage_rules",
    "_normalize_stage_three_result": "stage_rules", "_safe_segment": "stage_rules",
    "_stage_four_needs_review": "stage_rules", "_stage_timeout": "stage_rules",
    "_validate_stage_result": "stage_rules",
    "_build_supervisor": "workflow", "_human_payload": "workflow",
    "_review_human_revision": "workflow", "build_delegate_with_review_tool": "workflow",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(name)
    value = getattr(import_module(f"caspian.agents.commitment.{module}"), name)
    globals()[name] = value
    return value
