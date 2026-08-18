"""
本文件对外提供 make_lead_agent 异步工厂函数和 _build_middlewares 内部函数，
作为 lead_agent 装配的唯一对外入口。

对外提供:
    make_lead_agent — 装配并返回可执行的 CompiledStateGraph
    _build_middlewares — 组装 lead_agent 的中间件链（通用 + subagent 限制/账本）

输入:
    make_lead_agent:
        model_name: str | None — 目标模型名，None 时取 config.yaml 中 models[0] 作为默认
        agent_name: str | None — system prompt 中的 agent 名称，None 时使用默认值 "Caspian"
        tool_groups: list[str] | None — 需要加载的工具分组名列表，None 表示加载全部
        user_id: str | None — 用户标识，用于定位 per-user custom skills 路径，None 时跳过 custom
        selected_skills: list[str] | None — 用户显式选中的技能名列表，注入 system prompt
        subagent_enabled: bool — 是否装配 task 委托工具与 SubagentLimitMiddleware（默认 True）

输出:
    CompiledStateGraph — langchain.agents.create_agent() 产出的可执行 agent graph

具体工作流:
    (1) 调用 create_chat_model(name=model_name) 获取 BaseChatModel 实例
    (2) 加载 skills:
        (2a) 读 extensions_config.json 获取 enabled skill 名称集合
        (2b) public: SKILLS_PUBLIC_REAL_ROOT 下扫描 skills/public/ 发现 SKILL.md
        (2c) custom: SKILLS_CUSTOM_REAL_ROOT.format(user_id=user_id) 下扫描 skills/custom/（user_id 为 None 时跳过）
        (2d) 逐个 parse_skill_file() 解析 + _validate_skill_frontmatter 校验
        (2e) 过滤 enabled=true 的 Skill → 构建 SkillCatalog
        (2f) skill_names = catalog.names → 逗号分隔字符串
    (3) await get_available_tools(tool_groups=tool_groups, subagent_enabled=subagent_enabled) 汇集全局工具
        (3a) 调用 build_describe_skill_tool(catalog) 创建 skill 查询工具
        (3b) 合并: [describe_skill_tool] + global_tools
    (4) 调用 _build_middlewares() 获取中间件列表，按配置装配 CommitmentMiddleware 与 subagent 限制/账本
        (4a) 调用 build_general_middlewares() 获取通用中间件链
        (4b) subagent_enabled 时追加 SubagentLimitMiddleware 与 DelegationLedgerMiddleware
    (5) 调用 apply_prompt_template(agent_name, skill_names, container_base_path) 生成 system_prompt，
        追加委托纪律段与选中技能段
    (6) 调用 langchain.agents.create_agent(model, tools, middleware, system_prompt, state_schema=LeadAgentState)
    (7) 返回 CompiledStateGraph

示例:
    graph = await make_lead_agent()
    graph = await make_lead_agent(model_name="deepseek-v4-flash", agent_name="DeepSeek")
    graph = await make_lead_agent(tool_groups=["file:read", "bash"])
    graph = await make_lead_agent(user_id="uuid-xxx")
"""

import json
import logging
from pathlib import Path

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from caspian.agents.lead.prompt import apply_prompt_template
from caspian.agents.lead_agent_state import LeadAgentState
from caspian.agents.middlewares.builder import build_general_middlewares
from caspian.models import create_chat_model
from caspian.tools import get_available_tools

logger = logging.getLogger(__name__)


