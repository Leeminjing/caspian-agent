"""
本文件对外提供 ReviewedDelegator，封装隔离的 Worker-Evaluator 审核闭环。

输入:
    model — 创建 Worker 和 Evaluator 所使用的 BaseChatModel。
    context7_tools — 仅承诺层内部可见的 Context7 BaseTool 列表。
    TaskEnvelope — 当前阶段指令、上下文和验收条件。

输出:
    WorkerOutput | None — 最多三次审核后通过的阶段结果；全部失败时为 None。
    str — 未通过时最后一轮结构校验或 Evaluator 反馈。

具体工作流:
    (1) 为当前阶段创建干净的 Worker 上下文并生成候选结果。
    (2) 对特殊阶段执行确定性 Context7 查询、版本处理或结果规范化。
    (3) 使用独立 Evaluator 按 TaskEnvelope 验收条件审核结果。
    (4) 不合格时只携带反馈创建新的 Worker，最多执行三次。
    (5) 仅向调用方返回通过结果或最后反馈，不暴露中间失败过程。

示例:
    delegator = ReviewedDelegator(model, context7_tools)
    output, feedback = await delegator.run(envelope)
"""

import asyncio
from typing import Any

from langchain.agents import create_agent
from langchain.messages import HumanMessage
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from pydantic import BaseModel

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
    _normalize_stage_three_result,
    _stage_three_requirements,
    _validate_stage_result,
)

