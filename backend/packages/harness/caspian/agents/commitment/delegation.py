"""
本文件对外提供 ReviewedDelegator，封装隔离的 Worker-Evaluator 审核闭环。

输入:
    model — 创建 Worker 和 Evaluator 所使用的 BaseChatModel。
    context7_tools — 仅承诺层内部可见的 Context7 BaseTool 列表。
    TaskEnvelope — 当前阶段指令、上下文和验收条件。
    Supervisor messages — 原始用户输入和此前阶段最终 ToolMessage。

输出:
    WorkerOutput | None — 最多三次审核后通过的阶段结果；全部失败时为 None。
    str — 未通过时最后一轮结构校验或 Evaluator 反馈。

具体工作流:
    (1) 为当前阶段创建干净的 Worker 上下文并生成候选结果。
    (2) 对特殊阶段执行确定性 Context7 查询、版本处理或结果规范化。
    (3) 使用独立 Evaluator 按 TaskEnvelope 验收条件审核结果。
    (4) 单次语义尝试内有限重试模型传输故障，并区分截断、资源不足和结构错误。
    (5) Evaluator 不合格时携带反馈创建新的 Worker，最多执行三次。
    (6) 流式发布 Worker 和 Evaluator 各自的真实 messages，不读取私密 reasoning 字段。
    (7) 工具只向 Supervisor 返回最终通过结果或最后反馈。

示例:
    delegator = ReviewedDelegator(model, context7_tools)
    output, feedback = await delegator.run(envelope)
"""

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

from langchain.agents import create_agent
from langchain.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.messages import BaseMessage
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool, StructuredTool
from httpx import TransportError
from pydantic import BaseModel, ValidationError

from caspian.agents.commitment.references import find_reference_urls
from caspian.agents.commitment.schemas import (
    ReviewOutput,
    StageFourResult,
    StageTwoResult,
    TaskEnvelope,
    TechnologySelection,
    WorkerOutput,
)
from caspian.agents.commitment.stage_rules import (
    _LATEST_STABLE_VERSION,
    _MAX_REVIEW_ATTEMPTS,
    _context7_candidate_version,
    _context7_library_id,
    _context7_stable_version,
    _context7_version_evidence,
    _extract_structured,
    _filter_stage_four_result,
    _json,
    _merge_table_escalations,
    _normalize_stage_three_result,
    _source_text,
    _stage_result,
    _stage_three_requirements,
    _validate_stage_result,
)
from caspian.agents.commitment.tracing import (
    emit_commitment_messages,
    emit_commitment_trace,
)


class _ModelOutputError(RuntimeError):
    """模型请求未得到可解析的最终业务结果。"""


# 思考模式慢请求上限 10 分钟；实际执行仍受外层阶段预算（600s/900s）约束
_MODEL_REQUEST_TIMEOUT_SECONDS = 600


def _public_content(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, list):
        return "".join(
            item
            if isinstance(item, str)
            else str(item.get("text", ""))
            if isinstance(item, dict)
            else ""
            for item in content
        )
    return content if isinstance(content, str) else ""


def _last_ai_message(result: dict[str, Any]) -> AIMessage | None:
    for message in reversed(result.get("messages", [])):
        if isinstance(message, AIMessage):
            return message
    return None


def _finish_reason(message: AIMessage | None) -> str:
    if message is None:
        return ""
    metadata = getattr(message, "response_metadata", {})
    if not isinstance(metadata, dict):
        return ""
    reason = metadata.get("finish_reason")
    return str(reason) if reason else ""


def _raw_output_text(result: dict[str, Any]) -> str:
    """从流式结果提取最后一条非空 AIMessage 文本（受保护 helper）。

    输入:
        result: dict — _stream_agent 的返回（含 messages）

    输出:
        str — 最后一条非空 AIMessage 的文本，无则返回空串
    """
    for message in reversed(result.get("messages", [])):
        if not isinstance(message, AIMessage):
            continue
        content = message.content
        if isinstance(content, list):
            content = "\n".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict)
            )
        text = str(content).strip()
        if text:
            return text
    return ""


def _extract_structured_logged(
    result: dict[str, Any],
    schema: type[BaseModel],
    *,
    actor: str,
    stage: int,
    attempt: int,
) -> BaseModel:
    """解析结构化输出，失败时记录原始文本到日志后重抛（受保护 helper）。

    输入:
        result: dict — _stream_agent 的返回
        schema: type[BaseModel] — 目标 Pydantic 模型
        actor: str — 角色（worker/evaluator）
        stage: int — 阶段
        attempt: int — 尝试轮次

    输出:
        BaseModel — 解析结果；失败时记录日志（截断至 2000 字符）并重抛
    """
    structured_response = result.get("structured_response")
    if structured_response is not None:
        output = (
            structured_response
            if isinstance(structured_response, schema)
            else schema.model_validate(structured_response)
        )
        emit_commitment_messages(
            actor=actor,
            stage=stage,
            attempt=attempt,
            messages=[AIMessage(content=_json(output.model_dump()))],
        )
        return output
    try:
        return _extract_structured(result, schema)
    except ValueError:
        raw = _raw_output_text(result)
        logger.warning(
            "承诺层解析失败 actor=%s stage=%s attempt=%s schema=%s raw=%s",
            actor,
            stage,
            attempt,
            schema.__name__,
            raw[:2000],
        )
        raise


