"""
本文件对外提供 CommitmentMiddleware 旧导入路径的兼容重导出。

输入:
    仍从 caspian.agents.middlewares.commitment_middleware 导入承诺层对象的调用方。

输出:
    与 caspian.agents.commitment 相同的公开对象，不创建第二份实现。

具体工作流:
    (1) 从新的 caspian.agents.commitment 模块导入稳定导出集合。
    (2) 保持历史导入语句可用。
    (3) 所有运行逻辑继续由新模块中的唯一实现承担。

示例:
    from caspian.agents.middlewares.commitment_middleware import CommitmentMiddleware
"""

from caspian.agents.commitment import *

# 以下为提交 3a7d1b977d02adf1e3cfeddff1a4a86827edbc32 中的完整历史实现，仅保留注释，不参与运行。
# """
# 本文件对外提供 CommitmentMiddleware、TaskEnvelope 和 ReviewedDelegator。
#
# 输入:
#     lead agent state、LangGraph runtime、ChatModel 与 Context7 工具
#
# 输出:
#     CommitmentMiddleware — 在 lead agent 执行前运行隔离的九阶段承诺流程
#     TaskEnvelope — Supervisor 调用 delegate_with_review 的固定输入
#     ReviewedDelegator — Worker–Evaluator 最多三次审核闭环
#
# 具体工作流:
#     (1) Supervisor 只调用 delegate_with_review，stage 从 0 递增至 9
#     (2) 语义阶段由 Worker 产出并由 Evaluator 审核
#     (3) 阶段 3、5、6 通过 interrupt 等待人工批准或修订
#     (4) 阶段 6、7 写入 knowledge 与 requirements
#     (5) 最终只向 lead state 返回 task_contract 和一条 HumanMessage
# """
#
# import asyncio
# import json
# import re
# from html.parser import HTMLParser
# from pathlib import Path
# from typing import Any, Literal
# from urllib.parse import parse_qs, urlparse
#
# import httpx
# from langchain.agents import create_agent
# from langchain.agents.middleware import AgentMiddleware
# from langchain.agents.middleware.types import AgentState
# from langchain.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
# from langchain.tools import ToolRuntime, tool
# from langchain_core.language_models import BaseChatModel
# from langchain_core.tools import BaseTool
# from langgraph.errors import GraphInterrupt
# from langgraph.graph import END, START, StateGraph
# from langgraph.graph.message import REMOVE_ALL_MESSAGES
# from langgraph.prebuilt import ToolNode
# from langgraph.types import Command, interrupt
# from pydantic import BaseModel, Field
# from typing_extensions import NotRequired
#
#
# _HUMAN_REVIEW_STAGES = frozenset({3, 5, 6})
# _SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
# _MAX_REVIEW_ATTEMPTS = 3
# _STAGE_TIMEOUT_SECONDS = 600
# _KNOWLEDGE_STAGE_TIMEOUT_SECONDS = 900
# _LATEST_STABLE_VERSION = "latest-stable"
# _PROJECT_ROOT = Path(__file__).resolve().parents[6]
#
#
# def _stage_timeout(stage: int) -> int:
#     return (
#         _KNOWLEDGE_STAGE_TIMEOUT_SECONDS
#         if stage in {5, 6}
#         else _STAGE_TIMEOUT_SECONDS
#     )
#
# _STAGE_INSTRUCTIONS: dict[int, tuple[str, list[str]]] = {
#     1: ("明确用户的单一主目标，保留边界和预期结果。", ["目标清晰", "不引入用户未提出的目标"]),
#     2: (
#         "汇总全部要求并核验技术兼容性。逐项识别应用类型、UI载体、运行平台和宿主模型；"
#         "Web、桌面、移动、CMS模块与独立应用不得因同属一种语言或运行时而判为兼容。",
#         [
#             "要求完整",
#             "返回requirements、compatibility_checks和conflicts",
#             "每项技术有verified、conflict或unresolved状态",
#             "冲突或无法核实的组合不得写成兼容",
#         ],
#     ),
#     3: (
#         "基于第二步已经明确且无未决矛盾的要求集合，给每条要求分配1、2、3三档优先级；"
#         "3=必须，2=可协商，1=可选。",
#         [
#             "每条要求有等级且只使用1到3",
#             "不得重新解释或夹带第二步的矛盾处理",
#         ],
#     ),
#     4: (
#         "解析用户引用的文件与网址。文件必须与<current_uploads>中的精确文件名核对；"
#         "文件名不完整时列出上传文件候选并等待人工确认。用户提到网站、文档或项目但未给URL时，"
#         "必须先调用find_reference_urls查找候选URL，再等待人工确认。",
#         [
#             "返回files和urls",
#             "完整文件名标记matched，简称候选标记proposed，找不到标记unresolved",
#             "用户给出的完整URL标记provided，搜索候选标记proposed，找不到标记unresolved",
#             "不得把未确认候选写成已确认输入",
#         ],
#     ),
#     5: ("识别涉及技术，对比项目当前版本与 Context7 候选最新稳定版。", ["每项技术有精确版本或latest-stable策略", "不得猜测版本"]),
#     6: ("按已批准版本调用 Context7 获取官方技术知识。", ["每项知识含技术、版本、官方来源和正文", "不得使用非官方来源"]),
#     7: ("把已批准的阶段结果组装为完整 Markdown 任务合同。", ["合同包含九步已有结论", "合同可直接指导执行"]),
#     8: ("产出最终 task_contract。", ["内容与磁盘合同一致"]),
#     9: ("准备 lead agent 的最终合同消息。", ["只包含合同和理论基础"]),
# }
#
#
# class TaskEnvelope(BaseModel):
#     stage: int = Field(ge=1, le=9)
#     instruction: str
#     context: dict[str, Any] = Field(default_factory=dict)
#     acceptance_criteria: list[str] = Field(default_factory=list)
#
#
# class WorkerOutput(BaseModel):
#     result: dict[str, Any]
#     artifact_ref: str | None = None
#
#
# class ReviewOutput(BaseModel):
#     approved: bool
#     feedback: str = ""
#
#
# class CompatibilityCheck(BaseModel):
#     technology: str
#     application_type: str
#     ui_surface: str
#     runtime_platform: str
#     host_model: str
#     status: Literal["verified", "conflict", "unresolved"]
#
#
# class RequirementConflict(BaseModel):
#     requirements: list[str] = Field(min_length=1)
#     conflict_type: str
#     explanation: str
#     status: Literal["open", "resolved"]
#     resolution: str | None = None
#
#
# class StageTwoResult(BaseModel):
#     requirements: list[str] = Field(min_length=1)
#     discarded_requirements: list[str] = Field(default_factory=list)
#     compatibility_checks: list[CompatibilityCheck] = Field(min_length=1)
#     conflicts: list[RequirementConflict]
#
#
# class FileReference(BaseModel):
#     mention: str
#     uploaded_filename: str | None = None
#     candidates: list[str] = Field(default_factory=list)
#     status: Literal["matched", "proposed", "unresolved"]
#
#
# class UrlReference(BaseModel):
#     mention: str
#     url: str | None = None
#     candidates: list[str] = Field(default_factory=list)
#     source: Literal["user", "search", "none"]
#     status: Literal["provided", "proposed", "unresolved"]
#
#
# class StageFourResult(BaseModel):
#     files: list[FileReference]
#     urls: list[UrlReference]
#
#
# class TechnologySelection(BaseModel):
#     technologies: list[str] = Field(min_length=1)
#
#
# class TechnologyVersion(BaseModel):
#     name: str
#     project_version: str
#     version: str
#     library_id: str | None = None
#     source_url: str | None = None
#     version_basis: Literal[
#         "official_docs_explicit",
#         "context7_version_list",
#         "latest_stable_policy",
#         "unresolved",
#     ] = "unresolved"
#     version_evidence: str | None = None
#
#
# class StageFiveResult(BaseModel):
#     technologies: list[TechnologyVersion] = Field(min_length=1)
#
#
# class CommitmentState(AgentState):
#     stage: NotRequired[int]
#     awaiting_human: NotRequired[int | None]
#     artifacts: NotRequired[dict[str, Any]]
#     source_text: NotRequired[str]
#     thread_id: NotRequired[str]
#     knowledge_files: NotRequired[list[str]]
#     task_contract: NotRequired[str]
#     final_message: NotRequired[str]
#
#
# def _json(value: Any) -> str:
#     return json.dumps(value, ensure_ascii=False, default=str)
#
#
# def _safe_segment(value: str, label: str) -> str:
#     if value in {".", ".."} or not value or not _SAFE_SEGMENT.fullmatch(value):
#         raise ValueError(f"{label} 只允许字母、数字、点、下划线和连字符")
#     return value
#
#
# def _slug_segment(value: str, label: str) -> str:
#     slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
#     return _safe_segment(slug, label)
#
#
# def _stage_envelope(stage: int, state: dict[str, Any], feedback: str = "") -> TaskEnvelope:
#     instruction, criteria = _STAGE_INSTRUCTIONS[stage]
#     context = {
#         "source_text": state.get("source_text", ""),
#         "approved_stages": state.get("artifacts", {}),
#     }
#     if feedback:
#         context["human_feedback"] = feedback
#     return TaskEnvelope(
#         stage=stage,
#         instruction=instruction,
#         context=context,
#         acceptance_criteria=criteria,
#     )
#
#
# def _extract_structured(result: dict[str, Any], schema: type[BaseModel]) -> BaseModel:
#     value = result.get("structured_response")
#     if isinstance(value, schema):
#         return value
#     if value is not None:
#         return schema.model_validate(value)
#     for message in reversed(result.get("messages", [])):
#         if not isinstance(message, AIMessage):
#             continue
#         content = message.content
#         if isinstance(content, list):
#             content = "\n".join(
#                 item.get("text", "")
#                 for item in content
#                 if isinstance(item, dict)
#             )
#         text = str(content).strip()
#         fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
#         if fenced:
#             text = fenced.group(1)
#         try:
#             return schema.model_validate_json(text)
#         except ValueError:
#             if schema is not ReviewOutput:
#                 raise
#             approved = re.search(
#                 r'"approved"\s*:\s*(true|false)',
#                 text,
#                 re.IGNORECASE,
#             )
#             feedback = re.search(
#                 r'"feedback"\s*:\s*"(.*)"\s*}\s*$',
#                 text,
#                 re.DOTALL,
#             )
#             if not approved or not feedback:
#                 raise
#             return ReviewOutput(
#                 approved=approved.group(1).lower() == "true",
#                 feedback=feedback.group(1),
#             )
#     raise ValueError(f"模型未返回 {schema.__name__} JSON")
#
#
# def _context7_text(result: Any) -> str:
#     if isinstance(result, list):
#         return "\n".join(
#             str(item.get("text", ""))
#             for item in result
#             if isinstance(item, dict)
#         )
#     return str(result)
#
#
# def _context7_library_id(result: Any) -> str | None:
#     match = re.search(
#         r"Context7-compatible library ID:\s*(/\S+)",
#         _context7_text(result),
#     )
#     return match.group(1).strip() if match else None
#
#
# def _context7_stable_version(result: Any) -> str | None:
#     text = _context7_text(result)
#     patterns = (
#         r"latest stable version(?:\s+of\s+[^.\n]+)?\s+(?:is|as)\s+",
#         r"latest stable version\s*:\s*",
#         r"latest version\s*:\s*",
#         r"current latest version(?:\s+of\s+[^.\n]+)?\s+is\s+",
#     )
#     version_pattern = r"`?[vV]?(\d+\.\d+(?:\.\d+)?)`?(?![-0-9A-Za-z])"
#     for prefix in patterns:
#         match = re.search(prefix + version_pattern, text, re.IGNORECASE)
#         if match:
#             return match.group(1)
#     return None
#
#
# def _context7_candidate_version(
#     result: Any,
#     library_id: str,
# ) -> str | None:
#     text = _context7_text(result)
#     block = next(
#         (
#             item
#             for item in re.split(r"\n-+\n", text)
#             if f"Context7-compatible library ID: {library_id}" in item
#         ),
#         "",
#     )
#     versions = re.search(
#         r"^\s*-?\s*Versions:\s*(.+)$",
#         block,
#         re.MULTILINE,
#     )
#     if not versions:
#         return None
#     candidates = re.findall(
#         r"(?<![0-9A-Za-z])v?(\d+\.\d+(?:\.\d+)?)(?![-0-9A-Za-z.])",
#         versions.group(1),
#     )
#     return max(
#         candidates,
#         key=lambda value: tuple(int(part) for part in value.split(".")),
#         default=None,
#     )
#
#
# def _context7_version_evidence(
#     result: Any,
#     version: str,
# ) -> str | None:
#     for line in _context7_text(result).splitlines():
#         if version in line and (
#             "version" in line.lower()
#             or "latest stable" in line.lower()
#         ):
#             return line.strip()[:1000]
#     return None
#
#
# def _normalize_stage_three_result(
#     output: WorkerOutput,
#     requirements: list[str],
# ) -> WorkerOutput:
#     raw_items = output.result.get("requirements", [])
#     priorities = {
#         "1": 1,
#         "low": 1,
#         "optional": 1,
#         "2": 2,
#         "medium": 2,
#         "negotiable": 2,
#         "3": 3,
#         "high": 3,
#         "must": 3,
#     }
#
#     def priority_at(index: int, requirement: str) -> int:
#         raw = (
#             raw_items[index].get("priority")
#             if index < len(raw_items) and isinstance(raw_items[index], dict)
#             else None
#         )
#         if type(raw) is int and raw in {1, 2, 3}:
#             return raw
#         mapped = priorities.get(str(raw).strip().lower())
#         if mapped:
#             return mapped
#         if any(word in requirement for word in ("可选", "最好")):
#             return 1
#         if any(word in requirement for word in ("可协商", "可以", "尽量")):
#             return 2
#         return 3
#
#     return WorkerOutput(
#         result={
#             "requirements": [
#                 {
#                     "requirement": requirement,
#                     "priority": priority_at(index, requirement),
#                 }
#                 for index, requirement in enumerate(requirements)
#             ]
#         }
#     )
#
#
# def _stage_three_requirements(context: dict[str, Any] | None) -> list[str]:
#     stage_two = (
#         (context or {})
#         .get("approved_stages", {})
#         .get("2", {})
#     )
#     requirements = stage_two.get("requirements", [])
#     discarded = set(stage_two.get("discarded_requirements", []))
#     if not isinstance(requirements, list):
#         return []
#     return [
#         requirement
#         for requirement in requirements
#         if isinstance(requirement, str) and requirement not in discarded
#     ]
#
#
# def _validate_stage_result(
#     stage: int,
#     result: dict[str, Any],
#     context: dict[str, Any] | None = None,
# ) -> str | None:
#     if not result:
#         return "结果为空"
#     if stage == 2:
#         try:
#             parsed = StageTwoResult.model_validate(result)
#         except Exception as exc:
#             return f"阶段2结果不符合 StageTwoResult: {exc}"
#         if set(parsed.requirements) & set(parsed.discarded_requirements):
#             return "阶段2已放弃要求必须移出 requirements"
#         if any(
#             item.status in {"conflict", "unresolved"}
#             for item in parsed.compatibility_checks
#         ) and not parsed.conflicts:
#             return "阶段2存在 conflict 或 unresolved 时 conflicts 不得为空"
#     if stage == 4:
#         try:
#             parsed = StageFourResult.model_validate(result)
#         except Exception as exc:
#             return f"阶段4结果不符合 StageFourResult: {exc}"
#         for item in parsed.files:
#             if item.status == "matched" and not item.uploaded_filename:
#                 return "阶段4 matched 文件必须包含 uploaded_filename"
#             if item.status == "proposed" and not item.candidates:
#                 return "阶段4 proposed 文件必须包含 candidates"
#         for item in parsed.urls:
#             if item.status == "provided" and (
#                 not item.url or not item.url.startswith(("http://", "https://"))
#             ):
#                 return "阶段4 provided URL 必须包含完整 http(s) URL"
#             if item.status == "proposed" and not (
#                 (item.url and item.url.startswith(("http://", "https://")))
#                 or (
#                     item.candidates
#                     and all(
#                         url.startswith(("http://", "https://"))
#                         for url in item.candidates
#                     )
#                 )
#             ):
#                 return "阶段4 proposed URL 必须包含完整候选 URL"
#     if stage == 3:
#         requirements = result.get("requirements")
#         if not isinstance(requirements, list) or any(
#             not isinstance(item, dict)
#             or not str(item.get("requirement") or item.get("text") or "").strip()
#             or type(item.get("priority")) is not int
#             or item["priority"] not in {1, 2, 3}
#             for item in requirements
#         ):
#             return "阶段3必须返回 requirements 列表，priority 仅允许1、2、3"
#         expected = _stage_three_requirements(context)
#         actual = [
#             str(item.get("requirement") or item.get("text")).strip()
#             for item in requirements
#         ]
#         if actual != expected:
#             return "阶段3只能逐字、逐项沿用阶段2仍需完成的 requirements"
#     if stage == 5:
#         try:
#             StageFiveResult.model_validate(result)
#         except Exception as exc:
#             return f"阶段5结果不符合 StageFiveResult: {exc}"
#     if stage == 6:
#         knowledge = result.get("knowledge")
#         if not isinstance(knowledge, list) or not knowledge:
#             return "阶段6必须返回非空 knowledge 列表"
#     if stage == 7 and not isinstance(result.get("contract_markdown"), str):
#         return "阶段7必须返回 contract_markdown"
#     return None
#
#
# def _contains_unresolved_versions(result: Any) -> bool:
#     if not isinstance(result, dict):
#         return True
#     technologies = result.get("technologies")
#     if not isinstance(technologies, list) or not technologies:
#         return True
#     return any(
#         not isinstance(item, dict)
#         or not item.get("version")
#         or str(item.get("version")).lower() == "unresolved"
#         for item in technologies
#     )
#
#
# def _has_open_conflicts(result: Any) -> bool:
#     if not isinstance(result, dict):
#         return True
#     checks = result.get("compatibility_checks", [])
#     conflicts = result.get("conflicts", [])
#     return any(
#         isinstance(item, dict)
#         and item.get("status") in {"conflict", "unresolved"}
#         for item in checks
#     ) or any(
#         not isinstance(item, dict) or item.get("status") != "resolved"
#         for item in conflicts
#     )
#
#
# def _stage_four_needs_review(result: Any) -> bool:
#     if not isinstance(result, dict):
#         return True
#     return any(
#         isinstance(item, dict) and item.get("status") in {"proposed", "unresolved"}
#         for key in ("files", "urls")
#         for item in result.get(key, [])
#     )
#
#
# def _stage_four_has_unresolved(result: Any) -> bool:
#     if not isinstance(result, dict):
#         return True
#     return any(
#         not isinstance(item, dict) or item.get("status") == "unresolved"
#         for key in ("files", "urls")
#         for item in result.get(key, [])
#     )
#
#
# def _filter_stage_four_result(
#     output: WorkerOutput,
#     source_text: str,
# ) -> WorkerOutput:
#     intent = re.compile(
#         r"参考|参照|依据|文档|网址|网站|链接|官网|"
#         r"reference|refer(?:\s+to)?|according\s+to|"
#         r"docs?|documentation|website|url|link",
#         re.IGNORECASE,
#     )
#
#     def explicitly_referenced(item: Any) -> bool:
#         if not isinstance(item, dict):
#             return False
#         url = str(item.get("url") or "")
#         if url and url in source_text:
#             return True
#         mention = str(item.get("mention") or "").strip()
#         if not mention:
#             return False
#         for match in re.finditer(re.escape(mention), source_text, re.IGNORECASE):
#             nearby = source_text[
#                 max(0, match.start() - 24) : min(
#                     len(source_text),
#                     match.end() + 24,
#                 )
#             ]
#             if intent.search(nearby):
#                 return True
#         return False
#
#     result = dict(output.result)
#     result["urls"] = [
#         item
#         for item in result.get("urls", [])
#         if explicitly_referenced(item)
#     ]
#     return output.model_copy(update={"result": result})
#
#
# def _must_revise(stage: int, draft: Any) -> bool:
#     return (
#         isinstance(draft, dict) and draft.get("status") == "reviewed_failed"
#     ) or (stage == 2 and _has_open_conflicts(draft)) or (
#         stage == 4 and _stage_four_has_unresolved(draft)
#     )
#
#
# class _SearchResultParser(HTMLParser):
#     def __init__(self) -> None:
#         super().__init__()
#         self.results: list[dict[str, str]] = []
#         self._href = ""
#         self._text: list[str] = []
#
#     def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
#         values = dict(attrs)
#         if tag == "a" and "result__a" in values.get("class", ""):
#             self._href = values.get("href", "") or ""
#             self._text = []
#
#     def handle_data(self, data: str) -> None:
#         if self._href:
#             self._text.append(data)
#
#     def handle_endtag(self, tag: str) -> None:
#         if tag != "a" or not self._href:
#             return
#         query = parse_qs(urlparse(self._href).query)
#         url = query.get("uddg", [self._href])[0]
#         title = " ".join(self._text).strip()
#         if title and url.startswith(("http://", "https://")):
#             self.results.append({"title": title, "url": url})
#         self._href = ""
#         self._text = []
#
#
# @tool
# async def find_reference_urls(query: str) -> list[dict[str, str]]:
#     """Search the web for candidate URLs that the user mentioned without a URL."""
#     if not query.strip():
#         return []
#     try:
#         async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
#             response = await client.get(
#                 "https://html.duckduckgo.com/html/",
#                 params={"q": query},
#                 headers={"User-Agent": "Mozilla/5.0"},
#             )
#             response.raise_for_status()
#     except httpx.HTTPError as exc:
#         return [{"error": f"搜索失败: {exc}"}]
#     parser = _SearchResultParser()
#     parser.feed(response.text)
#     return parser.results[:5]
#
#
# class ReviewedDelegator:
#     def __init__(
#         self,
#         model: BaseChatModel,
#         context7_tools: list[BaseTool],
#     ) -> None:
#         self._model = model
#         self._context7_tools = context7_tools
#
#     async def _invoke_schema(
#         self,
#         *,
#         name: str,
#         system_prompt: str,
#         prompt: dict[str, Any],
#         schema: type[BaseModel],
#     ) -> BaseModel:
#         agent = create_agent(
#             model=self._model,
#             tools=[],
#             system_prompt=system_prompt,
#             name=name,
#         )
#         result = await agent.ainvoke(
#             {"messages": [HumanMessage(content=_json(prompt))]}
#         )
#         return _extract_structured(result, schema)
#
#     def _context7_tool(self, name: str) -> BaseTool:
#         for tool_item in self._context7_tools:
#             if tool_item.name == name:
#                 return tool_item
#         raise RuntimeError(f"Context7 工具不可用: {name}")
#
#     async def _stage_five_worker(
#         self,
#         envelope: TaskEnvelope,
#         feedback: str,
#     ) -> WorkerOutput:
#         selection = await self._invoke_schema(
#             name="commitment_technology_selector",
#             system_prompt=(
#                 "从第三步已确认要求中提取需要核验版本的具体软件技术、框架、库和服务名称。"
#                 "不要返回SSR、SEO、错误处理等没有独立软件版本的能力或质量要求。"
#                 "只返回 JSON。"
#                 f"JSON Schema: {_json(TechnologySelection.model_json_schema())}"
#             ),
#             prompt={
#                 "requirements": envelope.context.get("approved_stages", {})
#                 .get("3", {})
#                 .get("requirements", []),
#                 "reviewer_feedback": feedback,
#             },
#             schema=TechnologySelection,
#         )
#         names = list(
#             dict.fromkeys(
#                 name.strip()
#                 for name in selection.technologies
#                 if name.strip()
#             )
#         )
#         resolver = self._context7_tool("resolve-library-id")
#
#         async def resolve(name: str) -> dict[str, Any]:
#             try:
#                 result = await resolver.ainvoke(
#                     {
#                         "libraryName": name,
#                         "query": (
#                             f"{name} latest stable version and official documentation"
#                         ),
#                     }
#                 )
#                 return {"name": name, "result": result}
#             except Exception as exc:
#                 return {"name": name, "error": str(exc)}
#
#         resolution_evidence = await asyncio.gather(
#             *(resolve(name) for name in names)
#         )
#         query_docs = self._context7_tool("query-docs")
#
#         async def query_version(item: dict[str, Any]) -> dict[str, Any]:
#             name = str(item.get("name", ""))
#             library_id = _context7_library_id(item.get("result"))
#             if not library_id:
#                 return {"name": name, "error": "library_id unresolved"}
#             try:
#                 result = await query_docs.ainvoke(
#                     {
#                         "libraryId": library_id,
#                         "query": (
#                             f"What is the exact latest stable {name} version? "
#                             "Use official release or installation documentation "
#                             "and exclude canary, beta, rc, and nightly versions."
#                         ),
#                     }
#                 )
#                 official_version = _context7_stable_version(result)
#                 candidate_version = _context7_candidate_version(
#                     item.get("result"),
#                     library_id,
#                 )
#                 version_hint = official_version or candidate_version
#                 evidence_source = (
#                     result if official_version else item.get("result")
#                 )
#                 return {
#                     "name": name,
#                     "library_id": library_id,
#                     "version_hint": version_hint,
#                     "version_basis": (
#                         "official_docs_explicit"
#                         if official_version
#                         else (
#                             "context7_version_list"
#                             if candidate_version
#                             else "unresolved"
#                         )
#                     ),
#                     "version_evidence": (
#                         _context7_version_evidence(
#                             evidence_source,
#                             version_hint,
#                         )
#                         if version_hint
#                         else None
#                     ),
#                     "result": result,
#                 }
#             except Exception as exc:
#                 candidate_version = _context7_candidate_version(
#                     item.get("result"),
#                     library_id,
#                 )
#                 return {
#                     "name": name,
#                     "library_id": library_id,
#                     "version_hint": candidate_version,
#                     "version_basis": (
#                         "context7_version_list"
#                         if candidate_version
#                         else "unresolved"
#                     ),
#                     "version_evidence": (
#                         _context7_version_evidence(
#                             item.get("result"),
#                             candidate_version,
#                         )
#                         if candidate_version
#                         else None
#                     ),
#                     "error": str(exc),
#                 }
#
#         version_evidence = await asyncio.gather(
#             *(query_version(item) for item in resolution_evidence)
#         )
#         by_name = {
#             str(item.get("name")): item
#             for item in version_evidence
#         }
#         return WorkerOutput(
#             result={
#                 "technologies": [
#                     {
#                         "name": name,
#                         "project_version": "unresolved",
#                         "version": (
#                             by_name.get(name, {}).get("version_hint")
#                             or _LATEST_STABLE_VERSION
#                         ),
#                         "library_id": by_name.get(name, {}).get("library_id"),
#                         "source_url": None,
#                         "version_basis": by_name.get(name, {}).get(
#                             "version_basis",
#                         )
#                         if by_name.get(name, {}).get("version_hint")
#                         else "latest_stable_policy",
#                         "version_evidence": (
#                             by_name.get(name, {}).get("version_evidence")
#                             if by_name.get(name, {}).get("version_hint")
#                             else (
#                                 "Context7未返回可核实的精确稳定版本；"
#                                 "按用户策略在执行时采用最新稳定版。"
#                             )
#                         ),
#                     }
#                     for name in names
#                 ]
#             }
#         )
#
#     async def _stage_six_worker(
#         self,
#         envelope: TaskEnvelope,
#         feedback: str,
#     ) -> WorkerOutput:
#         technologies = (
#             envelope.context.get("approved_stages", {})
#             .get("5", {})
#             .get("technologies", [])
#         )
#         query_docs = self._context7_tool("query-docs")
#
#         async def query(item: dict[str, Any]) -> dict[str, Any]:
#             name = str(item.get("name", ""))
#             library_id = str(item.get("library_id", ""))
#             if not library_id or library_id == "unresolved":
#                 return {"name": name, "error": "library_id unresolved"}
#             try:
#                 result = await query_docs.ainvoke(
#                     {
#                         "libraryId": library_id,
#                         "query": (
#                             f"Official implementation knowledge required for "
#                             f"{name} {item.get('version', '')}"
#                         ),
#                     }
#                 )
#                 return {"name": name, "version": item.get("version"), "result": result}
#             except Exception as exc:
#                 return {"name": name, "error": str(exc)}
#
#         evidence = await asyncio.gather(
#             *(query(item) for item in technologies if isinstance(item, dict))
#         )
#         return await self._invoke_schema(
#             name="commitment_knowledge_assembler",
#             system_prompt=(
#                 "仅根据给定 Context7 官方文档证据组装第六步 knowledge。"
#                 "每项包含technology、version、source_url、content；没有官方来源或正文时"
#                 "不得猜测。最终返回 WorkerOutput JSON。"
#                 f"WorkerOutput Schema: {_json(WorkerOutput.model_json_schema())}"
#             ),
#             prompt={
#                 "confirmed_technologies": technologies,
#                 "context7_evidence": evidence,
#                 "reviewer_feedback": feedback,
#             },
#             schema=WorkerOutput,
#         )
#
#     async def _worker(self, envelope: TaskEnvelope, feedback: str) -> WorkerOutput:
#         if envelope.stage == 5:
#             return await self._stage_five_worker(envelope, feedback)
#         if envelope.stage == 6:
#             return await self._stage_six_worker(envelope, feedback)
#         tools = [find_reference_urls] if envelope.stage == 4 else []
#         if envelope.stage == 2:
#             stage_guidance = (
#                 "第二步必须严格按以下 JSON Schema 生成 result："
#                 f"{_json(StageTwoResult.model_json_schema())}。"
#                 "requirements只包含冲突解决后仍需完成的要求；用户明确放弃的要求必须从"
#                 "requirements移入discarded_requirements，二者不得重叠。"
#                 "每个冲突必须是对象；未由人解决时status=open，不能省略或写成字符串。"
#             )
#         elif envelope.stage == 3:
#             stage_guidance = (
#                 "第三步只能逐字、逐项复制approved_stages.2中仍需完成的requirements，"
#                 "必须排除discarded_requirements，已放弃的要求不得分配priority。"
#                 "只在本步骤首次分配priority；第二步不包含优先级，"
#                 "不得声称或推断第二步已为任何要求分配等级。"
#             )
#         elif envelope.stage == 4:
#             stage_guidance = (
#                 "第四步必须严格按以下 JSON Schema 生成 result："
#                 f"{_json(StageFourResult.model_json_schema())}。"
#                 "只处理用户引用的文件和网址。用<current_uploads>核对文件名；"
#                 "网站或文档名称没有URL时必须调用find_reference_urls。"
#                 "仅“参考、依据、按照文档/网站/链接”等明确引用意图算引用；"
#                 "“使用Next.js”等技术要求不是网址引用，没有引用时files和urls都返回空列表。"
#                 "候选只能标记proposed，不能替用户确认。"
#             )
#         elif envelope.stage == 5:
#             stage_guidance = (
#                 "第五步必须从approved_stages.3.requirements识别全部涉及技术，"
#                 "逐项调用Context7核验。result.technologies不得为空；"
#                 "每项必须包含name、project_version、version、source_url，"
#                 "无法确认精确版本时version写latest-stable并使用latest_stable_policy，"
#                 "不得省略技术或猜测版本号。"
#             )
#         else:
#             stage_guidance = ""
#         agent = create_agent(
#             model=self._model,
#             tools=tools,
#             system_prompt=(
#                 "你是承诺层 Worker。只处理当前阶段，使用工具核实版本和官方资料；"
#                 "不得依赖未核实的世界知识。最终只返回一个 JSON 对象，不要 Markdown。"
#                 "兼容性判断必须区分应用类型、UI载体、运行平台与宿主模型；"
#                 "同属一种编程语言或运行时不代表可以组合。官方资料不足时标记unresolved。"
#                 f"{stage_guidance}"
#                 f"JSON Schema: {_json(WorkerOutput.model_json_schema())}"
#             ),
#             name=f"commitment_worker_{envelope.stage}",
#         )
#         prompt = {
#             "stage": envelope.stage,
#             "instruction": envelope.instruction,
#             "context": envelope.context,
#             "acceptance_criteria": envelope.acceptance_criteria,
#             "reviewer_feedback": feedback,
#         }
#         result = await agent.ainvoke(
#             {"messages": [HumanMessage(content=_json(prompt))]}
#         )
#         output = _extract_structured(result, WorkerOutput)
#         if envelope.stage == 4:
#             return _filter_stage_four_result(
#                 output,
#                 str(envelope.context.get("source_text", "")),
#             )
#         if envelope.stage == 3:
#             return _normalize_stage_three_result(
#                 output,
#                 _stage_three_requirements(envelope.context),
#             )
#         return output
#
#     async def _evaluator(
#         self,
#         envelope: TaskEnvelope,
#         worker_output: WorkerOutput,
#     ) -> ReviewOutput:
#         agent = create_agent(
#             model=self._model,
#             tools=[],
#             system_prompt=(
#                 "你是独立 Evaluator。严格按验收条件判断 Worker 结果。"
#                 "存在遗漏、臆测版本、非官方来源或结构错误时必须拒绝并给出可执行反馈。"
#                 "特别检查框架与组件的应用类型、UI载体、运行平台和宿主模型；"
#                 "Web、桌面、移动、CMS模块与独立应用不可仅因同属一种语言或运行时而判为兼容。"
#                 "任何无法核实精确版本的技术都必须使用latest-stable策略并进入人工确认。"
#                 "第三步只检查要求是否逐字、逐项沿用第二步结果以及priority是否为1到3；"
#                 "第二步discarded_requirements中的已放弃要求不得出现在第三步；"
#                 "第二步不包含优先级，严禁声称或推断第二步已为任何要求分配等级。"
#                 "第四步仅核验明确引用的文件和网址；采用某项技术不等于引用其网站，"
#                 "没有明确引用时files和urls为空是合格结果。"
#                 "第五步technologies不得为空，且必须覆盖第三步要求涉及的全部技术；"
#                 "每项必须有name和version，未核实精确版本时使用latest-stable。第五步的具体 "
#                 "version由受控代码直接取自Context7官方文档的明确稳定版，或Context7所选库的"
#                 "版本列表在过滤canary、beta、rc、nightly后得到；原始工具回包不会进入"
#                 "WorkerOutput，但version_basis和version_evidence是受控代码截取的审核证据，"
#                 "是可信输入而非Worker的主张。version_basis为official_docs_explicit且有证据，"
#                 "或为context7_version_list且有证据时，必须视为已核实；过滤已经由受控代码完成。"
#                 "你不得用自身世界知识、记忆中的版本历史或缺少完整原始回包为由推翻这些结论。"
#                 "第五步只审核技术覆盖、字段结构以及具体版本是否同时具有basis和evidence；"
#                 "latest-stable配合latest_stable_policy是合格结果，随后由人工节点确认。"
#                 "最终只返回一个 JSON 对象，不要 Markdown。"
#                 f"JSON Schema: {_json(ReviewOutput.model_json_schema())}"
#             ),
#             name=f"commitment_evaluator_{envelope.stage}",
#         )
#         result = await agent.ainvoke(
#             {
#                 "messages": [
#                     HumanMessage(
#                         content=_json(
#                             {
#                                 "task": envelope.model_dump(),
#                                 "worker_output": worker_output.model_dump(),
#                             }
#                         )
#                     )
#                 ]
#             }
#         )
#         return _extract_structured(result, ReviewOutput)
#
#     async def run(self, envelope: TaskEnvelope) -> tuple[WorkerOutput | None, str]:
#         feedback = ""
#         for _ in range(_MAX_REVIEW_ATTEMPTS):
#             worker_output = await self._worker(envelope, feedback)
#             structure_error = _validate_stage_result(
#                 envelope.stage, worker_output.result, envelope.context
#             )
#             if structure_error:
#                 feedback = structure_error
#                 continue
#             review = await self._evaluator(envelope, worker_output)
#             if review.approved:
#                 return worker_output, ""
#             feedback = review.feedback or "Evaluator 未提供具体反馈"
#         return None, feedback
#
#
# def _write_knowledge(result: dict[str, Any]) -> list[str]:
#     files: list[str] = []
#     for item in result.get("knowledge", []):
#         if not isinstance(item, dict):
#             raise ValueError("knowledge 项必须是对象")
#         technology = str(item.get("technology", "")).strip()
#         technology_slug = _slug_segment(technology, "technology")
#         version = _safe_segment(str(item.get("version", "")), "version")
#         source = str(item.get("source_url", "")).strip()
#         content = str(item.get("content", "")).strip()
#         if not source.startswith("http") or not content:
#             raise ValueError("knowledge 项必须包含官方 source_url 和 content")
#         relative_path = Path("knowledge") / f"{technology_slug}-{version}.md"
#         path = _PROJECT_ROOT / relative_path
#         path.parent.mkdir(parents=True, exist_ok=True)
#         path.write_text(
#             f"# {technology} {version}\n\nSource: {source}\n\n{content}\n",
#             encoding="utf-8",
#         )
#         files.append(relative_path.as_posix())
#     if not files:
#         raise ValueError("阶段6没有产生知识文件")
#     return files
#
#
# def _write_contract(thread_id: str, result: dict[str, Any]) -> tuple[str, str]:
#     safe_thread_id = _safe_segment(thread_id, "thread_id")
#     contract = str(result.get("contract_markdown", "")).strip()
#     if not contract:
#         raise ValueError("合同内容为空")
#     relative_path = Path("requirements") / safe_thread_id / "task-contract.md"
#     path = _PROJECT_ROOT / relative_path
#     path.parent.mkdir(parents=True, exist_ok=True)
#     path.write_text(contract + "\n", encoding="utf-8")
#     return contract, relative_path.as_posix()
#
#
# def _build_final_message(contract: str, knowledge_files: list[str]) -> str:
#     sections = [f"<task_contract>\n{contract}\n</task_contract>"]
#     for name in knowledge_files:
#         content = (_PROJECT_ROOT / name).read_text(encoding="utf-8")
#         sections.append(
#             f'<theoretical foundation source="{name}">\n'
#             f"{content}\n"
#             "</theoretical foundation>"
#         )
#     return "\n\n".join(sections)
#
#
# def _human_payload(stage: int, state: dict[str, Any], error: str = "") -> dict[str, Any]:
#     draft = state.get("artifacts", {}).get(str(stage))
#     decisions = (
#         ["revise"]
#         if _must_revise(stage, draft)
#         else ["approve", "revise"]
#     )
#     if not error and isinstance(draft, dict):
#         error = str(draft.get("feedback", ""))
#     revise_label = (
#         "重试或修订"
#         if isinstance(draft, dict) and draft.get("status") == "reviewed_failed"
#         else "解决矛盾"
#         if stage == 2 and decisions == ["revise"]
#         else "补充引用"
#         if stage == 4 and decisions == ["revise"]
#         else "提出修订"
#     )
#     return {
#         "type": "commitment_review",
#         "stage": stage,
#         "draft": draft,
#         "allowed_decisions": decisions,
#         "revise_label": revise_label,
#         "error": error,
#     }
#
#
# async def _review_human_revision(
#     response: dict[str, Any],
#     stage: int,
#     state: dict[str, Any],
#     delegator: ReviewedDelegator,
# ) -> tuple[Any | None, str]:
#     replacement = response.get("replacement")
#     feedback = str(response.get("feedback", "")).strip()
#     if replacement is not None:
#         if not isinstance(replacement, dict):
#             return None, "replacement 必须是对象"
#         error = _validate_stage_result(
#             stage,
#             replacement,
#             _stage_envelope(stage, state, feedback).context,
#         )
#         if error:
#             return None, error
#         try:
#             review = await asyncio.wait_for(
#                 delegator._evaluator(
#                     _stage_envelope(stage, state, feedback),
#                     WorkerOutput(result=replacement),
#                 ),
#                 timeout=_stage_timeout(stage),
#             )
#         except TimeoutError:
#             return None, f"阶段{stage} replacement 审核超时，请重试"
#         return (
#             (replacement, "")
#             if review.approved
#             else (None, review.feedback or "Evaluator 拒绝 replacement")
#         )
#     if feedback:
#         try:
#             output, error = await asyncio.wait_for(
#                 delegator.run(_stage_envelope(stage, state, feedback)),
#                 timeout=_stage_timeout(stage),
#             )
#         except TimeoutError:
#             return None, f"阶段{stage}执行超时，请重试或提交 replacement"
#         return (output.result, "") if output else (None, error)
#     return None, "revise 必须提供 replacement 或 feedback"
#
#
# def build_delegate_with_review_tool(delegator: ReviewedDelegator) -> BaseTool:
#     @tool(
#         "delegate_with_review",
#         args_schema=TaskEnvelope,
#         description="执行承诺层当前阶段，经 Worker 和 Evaluator 审核后推进状态。",
#     )
#     async def delegate_with_review(
#         stage: int,
#         instruction: str,
#         context: dict[str, Any],
#         acceptance_criteria: list[str],
#         runtime: ToolRuntime,
#     ) -> Command:
#         state = dict(runtime.state)
#         artifacts = dict(state.get("artifacts", {}))
#         awaiting = state.get("awaiting_human")
#
#         if awaiting:
#             error = ""
#             while True:
#                 response = interrupt(_human_payload(awaiting, state, error))
#                 if not isinstance(response, dict):
#                     error = "resume 必须是对象"
#                     continue
#                 decision = response.get("decision")
#                 if decision == "approve":
#                     if _must_revise(awaiting, artifacts.get(str(awaiting))):
#                         error = f"第{awaiting}步仍需修订，不能直接批准"
#                         continue
#                     if awaiting == 5 and _contains_unresolved_versions(
#                         artifacts.get("5")
#                     ):
#                         error = "技术版本仍有 unresolved，必须先 revise"
#                         continue
#                     content = _json(
#                         {
#                             "status": "approved",
#                             "stage": awaiting,
#                             "result": artifacts.get(str(awaiting)),
#                         }
#                     )
#                     return Command(
#                         update={
#                             "awaiting_human": None,
#                             "messages": [
#                                 ToolMessage(
#                                     content=content,
#                                     tool_call_id=runtime.tool_call_id,
#                                 )
#                             ],
#                         }
#                     )
#                 if decision == "revise":
#                     revised, error = await _review_human_revision(
#                         response, awaiting, state, delegator
#                     )
#                     if revised is None:
#                         continue
#                     artifacts[str(awaiting)] = revised
#                     knowledge_files = state.get("knowledge_files", [])
#                     if awaiting == 6:
#                         knowledge_files = _write_knowledge(revised)
#                     return Command(
#                         update={
#                             "artifacts": artifacts,
#                             "knowledge_files": knowledge_files,
#                             "messages": [
#                                 ToolMessage(
#                                     content=_json(
#                                         {
#                                             "status": "revised",
#                                             "stage": awaiting,
#                                             "result": revised,
#                                             "next_action": "call_again_for_approval",
#                                         }
#                                     ),
#                                     tool_call_id=runtime.tool_call_id,
#                                 )
#                             ],
#                         }
#                     )
#                 error = "decision 只允许 approve 或 revise"
#
#         expected = int(state.get("stage", 0)) + 1
#         if stage != expected:
#             return Command(
#                 update={
#                     "messages": [
#                         ToolMessage(
#                             content=_json(
#                                 {
#                                     "status": "invalid_stage",
#                                     "expected": expected,
#                                     "received": stage,
#                                 }
#                             ),
#                             tool_call_id=runtime.tool_call_id,
#                         )
#                     ]
#                 }
#             )
#
#         envelope = TaskEnvelope(
#             stage=stage,
#             instruction=instruction,
#             context={
#                 **context,
#                 "source_text": state.get("source_text", ""),
#                 "approved_stages": artifacts,
#             },
#             acceptance_criteria=acceptance_criteria,
#         )
#
#         artifact_ref = None
#         if stage <= 7:
#             try:
#                 output, error = await asyncio.wait_for(
#                     delegator.run(envelope),
#                     timeout=_stage_timeout(stage),
#                 )
#             except TimeoutError:
#                 output = None
#                 error = f"阶段{stage}执行超时，请重试或提交 replacement"
#             if output is None:
#                 result = {
#                     "status": "reviewed_failed",
#                     "feedback": error,
#                 }
#                 artifacts[str(stage)] = result
#                 return Command(
#                     update={
#                         "artifacts": artifacts,
#                         "awaiting_human": stage,
#                         "messages": [
#                             ToolMessage(
#                                 content=_json(
#                                     {
#                                         "status": "needs_human",
#                                         "stage": stage,
#                                         "feedback": error,
#                                     }
#                                 ),
#                                 tool_call_id=runtime.tool_call_id,
#                             )
#                         ]
#                     }
#                 )
#             result = output.result
#             artifact_ref = output.artifact_ref
#         elif stage == 8:
#             result = {"task_contract": state.get("task_contract", "")}
#         else:
#             result = {
#                 "final_message": _build_final_message(
#                     state.get("task_contract", ""),
#                     list(state.get("knowledge_files", [])),
#                 )
#             }
#
#         updates: dict[str, Any] = {"stage": stage}
#         artifacts[str(stage)] = result
#         updates["artifacts"] = artifacts
#
#         if stage == 6:
#             updates["knowledge_files"] = _write_knowledge(result)
#             artifact_ref = ",".join(updates["knowledge_files"])
#         elif stage == 7:
#             contract, artifact_ref = _write_contract(
#                 str(state.get("thread_id", "")), result
#             )
#             updates["task_contract"] = contract
#         elif stage == 8:
#             updates["task_contract"] = state.get("task_contract", "")
#         elif stage == 9:
#             updates["final_message"] = result["final_message"]
#
#         if (
#             stage in _HUMAN_REVIEW_STAGES
#             or (stage == 2 and _has_open_conflicts(result))
#             or (stage == 4 and _stage_four_needs_review(result))
#         ):
#             updates["awaiting_human"] = stage
#
#         updates["messages"] = [
#             ToolMessage(
#                 content=_json(
#                     {
#                         "status": "approved",
#                         "stage": stage,
#                         "result": result,
#                         "artifact_ref": artifact_ref,
#                         "next_stage": stage + 1 if stage < 9 else None,
#                     }
#                 ),
#                 tool_call_id=runtime.tool_call_id,
#             )
#         ]
#         return Command(update=updates)
#
#     return delegate_with_review
#
#
# def _prepare_supervisor_call(state: CommitmentState) -> dict[str, Any]:
#     stage = int(state.get("awaiting_human") or state.get("stage", 0) + 1)
#     envelope = _stage_envelope(stage, dict(state))
#     return {
#         "messages": [
#             AIMessage(
#                 content="",
#                 tool_calls=[
#                     {
#                         "name": "delegate_with_review",
#                         "args": envelope.model_dump(),
#                         "id": f"commitment-stage-{stage}-{len(state.get('messages', []))}",
#                         "type": "tool_call",
#                     }
#                 ],
#             )
#         ]
#     }
#
#
# def _route_supervisor(state: CommitmentState) -> str:
#     if int(state.get("stage", 0)) >= 9:
#         return END
#     messages = state.get("messages", [])
#     if messages and isinstance(messages[-1], ToolMessage):
#         try:
#             if json.loads(str(messages[-1].content)).get("status") == "reviewed_failed":
#                 return END
#         except (TypeError, ValueError):
#             pass
#     return "prepare_call"
#
#
# def _build_supervisor(delegator: ReviewedDelegator):
#     builder = StateGraph(CommitmentState)
#     builder.add_node("prepare_call", _prepare_supervisor_call)
#     builder.add_node(
#         "delegate_with_review",
#         ToolNode([build_delegate_with_review_tool(delegator)]),
#     )
#     builder.add_edge(START, "prepare_call")
#     builder.add_edge("prepare_call", "delegate_with_review")
#     builder.add_conditional_edges("delegate_with_review", _route_supervisor)
#     return builder.compile()
#
#
# class CommitmentMiddleware(AgentMiddleware):
#     state_schema = CommitmentState
#
#     def __init__(
#         self,
#         model: BaseChatModel,
#         context7_tools: list[BaseTool],
#     ) -> None:
#         super().__init__()
#         delegator = ReviewedDelegator(model, context7_tools)
#         self._supervisor = _build_supervisor(delegator)
#
#     async def _run(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
#         if state.get("task_contract"):
#             return None
#         messages = state.get("messages", [])
#         if not messages:
#             return None
#         thread_id = getattr(getattr(runtime, "execution_info", None), "thread_id", None)
#         if thread_id is None:
#             raise ValueError("CommitmentMiddleware 无法获取 thread_id")
#         source_text = "\n\n".join(str(message.content) for message in messages)
#         result = await self._supervisor.ainvoke(
#             {
#                 "messages": [
#                     HumanMessage(
#                         content="开始承诺流程。调用 delegate_with_review 执行 stage 1。"
#                     )
#                 ],
#                 "stage": 0,
#                 "awaiting_human": None,
#                 "artifacts": {},
#                 "source_text": source_text,
#                 "thread_id": str(thread_id),
#                 "knowledge_files": [],
#             }
#         )
#         if interrupts := result.get("__interrupt__"):
#             raise GraphInterrupt(interrupts)
#         if result.get("stage") != 9:
#             raise RuntimeError(
#                 f"承诺流程异常终止于 stage {result.get('stage', 0)}"
#             )
#         contract = str(result.get("task_contract", ""))
#         final_message = str(result.get("final_message", ""))
#         if not contract or not final_message:
#             raise RuntimeError("承诺流程未产出合同或最终消息")
#         return {
#             "task_contract": contract,
#             "messages": [
#                 RemoveMessage(id=REMOVE_ALL_MESSAGES),
#                 HumanMessage(content=final_message),
#             ],
#         }
#
#     def before_agent(self, state: AgentState, runtime: Any) -> dict[str, Any] | None:
#         return asyncio.run(self._run(state, runtime))
#
#     async def abefore_agent(
#         self, state: AgentState, runtime: Any
#     ) -> dict[str, Any] | None:
#         return await self._run(state, runtime)