class ReviewedDelegator:
    def __init__(
        self,
        model: BaseChatModel,
        context7_tools: list[BaseTool],
    ) -> None:
        self._model = model
        self._context7_tools = context7_tools

    async def _invoke_schema(
        self,
        *,
        name: str,
        system_prompt: str,
        prompt: dict[str, Any],
        schema: type[BaseModel],
    ) -> BaseModel:
        agent = create_agent(
            model=self._model,
            tools=[],
            system_prompt=system_prompt,
            name=name,
        )
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=_json(prompt))]}
        )
        return _extract_structured(result, schema)

    def _context7_tool(self, name: str) -> BaseTool:
        for tool_item in self._context7_tools:
            if tool_item.name == name:
                return tool_item
        raise RuntimeError(f"Context7 工具不可用: {name}")

    async def _stage_five_worker(
        self,
        envelope: TaskEnvelope,
        feedback: str,
    ) -> WorkerOutput:
        selection = await self._invoke_schema(
            name="commitment_technology_selector",
            system_prompt=(
                "从第三步已确认要求中提取需要核验版本的具体软件技术、框架、库和服务名称。"
                "不要返回SSR、SEO、错误处理等没有独立软件版本的能力或质量要求。"
                "只返回 JSON。"
                f"JSON Schema: {_json(TechnologySelection.model_json_schema())}"
            ),
            prompt={
                "requirements": envelope.context.get("approved_stages", {})
                .get("3", {})
                .get("requirements", []),
                "reviewer_feedback": feedback,
            },
            schema=TechnologySelection,
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
            try:
                result = await resolver.ainvoke(
                    {
                        "libraryName": name,
                        "query": (
                            f"{name} latest stable version and official documentation"
                        ),
                    }
                )
                return {"name": name, "result": result}
            except Exception as exc:
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
            }
        )

    async def _stage_six_worker(
        self,
        envelope: TaskEnvelope,
        feedback: str,
    ) -> WorkerOutput:
        technologies = (
            envelope.context.get("approved_stages", {})
            .get("5", {})
            .get("technologies", [])
        )
        query_docs = self._context7_tool("query-docs")

        async def query(item: dict[str, Any]) -> dict[str, Any]:
            name = str(item.get("name", ""))
            library_id = str(item.get("library_id", ""))
            if not library_id or library_id == "unresolved":
                return {"name": name, "error": "library_id unresolved"}
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
                return {"name": name, "version": item.get("version"), "result": result}
            except Exception as exc:
                return {"name": name, "error": str(exc)}

        evidence = await asyncio.gather(
            *(query(item) for item in technologies if isinstance(item, dict))
        )
        return await self._invoke_schema(
            name="commitment_knowledge_assembler",
            system_prompt=(
                "仅根据给定 Context7 官方文档证据组装第六步 knowledge。"
                "每项包含technology、version、source_url、content；没有官方来源或正文时"
                "不得猜测。最终返回 WorkerOutput JSON。"
                f"WorkerOutput Schema: {_json(WorkerOutput.model_json_schema())}"
            ),
            prompt={
                "confirmed_technologies": technologies,
                "context7_evidence": evidence,
                "reviewer_feedback": feedback,
            },
            schema=WorkerOutput,
        )

    async def _worker(self, envelope: TaskEnvelope, feedback: str) -> WorkerOutput:
        if envelope.stage == 5:
            return await self._stage_five_worker(envelope, feedback)
        if envelope.stage == 6:
            return await self._stage_six_worker(envelope, feedback)
        tools = [find_reference_urls] if envelope.stage == 4 else []
        if envelope.stage == 2:
            stage_guidance = (
                "第二步必须严格按以下 JSON Schema 生成 result："
                f"{_json(StageTwoResult.model_json_schema())}。"
                "requirements只包含冲突解决后仍需完成的要求；用户明确放弃的要求必须从"
                "requirements移入discarded_requirements，二者不得重叠。"
                "每个冲突必须是对象；未由人解决时status=open，不能省略或写成字符串。"
            )
        elif envelope.stage == 3:
            stage_guidance = (
                "第三步只能逐字、逐项复制approved_stages.2中仍需完成的requirements，"
                "必须排除discarded_requirements，已放弃的要求不得分配priority。"
                "只在本步骤首次分配priority；第二步不包含优先级，"
                "不得声称或推断第二步已为任何要求分配等级。"
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
                "第五步必须从approved_stages.3.requirements识别全部涉及技术，"
                "逐项调用Context7核验。result.technologies不得为空；"
                "每项必须包含name、project_version、version、source_url，"
                "无法确认精确版本时version写latest-stable并使用latest_stable_policy，"
                "不得省略技术或猜测版本号。"
            )
        else:
            stage_guidance = ""
        agent = create_agent(
            model=self._model,
            tools=tools,
            system_prompt=(
                "你是承诺层 Worker。只处理当前阶段，使用工具核实版本和官方资料；"
                "不得依赖未核实的世界知识。最终只返回一个 JSON 对象，不要 Markdown。"
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
            "context": envelope.context,
            "acceptance_criteria": envelope.acceptance_criteria,
            "reviewer_feedback": feedback,
        }
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=_json(prompt))]}
        )
        output = _extract_structured(result, WorkerOutput)
        if envelope.stage == 4:
            return _filter_stage_four_result(
                output,
                str(envelope.context.get("source_text", "")),
            )
        if envelope.stage == 3:
            return _normalize_stage_three_result(
                output,
                _stage_three_requirements(envelope.context),
            )
        return output

    async def _evaluator(
        self,
        envelope: TaskEnvelope,
        worker_output: WorkerOutput,
    ) -> ReviewOutput:
        agent = create_agent(
            model=self._model,
            tools=[],
            system_prompt=(
                "你是独立 Evaluator。严格按验收条件判断 Worker 结果。"
                "存在遗漏、臆测版本、非官方来源或结构错误时必须拒绝并给出可执行反馈。"
                "特别检查框架与组件的应用类型、UI载体、运行平台和宿主模型；"
                "Web、桌面、移动、CMS模块与独立应用不可仅因同属一种语言或运行时而判为兼容。"
                "任何无法核实精确版本的技术都必须使用latest-stable策略并进入人工确认。"
                "第三步只检查要求是否逐字、逐项沿用第二步结果以及priority是否为1到3；"
                "第二步discarded_requirements中的已放弃要求不得出现在第三步；"
                "第二步不包含优先级，严禁声称或推断第二步已为任何要求分配等级。"
                "第四步仅核验明确引用的文件和网址；采用某项技术不等于引用其网站，"
                "没有明确引用时files和urls为空是合格结果。"
                "第五步technologies不得为空，且必须覆盖第三步要求涉及的全部技术；"
                "每项必须有name和version，未核实精确版本时使用latest-stable。第五步的具体 "
                "version由受控代码直接取自Context7官方文档的明确稳定版，或Context7所选库的"
                "版本列表在过滤canary、beta、rc、nightly后得到；原始工具回包不会进入"
                "WorkerOutput，但version_basis和version_evidence是受控代码截取的审核证据，"
                "是可信输入而非Worker的主张。version_basis为official_docs_explicit且有证据，"
                "或为context7_version_list且有证据时，必须视为已核实；过滤已经由受控代码完成。"
                "你不得用自身世界知识、记忆中的版本历史或缺少完整原始回包为由推翻这些结论。"
                "第五步只审核技术覆盖、字段结构以及具体版本是否同时具有basis和evidence；"
                "latest-stable配合latest_stable_policy是合格结果，随后由人工节点确认。"
                "最终只返回一个 JSON 对象，不要 Markdown。"
                f"JSON Schema: {_json(ReviewOutput.model_json_schema())}"
            ),
            name=f"commitment_evaluator_{envelope.stage}",
        )
        result = await agent.ainvoke(
            {
                "messages": [
                    HumanMessage(
                        content=_json(
                            {
                                "task": envelope.model_dump(),
                                "worker_output": worker_output.model_dump(),
                            }
                        )
                    )
                ]
            }
        )
        return _extract_structured(result, ReviewOutput)

    async def run(self, envelope: TaskEnvelope) -> tuple[WorkerOutput | None, str]:
        feedback = ""
        for _ in range(_MAX_REVIEW_ATTEMPTS):
            worker_output = await self._worker(envelope, feedback)
            structure_error = _validate_stage_result(
                envelope.stage, worker_output.result, envelope.context
            )
            if structure_error:
                feedback = structure_error
                continue
            review = await self._evaluator(envelope, worker_output)
            if review.approved:
                return worker_output, ""
            feedback = review.feedback or "Evaluator 未提供具体反馈"
        return None, feedback
