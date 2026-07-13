"""
本文件对外提供 make_lead_agent 异步工厂函数和 _build_middlewares 内部函数，
作为 lead_agent 装配的唯一对外入口。

对外提供:
    make_lead_agent — 装配并返回可执行的 CompiledStateGraph
    _build_middlewares — 组装 lead_agent 的中间件链（通用 + 专属）

输入:
    make_lead_agent:
        model_name: str | None — 目标模型名，None 时取 config.yaml 中 models[0] 作为默认
        agent_name: str | None — system prompt 中的 agent 名称，None 时使用默认值 "Caspian"
        tool_groups: list[str] | None — 需要加载的工具分组名列表，None 表示加载全部

输出:
    CompiledStateGraph — langchain.agents.create_agent() 产出的可执行 agent graph

具体工作流:
    (1) 调用 create_chat_model(name=model_name) 获取 BaseChatModel 实例
    (2) await get_available_tools(tool_groups=tool_groups) 汇集三类工具并去重过滤
    (3) 调用 _build_middlewares() 获取中间件列表
        (3a) 调用 build_general_middlewares() 获取通用中间件链
        (3b) 追加 lead_agent 专属中间件（当前无额外中间件，预留 extend 调用点）
    (4) 调用 apply_prompt_template(agent_name=agent_name) 生成 system_prompt 字符串
    (5) 调用 langchain.agents.create_agent(model, tools, middleware, system_prompt, state_schema=LeadAgentState)
    (6) 返回 CompiledStateGraph

示例:
    graph = await make_lead_agent()
    graph = await make_lead_agent(model_name="deepseek-v4-flash", agent_name="DeepSeek")
    graph = await make_lead_agent(tool_groups=["file:read", "bash"])
"""

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langgraph.graph.state import CompiledStateGraph

from lead_agent.agents.lead.prompt import apply_prompt_template
from lead_agent.agents.lead_agent_state import LeadAgentState
from lead_agent.agents.middlewares.builder import build_general_middlewares
from lead_agent.models import create_chat_model
from lead_agent.tools import get_available_tools


def _build_middlewares() -> list[AgentMiddleware]:
    """组装 lead_agent 的中间件链：通用链 + lead_agent 专属中间件。

    输入: 无

    输出:
        list[AgentMiddleware] — 按顺序排列的中间件列表

    工作流:
        (1) 调用 build_general_middlewares() 获取通用中间件链
        (2) 追加 lead_agent 专属中间件（当前无额外中间件，预留 extend 调用点）
        (3) 返回完整列表
    """
    middlewares = build_general_middlewares()
    # ponytail: lead_agent 专属中间件预留扩展点
    return middlewares


async def make_lead_agent(
    model_name: str | None = None,
    agent_name: str | None = None,
    tool_groups: list[str] | None = None,
) -> CompiledStateGraph:
    model = create_chat_model(name=model_name)
    tools = await get_available_tools(tool_groups=tool_groups)
    system_prompt = apply_prompt_template(agent_name=agent_name)

    middleware = _build_middlewares()

    return create_agent(
        model=model,
        tools=tools,
        middleware=middleware,
        system_prompt=system_prompt,
        state_schema=LeadAgentState,
    )
