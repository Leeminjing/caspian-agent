"""
本文件对外提供入库评级器的两个核心函数：rate_level（软评级）与 map_dimensions_to_level（硬映射）。

对外提供:
    rate_level — 单次结构化调用模型，对一条待入库知识评出四维分 + 信心 + 理由
    map_dimensions_to_level — 纯函数，把维度分按否决/封顶规则映射为离散等级与命中规则

输入:
    rate_level:
        content: str — 知识正文
        source: str — 来源名称
        source_url: str | None — 来源链接
        mechanical: dict — 机械域名信号（source_type / matched_domain），仅供参考可推翻
        model: BaseChatModel — 已构造的聊天模型
        timeout_seconds: float — 单次模型请求超时，默认 60

输出:
    rate_level → RatingOutput（dimensions/confidence/reason/claim_domain/unrated_reason）
    map_dimensions_to_level → tuple[int, str] — (level, mapping_rule)

具体工作流:
    (1) 组装 payload 与 system prompt
    (2) 结构化输出（function_calling）单次批量调用；失败回退纯文本解析（复用 judge 的 fenced JSON 解析）
    (3) 两条路径都失败 → 抛异常，由调用方降级为未评级（fail-safe，不猜）

设计依据见 design.md D3/D4/D8；结构对齐 judge.py 的两段式。
"""

import asyncio
import json
import logging

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from caspian.knowledge.json_parsing import parse_fenced_or_raw
from caspian.knowledge.schemas import RatingDimensions, RatingOutput

logger = logging.getLogger(__name__)

_RATER_SYSTEM_PROMPT = """你是知识证据的权威评级器。给定一条待入库知识（content）、来源名称（source）、来源链接（source_url），以及仅供你参考、可推翻的机械域名信号（source_type / matched_domain），评估"这条知识在其所断言命题的领域内有多大的话语权"。

对每个维度给出 1..3 的整数分（1=最弱，3=最强）：

1. primary_source（一手程度）：
   1=二手转述/汇总；2=间接（官方博客/论文/专业二次整理）；3=一手（官方文档/源码/规范/原始实验/官方发布）。
2. domain_fit（来源在该 claim 领域有无资格）：
   1=领域外；2=相关但非权威；3=该领域权威。
   判断依据是"这条知识断言的命题属于哪个领域"，而不是"来源自身属于哪个领域"。
   例如：某公司官方博客讨论另一门技术的实践，domain_fit 应为 1。
3. evidence（证据强度 + 可核验）：
   1=无证据断言；2=部分/间接证据；3=可验证直接证据（链接/版本/示例/可复现）。
4. specificity（是否精确针对该命题）：
   1=泛泛而谈；2=部分相关；3=精确针对。

同时给出：
- claim_domain：这条知识所断言命题的领域（简短短语，用于审计）。
- confidence：你对本次评分的信心（0..1）。
- reason：一句话说明评分理由。

如果你无法可靠评分（信息严重不足、无法推断命题领域），把 unrated_reason 设为一句说明；否则 unrated_reason 必须为 null。此时 dimensions 仍填你的最佳估计值，最终是否未评级由代码决定。

不要输出最终等级 L1/L2/L3——那由代码根据维度确定性映射，你只负责给出维度分与理由。"""


def map_dimensions_to_level(dimensions: RatingDimensions) -> tuple[int, str]:
    """按否决/封顶规则把维度分映射为离散等级，返回 (level, mapping_rule)。

    输入:
        dimensions: RatingDimensions — 评级器产出的四维分（各 1..3）

    输出:
        tuple[int, str] — (level ∈ {1,2,3}, 命中的封顶规则描述；无命中 → L3)

    规则（取最严格上限）:
        primary_source==1 → cap L1 ; primary_source==2 → cap L2
        domain_fit==1     → cap L1 ; domain_fit==2     → cap L2
        evidence==1       → cap L1
        specificity==1    → cap L2
    """
    caps: list[tuple[int, str]] = []
    if dimensions.primary_source == 1:
        caps.append((1, "primary_source==1 → cap L1"))
    elif dimensions.primary_source == 2:
        caps.append((2, "primary_source==2 → cap L2"))
    if dimensions.domain_fit == 1:
        caps.append((1, "domain_fit==1 → cap L1"))
    elif dimensions.domain_fit == 2:
        caps.append((2, "domain_fit==2 → cap L2"))
    if dimensions.evidence == 1:
        caps.append((1, "evidence==1 → cap L1"))
    if dimensions.specificity == 1:
        caps.append((2, "specificity==1 → cap L2"))

    if not caps:
        return 3, "no cap → L3"
    level, rule = min(caps, key=lambda c: c[0])
    return level, rule


def decide_level(
    rating: RatingOutput,
    *,
    confidence_threshold: float = 0.5,
) -> tuple[int | None, str]:
    """把评级器输出决策为离散等级（含未评级三出口），返回 (level, rule)。

    输入:
        rating: RatingOutput — 评级器结构化输出
        confidence_threshold: float — 信心阈值，低于则落未评级

    输出:
        tuple[int | None, str] — (level ∈ {None,1,2,3}, 决策说明)

    未评级三出口（任一命中 → level=None）:
        (1) rating.unrated_reason 非空（信息不足）
        (2) rating.confidence < confidence_threshold
        （评级失败在调用方 rate_level 抛异常处处理，不在本纯函数内）
    """
    if rating.unrated_reason:
        return None, f"unrated: {rating.unrated_reason}"
    if rating.confidence < confidence_threshold:
        return None, f"unrated: confidence {rating.confidence:.2f} < {confidence_threshold}"
    level, rule = map_dimensions_to_level(rating.dimensions)
    return level, rule


async def rate_level(
    content: str,
    source: str,
    source_url: str | None,
    mechanical: dict,
    model: BaseChatModel,
    *,
    timeout_seconds: float = 60,
) -> RatingOutput:
    """单次调用模型，对一条待入库知识评出四维分（软评级，不给最终 level）。

    输入:
        content / source / source_url — 待评级知识
        mechanical — 机械域名信号（参考输入，可推翻）
        model — 已构造的聊天模型
        timeout_seconds — 单次模型请求超时

    输出:
        RatingOutput

    异常:
        结构化与纯文本两条路径都失败时抛出异常，由调用方降级未评级。
    """
    payload = json.dumps(
        {
            "content": content,
            "source": source,
            "source_url": source_url,
            "mechanical_signal": mechanical,
        },
        ensure_ascii=False,
    )
    input_message = HumanMessage(content=payload)
    bound_model = model.bind(max_tokens=4096)

    try:
        structured_model = bound_model.with_structured_output(
            RatingOutput,
            method="function_calling",
        )
        async with asyncio.timeout(timeout_seconds):
            parsed = await structured_model.ainvoke(
                [SystemMessage(content=_RATER_SYSTEM_PROMPT), input_message]
            )
        if isinstance(parsed, RatingOutput):
            return parsed
        raise ValueError("结构化输出类型异常")
    except Exception as exc:
        logger.warning(
            "rater 结构化调用失败（%s），回退纯文本解析", type(exc).__name__
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                raw = await bound_model.ainvoke(
                    [
                        SystemMessage(content=_RATER_SYSTEM_PROMPT),
                        input_message,
                        HumanMessage(content="只返回上述 JSON，不要解释、不要 Markdown。"),
                    ]
                )
            data = parse_fenced_or_raw(str(raw.content))
            return RatingOutput.model_validate(data)
        except Exception:
            logger.error("rater 兜底解析也失败", exc_info=True)
            raise
