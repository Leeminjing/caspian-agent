"""
本文件对外提供上下文压缩的纯函数模块,供 ContextCompressionMiddleware 复用。

对外提供:
    SUMMARY_MESSAGE_ID / SUMMARY_MARKER_KEY / DECISION_TABLE_MESSAGE_ID / CONTRACT_TAG — 常量
    SUMMARY_PROMPT_TEMPLATE — 摘要 prompt 模板(含继承指令与 side-channel 占位符)
    make_token_counter — 构造近似 token 计数函数
    is_anchor — 判断消息是否为锚点(任务合同 / 决策等级表)
    compute_cutoff — 计算摘要区与保留区切点
    plan_compression — 产出 (to_summarize, preserved)
    build_summary_message — 构造固定 id + 标记的摘要消息
    render_side_channels — 从 state 组装确定性工作状态文本
    prune_large_tool_messages — 溢出恢复 L0:截断最大 ToolMessage
    verify_shrink — 后置校验:摘要必须真变小

输入/输出见各函数 docstring。

工作流:
    (1) compute_cutoff 按"配对保护 → 轮边界对齐"确定切点(照搬 langchain SummarizationMiddleware 思路)
    (2) plan_compression 将锚点无条件移入保留区
    (3) 摘要 prompt 注入 state 的确定性 side-channel,模型输出经 verify_shrink 校验

示例:
    plan = plan_compression(messages, keep_messages=20)
    summary = build_summary_message("摘要文本")
"""

import re
import uuid
from functools import partial

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.utils import (
    count_tokens_approximately,
    get_buffer_string,
    trim_messages,
)

SUMMARY_MESSAGE_ID = "caspian-summary"
SUMMARY_MARKER_KEY = "caspian_summary"
DECISION_TABLE_MESSAGE_ID = "decision-table"
CONTRACT_TAG = "<task_contract>"

_PRUNE_NOTICE = "\n\n(内容过长,已由上下文压缩截断;完整输出见对应工具执行结果)"
_MIN_PRUNE_SAVING_RATIO = 0.2
_DECISION_TABLE_VERSION_RE = re.compile(r'<decision_table version="([^"]+)"')
_CONTRACT_PREVIEW_CHARS = 200


def make_token_counter():
    """构造近似 token 计数函数(与 langchain 内置 SummarizationMiddleware 默认一致)。"""
    return partial(count_tokens_approximately, use_usage_metadata_scaling=True)


def is_anchor(message) -> bool:
    """判断消息是否为压缩锚点:决策等级表、任务合同或历史摘要消息。

    输入:
        message — BaseMessage 实例

    输出:
        bool — True 表示该消息必须原样保留,不得进入摘要区

    工作流:
        (1) 决策等级表按固定 id 检测
        (2) 任务合同按内容检测 <task_contract> 标签(其 id 为触发 /commit 消息的 id,压缩侧不可预知)
        (3) 历史摘要按 additional_kwargs 的 caspian_summary 标记检测(防止"摘要的摘要"退化)
    """
    if isinstance(message, SystemMessage) and message.id == DECISION_TABLE_MESSAGE_ID:
        return True
    if isinstance(message, HumanMessage) and CONTRACT_TAG in str(message.content):
        return True
    if (
        isinstance(message, HumanMessage)
        and (message.additional_kwargs or {}).get(SUMMARY_MARKER_KEY)
    ):
        return True
    return False


def compute_cutoff(messages: list, keep_messages: int) -> int | None:
    """计算摘要区与保留区的切点下标(保留区 = [cutoff, n))。

    输入:
        messages: list[BaseMessage] — 完整消息历史
        keep_messages: int — 保留区目标消息条数

    输出:
        int | None — 切点下标;消息数不足或切点退到 0 时返回 None

    工作流:
        (1) cutoff = len - keep_messages
        (2) 切点落在 ToolMessage 上 → 向后推进越过整个连续 ToolMessage 块
        (3) 切点落在带 tool_calls 的 AIMessage 与其配对 ToolMessage 之间 → 推进到配对 ToolMessage 之后
        (4) 回退到最近的 HumanMessage 对齐轮边界
    """
    n = len(messages)
    if n <= keep_messages or keep_messages <= 0:
        return None

    cutoff = n - keep_messages

    # (2) 切点落在 ToolMessage 块上
    if isinstance(messages[cutoff], ToolMessage):
        while cutoff < n and isinstance(messages[cutoff], ToolMessage):
            cutoff += 1
    # (3) 切点落在 AI(tool_call) 与其响应之间
    elif isinstance(messages[cutoff - 1], AIMessage) and messages[cutoff - 1].tool_calls:
        ids = {
            tool_call.get("id")
            for tool_call in messages[cutoff - 1].tool_calls
            if tool_call.get("id")
        }
        while (
            cutoff < n
            and isinstance(messages[cutoff], ToolMessage)
            and messages[cutoff].tool_call_id in ids
        ):
            cutoff += 1

    # (4) 对齐轮边界:保留区以 HumanMessage 开始
    while cutoff > 0 and not isinstance(messages[cutoff], HumanMessage):
        cutoff -= 1

    return cutoff


