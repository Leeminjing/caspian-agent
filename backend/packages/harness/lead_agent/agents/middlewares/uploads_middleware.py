"""
本文件对外提供 `UploadsMiddleware` 类，作为 agent 启动时的上传文件初始化中间件。

对外提供:
    UploadsMiddleware(AgentMiddleware) — 覆盖 before_agent 钩子，在 agent 启动时执行文件上传相关的初始化逻辑

输入:
    before_agent:
        state: AgentState — 当前 agent 状态
        runtime: ToolRuntime — LangGraph 运行时注入，提供 context / store / stream_writer / config

输出:
    dict | None — 返回 None 表示不修改 state

具体工作流:
    before_agent:
    (1) 当前为骨架实现，返回 None
    (2) 具体逻辑（如读取 uploaded_files、设置 workspace 路径等）留待后续 change 填充

示例:
    from lead_agent.agents.middlewares.uploads_middleware import UploadsMiddleware

    middleware = UploadsMiddleware()
    # 在 create_agent(middleware=[middleware, ...]) 中使用
"""

from langchain.agents.middleware import AgentMiddleware


class UploadsMiddleware(AgentMiddleware):

    def before_agent(self, state, runtime) -> dict | None:
        """agent 启动时调用的钩子，当前为骨架实现。

        输入:
            state: AgentState — 当前 agent 状态
            runtime: ToolRuntime — 运行时注入

        输出:
            None — 当前不修改 state
        """
        # ponytail: 骨架实现，后续 change 填充具体逻辑
        return None