def _build_middlewares(
    app_config,
    model: BaseChatModel,
    context7_tools: list[BaseTool],
    skill_names: frozenset[str] | None = None,
    subagent_enabled: bool = True,
) -> list[AgentMiddleware]:
    """组装 lead_agent 的中间件链：通用链 + subagent 限制/账本。

    输入:
        app_config: AppConfig — 应用配置
        model: BaseChatModel — 承诺层内部依赖
        context7_tools: list[BaseTool] — 承诺层内部依赖
        skill_names: frozenset[str] | None — enabled 技能名集合
        subagent_enabled: bool — 是否装配 subagent 限制与账本中间件

    输出:
        list[AgentMiddleware] — 按顺序排列的中间件列表

    工作流:
        (1) 调用 build_general_middlewares() 获取通用中间件链
        (2) subagent_enabled 时追加 SubagentLimitMiddleware 与 DelegationLedgerMiddleware
        (3) 返回完整列表
    """
    middlewares = build_general_middlewares(
        commitment_enabled=app_config.commitment.enabled,
        model=model,
        context7_tools=context7_tools,
        skill_names=skill_names,
        context_compression=app_config.context_compression,
    )
    if subagent_enabled:
        from caspian.agents.middlewares.delegation_ledger_middleware import (
            DelegationLedgerMiddleware,
        )
        from caspian.agents.middlewares.subagent_limit_middleware import (
            SubagentLimitMiddleware,
        )

        middlewares.append(SubagentLimitMiddleware())
        middlewares.append(DelegationLedgerMiddleware())
    return middlewares