def plan_compression(messages: list, *, keep_messages: int):
    """产出摘要区与保留区消息列表。

    输入:
        messages: list[BaseMessage] — 完整消息历史
        keep_messages: int — 保留区目标消息条数

    输出:
        tuple[list[BaseMessage], list[BaseMessage]] | None — (to_summarize, preserved);
        消息数不足或摘要区为空时返回 None

    工作流:
        (1) compute_cutoff 确定切点
        (2) 锚点消息无条件进保留区
        (3) 摘要区为空 → 返回 None
    """
    cutoff = compute_cutoff(messages, keep_messages)
    if cutoff is None or cutoff <= 0:
        return None

    to_summarize: list = []
    preserved: list = []
    for index, message in enumerate(messages):
        if index < cutoff and not is_anchor(message):
            to_summarize.append(message)
        else:
            preserved.append(message)

    if not to_summarize:
        return None
    return to_summarize, preserved


def build_summary_message(summary_text: str) -> HumanMessage:
    """构造唯一 id + 标记的摘要消息(每条摘要对应一次压缩 epoch)。

    输入:
        summary_text: str — 摘要正文

    输出:
        HumanMessage — id 为 SUMMARY_MESSAGE_ID 前缀 + 随机后缀,additional_kwargs 含 caspian_summary 标记
    """
    return HumanMessage(
        content=f"以下是对话摘要(由上下文压缩生成),用于替代已压缩的旧消息:\n\n{summary_text}",
        id=f"{SUMMARY_MESSAGE_ID}-{uuid.uuid4().hex}",
        additional_kwargs={"lc_source": "summarization", SUMMARY_MARKER_KEY: True},
    )


def _decision_table_version(messages: list) -> str | None:
    """从 id="decision-table" 的 SystemMessage 内容提取等级表版本号。"""
    for message in messages:
        if isinstance(message, SystemMessage) and message.id == DECISION_TABLE_MESSAGE_ID:
            match = _DECISION_TABLE_VERSION_RE.search(str(message.content))
            if match:
                return match.group(1)
    return None


def render_side_channels(state: dict) -> str:
    """从 state 组装确定性工作状态文本,注入摘要 prompt。

    输入:
        state: dict — AgentState(dict 视图),读 artifacts / delegations / task_contract / messages

    输出:
        str — 确定性工作状态清单文本
    """
    artifacts = state.get("artifacts") or []
    artifact_text = ", ".join(str(item) for item in artifacts) if artifacts else "无"

    from caspian.agents.middlewares.delegation_ledger import render_delegation_ledger

    ledger = render_delegation_ledger(list(state.get("delegations") or []))

    contract = state.get("task_contract")
    if contract:
        preview = str(contract).strip().replace("\n", " ")[:_CONTRACT_PREVIEW_CHARS]
        contract_text = f"已存在({len(str(contract))} 字符): {preview}…"
    else:
        contract_text = "无"

    table_text = _decision_table_version(list(state.get("messages") or [])) or "无"

    lines = [
        f"- 已 present 文件列表(state.artifacts): {artifact_text}",
        f"- 委派账本:\n{ledger}" if ledger else "- 委派账本: 无",
        f"- 任务合同: {contract_text}",
        f"- 决策等级表版本: {table_text}",
    ]
    return "\n".join(lines)


def prune_large_tool_messages(
    messages: list,
    *,
    prune_max_chars: int,
    token_counter,
):
    """溢出恢复 L0:截断 token 数最大的 ToolMessage(同 id 原位替换)。

    输入:
        messages: list[BaseMessage] — 当前消息历史
        prune_max_chars: int — 截断后保留字符数
        token_counter — 近似 token 计数函数

    输出:
        tuple[list[BaseMessage], ToolMessage] | None — (剪枝后的消息列表, 替换后的 ToolMessage);
        无超长 ToolMessage 或节省不足 20% 时返回 None
    """
    candidates = [
        message
        for message in messages
        if isinstance(message, ToolMessage)
        and isinstance(message.content, str)
        and len(message.content) > prune_max_chars
    ]
    if not candidates:
        return None

    largest = max(candidates, key=lambda message: len(message.content))
    truncated = largest.content[:prune_max_chars] + _PRUNE_NOTICE
    replacement = largest.model_copy(update={"content": truncated})

    saved = token_counter([largest]) - token_counter([replacement])
    total = token_counter(messages)
    if total > 0 and saved < _MIN_PRUNE_SAVING_RATIO * total:
        return None

    new_messages = [replacement if message is largest else message for message in messages]
    return new_messages, replacement


def verify_shrink(summary_message, to_summarize: list, token_counter) -> bool:
    """后置校验:摘要消息 tokens 必须严格小于被替换消息 tokens。

    输入:
        summary_message — 生成的摘要消息
        to_summarize: list[BaseMessage] — 被替换的摘要区消息
        token_counter — 近似 token 计数函数

    输出:
        bool — True 表示压缩确实变小,可以替换
    """
    return token_counter([summary_message]) < token_counter(to_summarize)