class ReviewedDelegator:
    def __init__(
        self,
        model: BaseChatModel,
        context7_tools: list[BaseTool],
    ) -> None:
        self._model = model
        self._context7_tools = context7_tools

    async def _stream_agent(
        self,
        agent: Any,
        messages: list[HumanMessage],
        *,
        actor: str,
        stage: int,
        stream_id: str,
        attempt: int = 1,
    ) -> dict[str, Any]:
        emit_commitment_messages(
            actor=actor,
            stage=stage,
            attempt=attempt,
            messages=messages,
        )
        last_reason = "missing_final_message"
        # DeepSeek 思考模式 + json_object 偶发"空正文 + finish_reason=stop"（vLLM #41132），
        # 传输尝试 2 次不足，取 3 次；重试不消耗语义 attempt 额度
        for transport_attempt in range(1, 4):
            result: dict[str, Any] = {}
            try:
                async with asyncio.timeout(_MODEL_REQUEST_TIMEOUT_SECONDS):
                    async for mode, chunk in agent.astream(
                        {"messages": messages},
                        stream_mode=["messages", "values"],
                    ):
                        if mode == "values":
                            result = chunk
                            continue
                        if mode != "messages":
                            continue
                        message_chunk, _metadata = chunk
                        if not _public_content(message_chunk):
                            continue
                        emit_commitment_messages(
                            actor=actor,
                            stage=stage,
                            attempt=attempt,
                            messages=[message_chunk],
                        )
            except (TransportError, TimeoutError) as exc:
                last_reason = f"{type(exc).__name__}: {exc}"
                if transport_attempt < 2:
                    emit_commitment_trace(
                        actor=actor,
                        event="model_transport_retry",
                        title=f"{actor} 模型流连接中断，正在重试",
                        status="running",
                        stage=stage,
                        detail=(
                            f"transport_attempt={transport_attempt}; "
                            f"error={last_reason}"
                        ),
                    )
                continue

            final_message = _last_ai_message(result)
            finish_reason = _finish_reason(final_message)
            if finish_reason == "length":
                raise _ModelOutputError("模型输出被截断（finish_reason=length）")
            if (
                finish_reason != "insufficient_system_resource"
                and result.get("structured_response") is not None
            ):
                return result
            if (
                finish_reason != "insufficient_system_resource"
                and final_message is not None
                and _public_content(final_message)
            ):
                return result

            last_reason = finish_reason or "missing_final_message"
            if transport_attempt < 2:
                emit_commitment_trace(
                    actor=actor,
                    event="model_transport_retry",
                    title=f"{actor} 模型正文为空，正在重试",
                    status="running",
                    stage=stage,
                    detail=(
                        f"transport_attempt={transport_attempt}; "
                        f"finish_reason={last_reason}"
                    ),
                )

        raise _ModelOutputError(
            "模型传输重试耗尽，未得到最终业务结果；"
            f"finish_reason={last_reason}"
        )

    async def _invoke_schema(
        self,
        *,
        name: str,
        system_prompt: str,
        prompt: dict[str, Any],
        schema: type[BaseModel],
        attempt: int,
    ) -> BaseModel:
        emit_commitment_trace(
            actor="worker",
            event="model_started",
            title=f"正在运行 {name}",
            status="running",
            stage=int(prompt.get("stage", 0) or 0),
            payload={"input": prompt},
        )
        agent = create_agent(
            model=self._model.bind(response_format={"type": "json_object"}),
            tools=[],
            system_prompt=system_prompt,
            name=name,
        )
        result = await self._stream_agent(
            agent,
            [HumanMessage(content=_json(prompt))],
            actor="worker",
            stage=int(prompt.get("stage", 0) or 0),
            stream_id=name,
            attempt=attempt,
        )
        output = _extract_structured_logged(
            result,
            schema,
            actor="worker",
            stage=int(prompt.get("stage", 0) or 0),
            attempt=attempt,
        )
        emit_commitment_trace(
            actor="worker",
            event="model_completed",
            title=f"{name} 已返回",
            status="completed",
            stage=int(prompt.get("stage", 0) or 0),
            payload={"output": output.model_dump()},
        )
        return output

    def _context7_tool(self, name: str) -> BaseTool:
        for tool_item in self._context7_tools:
            if tool_item.name == name:
                return tool_item
        raise RuntimeError(f"Context7 工具不可用: {name}")

    async def _stage_five_worker(
        self,
        envelope: TaskEnvelope,
        feedback: str,
        supervisor_messages: list[BaseMessage] | None = None,
        attempt: int = 1,
    ) -> WorkerOutput:
        supervisor_messages = supervisor_messages or []
        stage_three = _stage_result(supervisor_messages, 3)
        selection = await self._invoke_schema(
            name="commitment_technology_selector",
            system_prompt=(
                "从第三步已确认要求中提取需要核验版本的具体软件技术、框架、库和服务名称。"
                "不要返回SSR、SEO、错误处理等没有独立软件版本的能力或质量要求。"
                "要求未指名具体库的能力（如“完整测试”“国际化”“无障碍”）不提取为技术；"
                "即使reviewer_feedback要求覆盖未指名技术，也不得臆造技术名。"
                "只返回 JSON。"
                f"JSON Schema: {_json(TechnologySelection.model_json_schema())}"
            ),
            prompt={
                "stage": envelope.stage,
                "supervisor_messages": [
                    message.model_dump() for message in supervisor_messages
                ],
                "requirements": stage_three.get("requirements", []),
                "reviewer_feedback": feedback,
            },
            schema=TechnologySelection,
            attempt=attempt,
        )
        names = list(
            dict.fromkeys(
                name.strip()
                for name in selection.technologies
                if name.strip()
            )
        )
        resolver = self._context7_tool("resolve-library-id")

        async def resolve(name: str) -> dict[str, Any]:
            emit_commitment_trace(
                actor="tool",
                event="tool_started",
                title="调用 Context7 resolve-library-id",
                status="running",
                stage=envelope.stage,
                detail=f"正在解析 {name} 的 Context7 库标识。",
                payload={"libraryName": name},
            )
            try:
                result = await resolver.ainvoke(
                    {
                        "libraryName": name,
                        "query": (
                            f"{name} latest stable version and official documentation"
                        ),
                    }
                )
                emit_commitment_trace(
                    actor="tool",
                    event="tool_completed",
                    title="Context7 resolve-library-id 已返回",
                    status="completed",
                    stage=envelope.stage,
                    detail=f"{name} 的库标识解析完成。",
                    payload={
                        "name": name,
                        "library_id": _context7_library_id(result),
                    },
                )
                return {"name": name, "result": result}
            except Exception as exc:
                emit_commitment_trace(
                    actor="tool",
                    event="tool_failed",
                    title="Context7 resolve-library-id 调用失败",
                    status="failed",
                    stage=envelope.stage,
                    detail=f"{name}: {exc}",
                )
                return {"name": name, "error": str(exc)}

        resolution_evidence = await asyncio.gather(
            *(resolve(name) for name in names)
        )
        query_docs = self._context7_tool("query-docs")

        async def query_version(item: dict[str, Any]) -> dict[str, Any]:
            name = str(item.get("name", ""))
            library_id = _context7_library_id(item.get("result"))
            if not library_id:
                return {"name": name, "error": "library_id unresolved"}
            emit_commitment_trace(
                actor="tool",
                event="tool_started",
                title="调用 Context7 query-docs",
                status="running",
                stage=envelope.stage,
                detail=f"正在核实 {name} 的最新稳定版本。",
                payload={"libraryId": library_id},
            )
            try:
                result = await query_docs.ainvoke(
                    {
                        "libraryId": library_id,
                        "query": (
                            f"What is the exact latest stable {name} version? "
                            "Use official release or installation documentation "
                            "and exclude canary, beta, rc, and nightly versions."
                        ),
                    }
                )
                official_version = _context7_stable_version(result)
                candidate_version = _context7_candidate_version(
                    item.get("result"),
                    library_id,
                )
                version_hint = official_version or candidate_version
                evidence_source = (
                    result if official_version else item.get("result")
                )
                emit_commitment_trace(
                    actor="tool",
                    event="tool_completed",
                    title="Context7 query-docs 已返回",
                    status="completed",
                    stage=envelope.stage,
                    detail=f"{name} 的版本证据已完成处理。",
                    payload={
                        "name": name,
                        "library_id": library_id,
                        "version": version_hint or _LATEST_STABLE_VERSION,
                    },
                )
                return {
                    "name": name,
                    "library_id": library_id,
                    "version_hint": version_hint,
                    "version_basis": (
                        "official_docs_explicit"
                        if official_version
                        else (
                            "context7_version_list"
                            if candidate_version
                            else "unresolved"
                        )
                    ),
                    "version_evidence": (
                        _context7_version_evidence(
                            evidence_source,
                            version_hint,
                        )
                        if version_hint
                        else None
                    ),
                    "result": result,
                }
            except Exception as exc:
                candidate_version = _context7_candidate_version(
                    item.get("result"),
                    library_id,
                )
                emit_commitment_trace(
                    actor="tool",
                    event="tool_failed",
                    title="Context7 query-docs 调用失败",
                    status="failed",
                    stage=envelope.stage,
                    detail=f"{name}: {exc}",
                    payload={
                        "fallback_version": candidate_version
                        or _LATEST_STABLE_VERSION
                    },
                )
                return {
                    "name": name,
                    "library_id": library_id,
                    "version_hint": candidate_version,
                    "version_basis": (
                        "context7_version_list"
                        if candidate_version
                        else "unresolved"
                    ),
                    "version_evidence": (
                        _context7_version_evidence(
                            item.get("result"),
                            candidate_version,
                        )
                        if candidate_version
                        else None
                    ),
                    "error": str(exc),
                }

        version_evidence = await asyncio.gather(
            *(query_version(item) for item in resolution_evidence)
        )
        by_name = {
            str(item.get("name")): item
            for item in version_evidence
        }
        return WorkerOutput(
            result={
                "technologies": [
                    {
                        "name": name,
                        "project_version": "unresolved",
                        "version": (
                            by_name.get(name, {}).get("version_hint")
                            or _LATEST_STABLE_VERSION
                        ),
                        "library_id": by_name.get(name, {}).get("library_id"),
                        "source_url": None,
                        "version_basis": by_name.get(name, {}).get(
                            "version_basis",
                        )
                        if by_name.get(name, {}).get("version_hint")
                        else "latest_stable_policy",
                        "version_evidence": (
                            by_name.get(name, {}).get("version_evidence")
                            if by_name.get(name, {}).get("version_hint")
                            else (
                                "Context7未返回可核实的精确稳定版本；"
                                "按用户策略在执行时采用最新稳定版。"
                            )
                        ),
                    }
                    for name in names
                ]
            },
            reasoning_summary=(
                "我先从第三步已确认要求中提取需要独立版本的技术，再解析各自的 "
                "Context7 library_id，并优先采用官方文档明确版本，其次采用过滤预发布"
                "版本后的 Context7 版本列表。没有可核实精确版本的技术按已确认策略使用 "
                "latest-stable；本阶段没有数值计算。"
            ),
        )

    async def _stage_six_worker(
        self,
        envelope: TaskEnvelope,
        feedback: str,
        supervisor_messages: list[BaseMessage] | None = None,
        attempt: int = 1,
    ) -> WorkerOutput:
        supervisor_messages = supervisor_messages or []
        technologies = _stage_result(supervisor_messages, 5).get(
            "technologies",
            [],
        )
        query_docs = self._context7_tool("query-docs")

        async def query(item: dict[str, Any]) -> dict[str, Any]:
            name = str(item.get("name", ""))
            raw_library_id = item.get("library_id")
            library_id = (
                raw_library_id.strip()
                if isinstance(raw_library_id, str)
                else ""
            )
            if not library_id.startswith("/") or library_id.count("/") < 2:
                resolver = self._context7_tool("resolve-library-id")
                emit_commitment_trace(
                    actor="tool",
                    event="tool_started",
                    title="调用 Context7 resolve-library-id",
                    status="running",
                    stage=envelope.stage,
                    detail=f"正在补全 {name} 缺失的 Context7 库标识。",
                    payload={"libraryName": name},
                )
                try:
                    resolution = await resolver.ainvoke(
                        {
                            "libraryName": name,
                            "query": (
                                f"{name} official implementation documentation"
                            ),
                        }
                    )
                    library_id = _context7_library_id(resolution) or ""
                except Exception as exc:
                    emit_commitment_trace(
                        actor="tool",
                        event="tool_failed",
                        title="Context7 resolve-library-id 调用失败",
                        status="failed",
                        stage=envelope.stage,
                        detail=f"{name}: {exc}",
                    )
                    return {"name": name, "error": str(exc)}
                if not library_id:
                    return {"name": name, "error": "library_id unresolved"}
                emit_commitment_trace(
                    actor="tool",
                    event="tool_completed",
                    title="Context7 resolve-library-id 已返回",
                    status="completed",
                    stage=envelope.stage,
                    payload={"name": name, "library_id": library_id},
                )
            emit_commitment_trace(
                actor="tool",
                event="tool_started",
                title="调用 Context7 query-docs",
                status="running",
                stage=envelope.stage,
                detail=f"正在读取 {name} {item.get('version', '')} 的官方实现知识。",
                payload={"libraryId": library_id},
            )
            try:
                result = await query_docs.ainvoke(
                    {
                        "libraryId": library_id,
                        "query": (
                            f"Official implementation knowledge required for "
                            f"{name} {item.get('version', '')}"
                        ),
                    }
                )
                emit_commitment_trace(
                    actor="tool",
                    event="tool_completed",
                    title="Context7 官方知识已返回",
                    status="completed",
                    stage=envelope.stage,
                    detail=f"{name} 的官方知识已交给 Worker 组装。",
                )
                return {
                    "name": name,
                    "version": item.get("version"),
                    "library_id": library_id,
                    "result": result,
                }
            except Exception as exc:
                emit_commitment_trace(
                    actor="tool",
                    event="tool_failed",
                    title="Context7 官方知识查询失败",
                    status="failed",
                    stage=envelope.stage,
                    detail=f"{name}: {exc}",
                )
                return {"name": name, "error": str(exc)}

        evidence = await asyncio.gather(
            *(query(item) for item in technologies if isinstance(item, dict))
        )
        return await self._invoke_schema(
            name="commitment_knowledge_assembler",
            system_prompt=(
                "仅根据给定 Context7 官方文档证据组装第六步 knowledge。"
                "每项包含technology、version、source_url、content；没有官方来源或正文时"
                "不得猜测。最终返回 WorkerOutput JSON，并用reasoning_summary自然说明"
                "结论依据、关键步骤、必要计算、方案比较以及不确定点与假设。"
                f"WorkerOutput Schema: {_json(WorkerOutput.model_json_schema())}"
            ),
            prompt={
                "stage": envelope.stage,
                "supervisor_messages": [
                    message.model_dump() for message in supervisor_messages
                ],
                "confirmed_technologies": technologies,
                "context7_evidence": evidence,
                "reviewer_feedback": feedback,
            },
            schema=WorkerOutput,
            attempt=attempt,
        )

    async def _worker(
        self,
        envelope: TaskEnvelope,
        feedback: str,
        supervisor_messages: list[BaseMessage] | None = None,
        attempt: int = 1,
    ) -> WorkerOutput:
        supervisor_messages = supervisor_messages or []
        if envelope.stage == 5:
            return await self._stage_five_worker(
                envelope,
                feedback,
                supervisor_messages,
                attempt,
            )
        if envelope.stage == 6:
            return await self._stage_six_worker(
                envelope,
                feedback,
                supervisor_messages,
                attempt,
            )
        async def traced_find_reference_urls(query: str) -> list[dict[str, str]]:
            emit_commitment_trace(
                actor="tool",
                event="tool_started",
                title="搜索用户提到的网址",
                status="running",
                stage=envelope.stage,
                detail=f"正在搜索：{query}",
            )
            result = await find_reference_urls.ainvoke({"query": query})
            emit_commitment_trace(
                actor="tool",
                event="tool_completed",
                title="网址候选搜索已返回",
                status="completed",
                stage=envelope.stage,
                detail=f"找到 {len(result)} 个候选结果。",
                payload={"query": query, "results": result},
            )
            return result

        tools = (
            [
                StructuredTool.from_function(
                    coroutine=traced_find_reference_urls,
                    name="find_reference_urls",
                    description="Search for candidate URLs explicitly mentioned by the user.",
                )
            ]
            if envelope.stage == 4
            else []
        )
        if envelope.stage == 2:
            stage_guidance = (
                "第二步必须严格按以下 JSON Schema 生成 result："
                f"{_json(StageTwoResult.model_json_schema())}。"
                "requirements只包含冲突解决后仍需完成的要求；用户明确放弃的要求必须从"
                "requirements移入discarded_requirements，二者不得重叠。"
                "human_feedback中的放弃类指令（放弃/移除/去掉/不要/不需要/取消/允许 X）"
                "必须严格执行：把 X 对应的原文要求逐字移入discarded_requirements；"
                "X 是简称或关键词（如\"放弃离线\"对应\"无需网络连接的完全离线运行\"）时"
                "必须按语义对应到正确要求并移出，不得因找不到逐字文本而忽略，"
                "也不得把放弃指令只写进conflicts的explanation而保留要求。"
                "每个冲突必须是对象；未由人解决时status=open，不能省略或写成字符串。"
                "requirements与discarded_requirements每项必须逐字来自用户输入原文，"
                "不得添加任何括注、解释、冲突解决说明或括号注释；解决结论只写入conflicts的"
                "explanation字段，不得夹进要求文本。"
                "如果提供了决策等级表（decision_table），必须将本轮全部新要求与表内条目"
                "全量对照：语义冲突（包括措辞改写、技术替换等字面无关的冲突）必须写入"
                "table_conflicts，每项包含requirement（新要求）、table_requirement（表内条目）、"
                "table_priority（表内等级）和explanation（冲突说明）；没有冲突时table_conflicts"
                "返回空列表，不得遗漏。"
            )
        elif envelope.stage == 3:
            stage_guidance = (
                "第三步不得重新生成、改写或省略requirement正文。"
                "只读取输入priority_requirements中的稳定requirement_id与对应原文，"
                "必须排除discarded_requirements，已放弃的要求不得分配priority。"
                "只在本步骤首次分配priority；第二步不包含优先级，"
                "不得声称或推断第二步已为任何要求分配等级。"
                "result只能包含priority_assignments列表；每项只能包含requirement_id和"
                "整数priority 1、2或3，每个已知ID恰好出现一次。"
                "受控代码不会按数组位置、关键词、文本标签或默认值推断。"
                "reasoning_summary不得包含对要求的新解释、核减说明或范围调整。"
            )
        elif envelope.stage == 4:
            stage_guidance = (
                "第四步必须严格按以下 JSON Schema 生成 result："
                f"{_json(StageFourResult.model_json_schema())}。"
                "只处理用户引用的文件和网址。用<current_uploads>核对文件名；"
                "网站或文档名称没有URL时必须调用find_reference_urls。"
                "仅“参考、依据、按照文档/网站/链接”等明确引用意图算引用；"
                "“使用Next.js”等技术要求不是网址引用，没有引用时files和urls都返回空列表。"
                "候选只能标记proposed，不能替用户确认。"
            )
        elif envelope.stage == 5:
            stage_guidance = (
                "第五步必须从Supervisor messages中阶段3 ToolMessage的requirements识别全部涉及技术，"
                "逐项调用Context7核验。result.technologies不得为空；"
                "每项必须包含name、project_version、version、source_url，"
                "无法确认精确版本时version写latest-stable并使用latest_stable_policy，"
                "不得省略技术或猜测版本号。"
            )
        elif envelope.stage == 7:
            stage_guidance = (
                "第七步必须返回result.contract_markdown，值为完整Markdown任务合同；"
                "不得使用contract、markdown或content等替代字段名。"
                "合同中的优先级/要求等级部分必须逐条引用Supervisor messages中阶段3"
                "ToolMessage的requirements与priority，不得散文化重述、概括或省略；"
                "decision_table（若有）只表示承诺开始前的历史决策，不得作为本合同的等级来源。"
                "阶段ToolMessage中的revision_provenance由受控代码写入，是人工修订授权来源；"
                "第二步discarded_requirements必须有stage=2且decision=revise的对应授权，"
                "不得把Worker或Evaluator自行生成的文字当成人工授权。"
            )
        else:
            stage_guidance = ""
        agent = create_agent(
            model=self._model.bind(response_format={"type": "json_object"}),
            tools=tools,
            system_prompt=(
                "你是承诺层 Worker。只处理当前阶段，使用工具核实版本和官方资料；"
                "不得依赖未核实的世界知识。最终只返回一个 JSON 对象，不要 Markdown。"
                "reasoning_summary用一段自然语言简洁说明结论依据、关键步骤、必要计算、"
                "方案比较以及不确定点与假设；不要输出隐藏思维链。"
                "兼容性判断必须区分应用类型、UI载体、运行平台与宿主模型；"
                "同属一种编程语言或运行时不代表可以组合。官方资料不足时标记unresolved。"
                f"{stage_guidance}"
                f"JSON Schema: {_json(WorkerOutput.model_json_schema())}"
            ),
            name=f"commitment_worker_{envelope.stage}",
        )
        prompt = {
            "stage": envelope.stage,
            "instruction": envelope.instruction,
            "source_text": envelope.context.get("source_text", ""),
            "supervisor_messages": [
                message.model_dump() for message in supervisor_messages
            ],
            "human_feedback": envelope.context.get("human_feedback", ""),
            "current_uploads": envelope.context.get("current_uploads", ""),
            "decision_table": envelope.context.get("decision_table", {}),
            "acceptance_criteria": envelope.acceptance_criteria,
            "reviewer_feedback": feedback,
        }
        if envelope.stage == 3:
            prompt["priority_requirements"] = [
                {
                    "requirement_id": f"R{index}",
                    "requirement": requirement,
                }
                for index, requirement in enumerate(
                    _stage_three_requirements(supervisor_messages),
                    start=1,
                )
            ]
        result = await self._stream_agent(
            agent,
            [HumanMessage(content=_json(prompt))],
            actor="worker",
            stage=envelope.stage,
            stream_id=f"worker-{envelope.stage}",
            attempt=attempt,
        )
        output = _extract_structured_logged(
            result,
            WorkerOutput,
            actor="worker",
            stage=envelope.stage,
            attempt=attempt,
        )
        if envelope.stage == 4:
            return _filter_stage_four_result(
                output,
                envelope.context.get("source_text", "")
                or _source_text(supervisor_messages),
            )
        if envelope.stage == 3:
            output = _normalize_stage_three_result(
                output,
                _stage_three_requirements(supervisor_messages),
            )
            stage_two = _stage_result(supervisor_messages, 2)
            return output.model_copy(
                update={
                    "result": _merge_table_escalations(
                        output.result,
                        stage_two.get("table_conflicts", []),
                        envelope.context.get("decision_table_rows", []),
                        output.result.get("requirements", []),
                    )
                }
            )
        return output

    async def _evaluator(
        self,
        envelope: TaskEnvelope,
        worker_output: WorkerOutput,
        supervisor_messages: list[BaseMessage] | None = None,
        attempt: int = 1,
        structure_error: str = "",
        reviewer_feedback: str = "",
    ) -> ReviewOutput:
        supervisor_messages = supervisor_messages or []
        evaluator_input = {
            "supervisor_messages": [
                message.model_dump() for message in supervisor_messages
            ],
            "task": envelope.model_dump(),
            "source_text": envelope.context.get("source_text", ""),
            "current_uploads": envelope.context.get("current_uploads", ""),
            "reviewer_feedback": reviewer_feedback,
            "worker_output": worker_output.model_dump(),
            "deterministic_validation_error": structure_error,
        }
        if structure_error:
            review = ReviewOutput(
                approved=False,
                feedback=structure_error,
                reasoning_summary="候选结果未通过当前阶段的确定性结构校验，需要重试。",
            )
            emit_commitment_messages(
                actor="evaluator",
                stage=envelope.stage,
                attempt=attempt,
                messages=[
                    HumanMessage(content=_json(evaluator_input)),
                    AIMessage(content=_json(review.model_dump())),
                ],
            )
            return review
        system_prompt = (
                "你是独立 Evaluator。严格按验收条件判断 Worker 结果。"
                "reasoning_summary用一段自然语言说明审核依据、关键检查、必要计算、"
                "替代判断以及不确定点与假设；不要输出隐藏思维链。"
                "存在遗漏、臆测版本、非官方来源或结构错误时必须拒绝并给出可执行反馈。"
                "特别检查框架与组件的应用类型、UI载体、运行平台和宿主模型；"
                "Web、桌面、移动、CMS模块与独立应用不可仅因同属一种语言或运行时而判为兼容。"
                "任何无法核实精确版本的技术都必须使用latest-stable策略并进入人工确认。"
                "第三步只检查要求是否逐字、逐项沿用第二步结果以及priority是否为1到3；"
                "第二步discarded_requirements中的已放弃要求不得出现在第三步；"
                "第二步不包含优先级，严禁声称或推断第二步已为任何要求分配等级。"
                "第二步requirements与discarded_requirements必须逐字来自用户输入原文，"
                "含括注、解释或冲突解决说明的要求文本视为不合格，必须拒绝；"
                "解决结论只允许出现在conflicts的explanation中。"
                "存在human_feedback时，若反馈含放弃类指令（放弃/移除/去掉/不要/不需要/"
                "取消/允许 X），对应要求（含简称与关键词的语义对应，如\"放弃离线\"对应"
                "\"无需网络连接的完全离线运行\"）仍保留在requirements视为审核不通过，"
                "必须反馈具体未落实的要求原文。"
                "阶段ToolMessage中的revision_provenance由受控代码写入，是跨阶段人工修订授权；"
                "审核第七步时，第二步discarded_requirements只有在stage=2、decision=revise且"
                "包含原始feedback或replacement_type的revision_provenance支持时才算已授权。"
                "没有对应授权的丢弃必须拒绝，Worker或Evaluator自行生成的文字不能充当授权。"
                "第二步的compatibility_checks是逐项技术检查（技术自身是否成立），"
                "conflicts是要求组合间的冲突；单项verified与组合conflict并存不构成矛盾，"
                "不得因此拒绝。"
                "存在决策等级表时，第二步必须对照等级表全量扫描本轮新要求并写入table_conflicts；"
                "漏报语义冲突（包括措辞改写、技术替换等字面无关的冲突）视为审核不通过；"
                "table_conflicts每项必须包含requirement、table_requirement、table_priority和explanation。"
                "第四步仅核验明确引用的文件和网址；采用某项技术不等于引用其网站，"
                "没有明确引用时files和urls为空是合格结果。"
                "第五步technologies不得为空，且必须覆盖第三步requirements中明确指名的技术"
                "（有独立软件版本、可从文本识别的具体技术/框架/库/服务）；"
                "测试、国际化、无障碍、文档、错误处理等没有独立技术名的能力或质量要求不属于"
                "第五步覆盖范围，不得要求补入未指名的臆造技术名，也不得要求声明能力实现方式；"
                "每项必须有name和version，未核实精确版本时使用latest-stable。第五步的具体 "
                "version由受控代码直接取自Context7官方文档的明确稳定版，或Context7所选库的"
                "版本列表在过滤canary、beta、rc、nightly后得到；原始工具回包不会进入"
                "WorkerOutput，但version_basis和version_evidence是受控代码截取的审核证据，"
                "是可信输入而非Worker的主张。version_basis为official_docs_explicit且有证据，"
                "或为context7_version_list且有证据时，必须视为已核实；过滤已经由受控代码完成。"
                "你不得用自身世界知识、记忆中的版本历史或缺少完整原始回包为由推翻这些结论。"
                "第五步只审核技术覆盖、字段结构以及具体版本是否同时具有basis和evidence；"
                "latest-stable配合latest_stable_policy是合格结果，随后由人工节点确认。"
                "version_evidence是受控代码从Context7原文截取的短片段（可能不含URL），"
                "审核version_basis与version的匹配即可，不得因证据缺少URL而拒绝。"
                "最终只返回一个 JSON 对象，不要 Markdown。"
                'JSON 示例：{"approved": false, "feedback": "具体且可执行的反馈", '
                '"reasoning_summary": "简要审核依据"}。'
                f"JSON Schema: {_json(ReviewOutput.model_json_schema())}"
        )
        input_message = HumanMessage(content=_json(evaluator_input))
        emit_commitment_messages(
            actor="evaluator",
            stage=envelope.stage,
            attempt=attempt,
            messages=[input_message],
        )
        bound_model = self._model.bind(
            max_tokens=8192,
            reasoning_effort="low",
        )
        structured_model = bound_model.with_structured_output(
            ReviewOutput,
            method="json_mode",
            include_raw=True,
        )
        last_reason = "missing_json_result"
        use_plain_recovery = False
        for transport_attempt in range(1, 3):
            try:
                async with asyncio.timeout(_MODEL_REQUEST_TIMEOUT_SECONDS):
                    if use_plain_recovery:
                        raw_message = await bound_model.ainvoke(
                            [
                                SystemMessage(content=system_prompt),
                                input_message,
                                HumanMessage(
                                    content=(
                                        "上一次 JSON mode 返回空正文。保持上述审核任务和"
                                        "thinking，立即只返回最终 JSON；不要解释、不要 Markdown。"
                                    )
                                ),
                            ]
                        )
                        result = {"raw": raw_message, "parsed": None}
                    else:
                        result = await structured_model.ainvoke(
                            [SystemMessage(content=system_prompt), input_message]
                        )
            except (TransportError, TimeoutError) as exc:
                last_reason = f"{type(exc).__name__}: {exc}"
                if transport_attempt < 2:
                    emit_commitment_trace(
                        actor="evaluator",
                        event="model_transport_retry",
                        title="Evaluator JSON 请求失败，正在重试",
                        status="running",
                        stage=envelope.stage,
                        detail=(
                            f"transport_attempt={transport_attempt}; "
                            f"error={last_reason}"
                        ),
                    )
                continue

            raw_message = result.get("raw") if isinstance(result, dict) else None
            finish_reason = _finish_reason(
                raw_message if isinstance(raw_message, AIMessage) else None
            )
            if finish_reason == "length":
                raise _ModelOutputError("模型输出被截断（finish_reason=length）")
            parsed = result.get("parsed") if isinstance(result, dict) else None
            if parsed is not None:
                review = (
                    parsed
                    if isinstance(parsed, ReviewOutput)
                    else ReviewOutput.model_validate(parsed)
                )
                emit_commitment_messages(
                    actor="evaluator",
                    stage=envelope.stage,
                    attempt=attempt,
                    messages=[AIMessage(content=_json(review.model_dump()))],
                )
                return review

            raw = _public_content(raw_message)
            if raw and use_plain_recovery:
                try:
                    review = _extract_structured(
                        {"messages": [raw_message]},
                        ReviewOutput,
                    )
                except ValueError:
                    pass
                else:
                    emit_commitment_messages(
                        actor="evaluator",
                        stage=envelope.stage,
                        attempt=attempt,
                        messages=[AIMessage(content=_json(review.model_dump()))],
                    )
                    return review
            if raw:
                feedback = (
                    "Evaluator 输出不是有效的 JSON 对象（可能被截断或包含尾随内容），"
                    "无法解析为审核结论；请重新生成并只输出 JSON。"
                )
                review = ReviewOutput(
                    approved=False,
                    feedback=feedback,
                    reasoning_summary="Evaluator输出结构无效，需要重新执行Worker-Evaluator审核。",
                )
                emit_commitment_messages(
                    actor="evaluator",
                    stage=envelope.stage,
                    attempt=attempt,
                    messages=[AIMessage(content=_json(review.model_dump()))],
                )
                return review

            last_reason = finish_reason or "missing_json_result"
            if transport_attempt < 2:
                use_plain_recovery = finish_reason == "stop"
                emit_commitment_trace(
                    actor="evaluator",
                    event="model_transport_retry",
                    title=(
                        "Evaluator JSON 结果为空，切换普通文本恢复"
                        if use_plain_recovery
                        else "Evaluator JSON 结果为空，正在重试"
                    ),
                    status="running",
                    stage=envelope.stage,
                    detail=(
                        f"transport_attempt={transport_attempt}; "
                        f"finish_reason={last_reason}"
                    ),
                )

        raise _ModelOutputError(
            "Evaluator 输出恢复耗尽，未得到最终业务结果；"
            f"finish_reason={last_reason}"
        )

    async def run(
        self,
        envelope: TaskEnvelope,
        supervisor_messages: list[BaseMessage] | None = None,
    ) -> tuple[WorkerOutput | None, str]:
        supervisor_messages = supervisor_messages or []
        feedback = ""
        for attempt in range(1, _MAX_REVIEW_ATTEMPTS + 1):
            worker_feedback = feedback
            try:
                worker_output = await self._worker(
                    envelope,
                    worker_feedback,
                    supervisor_messages,
                    attempt,
                )
            except _ModelOutputError as exc:
                feedback = f"Worker 模型输出错误：{exc}"
                emit_commitment_messages(
                    actor="evaluator",
                    stage=envelope.stage,
                    attempt=attempt,
                    messages=[
                        AIMessage(
                            content=_json(
                                ReviewOutput(
                                    approved=False,
                                    feedback=feedback,
                                    reasoning_summary="Worker模型请求未得到最终业务结果。",
                                ).model_dump()
                            )
                        )
                    ],
                )
                return None, feedback
            except ValidationError as exc:
                exc_text = str(exc)
                # ponytail: 空输出以 EOF/空输入 报错特征识别，给可读反馈；其他结构错误保留字段说明供重试
                if "EOF while parsing" in exc_text and "input_value=''" in exc_text:
                    feedback = (
                        "Worker 输出为空（模型未返回内容），请重新生成并只输出 JSON"
                    )
                else:
                    feedback = f"Worker输出不符合WorkerOutput结构：{exc_text}"
                if envelope.stage == 7:
                    feedback += "；阶段7必须返回result.contract_markdown"
                review = ReviewOutput(
                    approved=False,
                    feedback=feedback,
                    reasoning_summary="Worker输出结构无效，需要根据Schema修订后重试。",
                )
                emit_commitment_messages(
                    actor="evaluator",
                    stage=envelope.stage,
                    attempt=attempt,
                    messages=[
                        HumanMessage(
                            content=_json(
                                {
                                    "supervisor_messages": [
                                        message.model_dump()
                                        for message in supervisor_messages
                                    ],
                                    "task": envelope.model_dump(),
                                    "reviewer_feedback": worker_feedback,
                                    "worker_output": None,
                                    "deterministic_validation_error": feedback,
                                }
                            )
                        ),
                        AIMessage(content=_json(review.model_dump())),
                    ],
                )
                continue
            structure_error = _validate_stage_result(
                envelope.stage,
                worker_output.result,
                supervisor_messages,
                envelope.context.get("decision_table_rows", []),
            )
            if structure_error:
                logger.warning(
                    "承诺层校验拒绝 actor=worker stage=%s attempt=%s error=%s result=%s",
                    envelope.stage,
                    attempt,
                    structure_error,
                    _json(worker_output.result)[:2000],
                )
            try:
                review = await self._evaluator(
                    envelope,
                    worker_output,
                    supervisor_messages,
                    attempt,
                    structure_error or "",
                    worker_feedback,
                )
            except _ModelOutputError as exc:
                feedback = f"Evaluator 模型输出错误：{exc}"
                emit_commitment_messages(
                    actor="evaluator",
                    stage=envelope.stage,
                    attempt=attempt,
                    messages=[
                        AIMessage(
                            content=_json(
                                ReviewOutput(
                                    approved=False,
                                    feedback=feedback,
                                    reasoning_summary="Evaluator模型请求未得到最终业务结果。",
                                ).model_dump()
                            )
                        )
                    ],
                )
                return None, feedback
            if review.approved:
                return worker_output, ""
            feedback = review.feedback or "Evaluator 未提供具体反馈"
        return None, feedback
