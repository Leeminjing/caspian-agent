"""
本文件对外提供 knowledge_query 内置工具：lead agent 查询知识库的唯一切口。

对外提供:
    knowledge_query_tool — 经共享管线 run_governed_query 执行向量召回 + LLM 冲突判定 +
        离散等级治理，产出最终证据集

输入:
    query: str — 自然语言查询
    top_k: int — 召回条数（钳制 [1,20]，默认 5）
    runtime: ToolRuntime — 注入运行时（取 runtime.store 与 runtime.context.user_id/model_name）

输出:
    str — 给模型使用的最终证据集文本（含被压命题注解与治理提示）；被压制证据
        不进模型文本，其账本经自定义事件 knowledge_governance 推给前端面板

具体工作流:
    (1) 从 runtime 取 store 与 user_id；缺失时返回说明性错误字符串
    (2) 调用共享管线 run_governed_query（search_knowledge 向量召回 → judge_conflicts
        冲突判定 → govern 等级治理）；judge 失败返回显式错误文本，不静默跳过治理
    (3) 经 get_stream_writer 发射 {type: "knowledge_governance", ...} 自定义事件
        （前端渲染证据/账本面板）
    (4) 组装模型文本：最终证据集 + 部分压制注解 + 同等级冲突/潜在分歧提示

示例:
    result = await knowledge_query_tool.ainvoke({
        "query": "功能 A 是否已经废弃？",
        "top_k": 5,
    })
"""

from langchain_core.tools import tool
from langgraph.config import get_stream_writer
from langgraph.prebuilt import ToolRuntime

from caspian.knowledge.pipeline import run_governed_query


def _runtime_context(runtime: ToolRuntime | None) -> dict:
    ctx = getattr(runtime, "context", None)
    return ctx if isinstance(ctx, dict) else {}


def _emit_governance_event(payload: dict) -> None:
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    writer(payload)


def _format_evidence_text(result) -> str:
    lines = ["以下证据已经离散等级治理，可作为回答依据："]
    for i, evidence in enumerate(result.final_evidence_set, start=1):
        lines.append(f"{i}. [{evidence.level_display}] {evidence.content}")
        if evidence.suppressed_claims:
            lines.append(
                f"   ⚠ 该证据中以下命题已被等级治理压制，不得作为依据："
                + "；".join(evidence.suppressed_claims)
            )
    suppressed = [item for item in result.ledger if item.status == "suppressed"]
    if suppressed:
        lines.append(
            f"另有 {len(suppressed)} 条证据因与更高等级证据冲突被压制"
            "（不得引用其内容，治理账本已展示给用户）。"
        )
    if result.notes:
        lines.append("治理提示：")
        lines.extend(f"- {note}" for note in result.notes)
    return "\n".join(lines)


@tool
async def knowledge_query(
    query: str,
    top_k: int = 5,
    runtime: ToolRuntime = None,
) -> str:
    """在受治理的知识库中检索证据（离散等级治理 RAG）。

    When to use: 回答与已收录知识相关的问题时，先调用本工具获取经过等级治理的
    最终证据集；知识库中的证据按权威等级（L0-L3/未评级）入库，明确冲突时高等级
    压制低等级。不要自行臆测知识库内容，先检索。

    When NOT to use: 与知识库无关的对话、代码编写、一般常识问答不需要调用。

    Args:
        query: 自然语言查询文本。
        top_k: 召回候选条数，默认 5。
    """
    store = getattr(runtime, "store", None)
    user_id = _runtime_context(runtime).get("user_id")
    if store is None or user_id is None:
        return "知识库不可用：缺少 store 或 user_id 运行上下文。"

    model_name = _runtime_context(runtime).get("model_name")
    result, candidates, error = await run_governed_query(
        store,
        str(user_id),
        query,
        top_k,
        model_name,
    )
    if error is not None:
        return (
            "知识检索治理失败：冲突判定出错，本次不采用知识库证据。"
            f"（{error}）"
        )
    if result is None:
        return "知识库中没有检索到相关内容。"

    _emit_governance_event(
        {
            "type": "knowledge_governance",
            "query": query,
            "ledger": [item.model_dump() for item in result.ledger],
            "notes": result.notes,
        }
    )
    return _format_evidence_text(result)