SUMMARY_PROMPT_TEMPLATE = """<role>对话历史压缩助手</role>

<任务>
你是 Caspian 的上下文压缩器。下面是一段对话历史与系统确定性工作状态。你的输出将**替换**被压缩的旧消息,近期消息保持原文。压缩必须保证模型恢复执行时不丢失关键上下文、不重复已完成的工作。
</任务>

<输出要求>
- 用中文输出,紧凑 Markdown,总长度尽量短(目标:不超过输入历史的四分之一)。
- 只输出摘要正文,禁止任何前后缀、解释或问候。
- 必须包含以下检查点小节,没有内容的节写"无":
  ## 用户目标
  ## 已完成
  ## 正在进行
  ## 下一步
  ## 涉及文件
  ## 错误与教训
  ## 约束与已决策
</输出要求>

<摘要继承>
若历史中已存在一段"对话摘要"(内容以"以下是对话摘要"开头),必须执行:
- PRESERVE:保留仍然成立的历史信息(决策、约束、用户偏好、已完成工作);
- ADD:补充自上次摘要之后的新信息;
- UPDATE:更新已过时的状态(如"正在进行"改为"已完成"、委派状态变化、等级表版本变化);
- 禁止整段复制旧摘要,禁止嵌套多层"摘要的摘要"。
</摘要继承>

<确定性工作状态>
以下来自系统状态,不是对话推测。摘要必须与之一致,不得编造或遗漏:
{side_channels}
</确定性工作状态>

<messages>
{history}
</messages>"""


if __name__ == "__main__":
    # ponytail: assert 自检,最小可运行验证(切点配对保护 + 锚点保留 + 摘要消息标记)
    counter = make_token_counter()

    def _ai(tool_call_ids=("tc-1",)):
        return AIMessage(
            content="",
            id="ai-1",
            tool_calls=[{"name": "bash", "args": {}, "id": tid} for tid in tool_call_ids],
        )

    def _tool(tid="tc-1"):
        return ToolMessage(content="result", tool_call_id=tid, id=f"tool-{tid}")

    # 切点落在 AI(tool_call) 与 ToolMessage 之间 → 推进越过配对
    seq = [HumanMessage(content="u1", id="h1"), _ai(), _tool(), HumanMessage(content="u2", id="h2")]
    cutoff = compute_cutoff(seq, keep_messages=2)
    assert cutoff is not None and seq[cutoff].id == "h2", cutoff

    # 切点落在 ToolMessage 块上 → 推进越过整个块
    seq = [HumanMessage(content="u1", id="h1"), _ai(), _tool(), _tool("tc-2"), HumanMessage(content="u2", id="h2")]
    cutoff = compute_cutoff(seq, keep_messages=2)
    assert cutoff is not None and seq[cutoff].id == "h2", cutoff

    # 锚点进保留区
    contract = HumanMessage(content="<task_contract>\n合同\n</task_contract>", id="h1")
    table = SystemMessage(content='<decision_table version="v1">', id="decision-table")
    seq = [contract, table, HumanMessage(content="u1", id="h2"), _ai(), _tool(), HumanMessage(content="u2", id="h3")]
    plan = plan_compression(seq, keep_messages=2)
    assert plan is not None
    to_summarize, preserved = plan
    assert all(is_anchor(message) for message in preserved if is_anchor(message))
    assert contract in preserved and table in preserved
    assert contract not in to_summarize and table not in to_summarize

    # 摘要消息唯一 id + 标记
    summary = build_summary_message("摘要")
    assert summary.id.startswith(SUMMARY_MESSAGE_ID + "-")
    assert summary.additional_kwargs.get(SUMMARY_MARKER_KEY) is True
    assert summary.additional_kwargs.get("lc_source") == "summarization"
    assert build_summary_message("摘要").id != summary.id

    # 后置校验:更大 → False;更小 → True
    big_list = [HumanMessage(content="x" * 1000, id="big")]
    small_msg = HumanMessage(content="x" * 10, id="small")
    small_list = [small_msg]
    assert verify_shrink(small_msg, big_list, counter) is True
    assert verify_shrink(big_list[0], small_list, counter) is False

    # 剪枝保留 tool_call_id 且截断
    long_tool = ToolMessage(content="y" * 5000, tool_call_id="tc-9", id="tool-9")
    pruned = prune_large_tool_messages([long_tool], prune_max_chars=800, token_counter=counter)
    assert pruned is not None
    _, replacement = pruned
    assert replacement.tool_call_id == "tc-9"
    assert len(replacement.content) == 800 + len(_PRUNE_NOTICE)

    # trim_messages 与 get_buffer_string 原语可用
    trimmed = trim_messages([HumanMessage(content="a"), HumanMessage(content="b")], max_tokens=100, token_counter=counter)
    assert isinstance(get_buffer_string(trimmed), str)

    print("context_compression_plan 自检通过")