def _load_enabled_skill_names() -> frozenset[str]:
    """从 extensions_config.json 读取已启用的 skill 名称集合。

    输入: 无

    输出:
        frozenset[str] — enabled=true 的 skill 名称集合

    工作流:
        (1) 读取 extensions_config.json
        (2) 提取 skills 段
        (3) 过滤 enabled=true → 返回名称 frozenset
        (4) 文件不存在或 skills 段为空 → 返回空 frozenset
    """
    try:
        with open("extensions_config.json", "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return frozenset()

    skills_cfg = raw.get("skills")
    if not isinstance(skills_cfg, dict):
        return frozenset()

    return frozenset(
        name for name, cfg in skills_cfg.items()
        if isinstance(cfg, dict) and cfg.get("enabled") is True
    )


def _discover_and_build_catalog(
    enabled_names: frozenset[str],
    host_base_path: str | None,
    user_id: str | None = None,
) -> "SkillCatalog":
    """扫描宿主机文件系统发现 SKILL.md，解析并构建 SkillCatalog。

    输入:
        enabled_names: frozenset[str] — extensions_config.json 中 enabled=true 的 skill 名称
        host_base_path: str | None — skills 根目录的宿主机路径，None 时从 SKILLS_PUBLIC_REAL_ROOT 取默认值
        user_id: str | None — 用户标识，用于定位 per-user custom skills 路径，None 时跳过 custom

    输出:
        SkillCatalog — 包含所有已启用 skill 的不可变索引

    工作流:
        (1) public: 遍历 SKILLS_PUBLIC_REAL_ROOT 下的 SKILL.md
        (2) custom: 若 user_id 非 None，遍历 SKILLS_CUSTOM_REAL_ROOT.format(user_id=user_id) 下的 SKILL.md
        (3) 逐个 parse_skill_file() 解析
        (4) 根据 enabled_names 过滤 enabled=true
        (5) 构建 SkillCatalog 并返回
    """
    from caspian.sandbox.path_utils import SKILLS_CUSTOM_REAL_ROOT, SKILLS_PUBLIC_REAL_ROOT
    from caspian.skills.catalog import SkillCatalog
    from caspian.skills.parser import parse_skill_file
    from caspian.skills.types import SKILL_MD_FILE, SkillCategory

    base_path = Path(host_base_path) if host_base_path else Path(SKILLS_PUBLIC_REAL_ROOT)

    categories: list[tuple[Path, SkillCategory]] = [
        (base_path, SkillCategory.PUBLIC),
    ]

    if user_id is not None:
        custom_base = Path(SKILLS_CUSTOM_REAL_ROOT.format(user_id=user_id))
        categories.append((custom_base, SkillCategory.CUSTOM))

    skills: list = []
    for cat_dir, category in categories:
        if not cat_dir.is_dir():
            continue
        for skill_file in cat_dir.rglob(SKILL_MD_FILE):
            skill = parse_skill_file(
                skill_file=skill_file,
                category=category,
                relative_path=skill_file.parent.relative_to(cat_dir),
            )
            if skill is None:
                continue
            # 设置 enabled 状态
            skill.enabled = skill.name in enabled_names
            if skill.enabled:
                skills.append(skill)

    return SkillCatalog(skills)


def build_enabled_skill_catalog(user_id: str | None = None) -> "SkillCatalog":
    return _discover_and_build_catalog(
        _load_enabled_skill_names(),
        host_base_path=None,
        user_id=user_id,
    )


def skill_by_name(catalog: "SkillCatalog") -> dict[str, "Skill"]:
    return {skill.name: skill for skill in catalog.skills}


def canonicalize_selected_skills(
    selected_skills: list[str] | None,
    catalog: "SkillCatalog",
) -> tuple[list[str], list[str]]:
    available = skill_by_name(catalog)
    result: list[str] = []
    seen: set[str] = set()
    invalid: list[str] = []
    for raw in selected_skills or []:
        name = str(raw).strip()
        if name not in available:
            invalid.append(name)
            continue
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result, invalid


def _selected_skill_prompt(catalog: "SkillCatalog", selected_skills: list[str]) -> str:
    if not selected_skills:
        return ""

    available = skill_by_name(catalog)
    sections = [
        "<selected_skill_instructions>",
        "The following Skills were explicitly selected by the user for this run.",
        "They are mandatory instructions. If selected Skills conflict, later sections take precedence.",
    ]
    for name in selected_skills:
        skill = available.get(name)
        if skill is None:
            raise ValueError(f"Selected Skill is not enabled: {name}")
        content = skill.skill_file.read_text(encoding="utf-8")
        sections.extend([
            f'<skill name="{name}">',
            content,
            "</skill>",
        ])
    sections.append("</selected_skill_instructions>")
    return "\n\n".join(sections)


async def make_lead_agent(
    model_name: str | None = None,
    agent_name: str | None = None,
    tool_groups: list[str] | None = None,
    user_id: str | None = None,
    selected_skills: list[str] | None = None,
    subagent_enabled: bool = True,
) -> CompiledStateGraph:
    # (1) 创建模型
    model = create_chat_model(name=model_name)

    # (2) 加载 skills
    from caspian.config import get_app_config

    app_config = get_app_config("config.yaml")
    # container_base_path 用于 system prompt（沙箱内的 skill 路径），host 路径用 SKILLS_PUBLIC_REAL_ROOT
    container_base_path = app_config.skills.container_path if app_config.skills else None

    catalog = build_enabled_skill_catalog(user_id=user_id)
    skill_names = ", ".join(sorted(catalog.names))

    # (3) 汇集工具（subagent_enabled=False 时不加载 task 委托工具）
    tools = await get_available_tools(
        tool_groups=tool_groups,
        subagent_enabled=subagent_enabled,
    )

    from caspian.tools.builtins.describe_skill_tool import build_describe_skill_tool

    describe_skill_tool = build_describe_skill_tool(catalog)
    tools = [describe_skill_tool] + tools

    # (4) system prompt（委托纪律段 + 选中技能段）
    system_prompt = apply_prompt_template(
        agent_name=agent_name,
        skill_names=skill_names,
        container_base_path=container_base_path,
    )
    if subagent_enabled:
        from caspian.agents.lead.prompt import build_subagent_section

        subagent_section = build_subagent_section(app_config=app_config)
        if subagent_section:
            system_prompt = f"{system_prompt}\n\n{subagent_section}"
    selected_prompt = _selected_skill_prompt(catalog, selected_skills or [])
    if selected_prompt:
        system_prompt = f"{system_prompt}\n\n{selected_prompt}"

    # (5) middleware
    context7_tools: list[BaseTool] = []
    if app_config.commitment.enabled:
        from caspian.mcp import get_context7_tools

        context7_tools = await get_context7_tools(app_config.commitment.context7_url)
        if not context7_tools:
            raise RuntimeError("CommitmentMiddleware 已启用，但 Context7 工具不可用")
    middleware = _build_middlewares(
        app_config,
        model,
        context7_tools,
        frozenset(catalog.names),
        subagent_enabled=subagent_enabled,
    )

    # (6) create_agent
    return create_agent(
        model=model,
        tools=tools,
        middleware=middleware,
        system_prompt=system_prompt,
        state_schema=LeadAgentState,
    )
