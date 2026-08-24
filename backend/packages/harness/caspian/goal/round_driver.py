"""
本文件对外提供 GoalRoundDriver：一次 run 内的目标自动推进决策器。

它不像 deepseek 那样在"agent 空闲"边界注入（Caspian 无常驻活体代理），而是在 run_agent 的
astream 调用者层循环里被 worker 逐轮询问：一轮结束后 decide_after_round() 决定是否继续，
build_next_round_input() 构造包含 goal-round 标记的下一条续跑用户消息。

输入:
    store: BaseStore — LangGraph Store
    user_id / thread_id — 目标命名空间
    default_max_goal_rounds — create 省略时的默认上限

输出:
    disarm_on_run_start() → None（run 起点 disarms 既有 armed 目标）
    decide_after_round() → 'continue' | 'stop'
    build_next_round_input() → {"messages": [HumanMessage]}

示例:
    driver = GoalRoundDriver(store, user_id, thread_id, cfg.default_max_goal_rounds)
    await driver.disarm_on_run_start()
    action = await driver.decide_after_round()
    if action == 'continue':
        next_input = await driver.build_next_round_input()
"""

from langchain_core.messages import HumanMessage

from caspian.goal.prompt import render_goal_round_prompt
from caspian.goal.service import GoalService


class GoalRoundDriver:
    """目标自动推进决策器（进程内一次 run 的循环）。"""

    def __init__(
        self,
        store,
        user_id: str,
        thread_id: str,
        default_max_goal_rounds: int = 256,
    ) -> None:
        self._service = GoalService(
            store=store,
            user_id=user_id,
            thread_id=thread_id,
            default_max_goal_rounds=default_max_goal_rounds,
        )

    async def disarm_on_run_start(self) -> None:
        """run 起点 disarms 既有 armed 目标（run/进程边界不自动续跑）。"""
        await self._service.disarm()

    async def current_view(self) -> dict | None:
        """当前目标视图（供前端/SSE 展示）。"""
        goal = await self._service.get()
        return None if goal is None else goal.to_dict()

    async def decide_after_round(self) -> str:
        """一轮结束后决定是否注入下一轮。返回 'continue' 或 'stop'。"""
        goal = await self._service.get()
        if goal is None or goal.phase != "active" or not goal.armed:
            return "stop"
        if goal.rounds_started >= goal.max_goal_rounds:
            # 达到上限 → 自动阻塞，并停止
            await self._service.block(
                {"id": goal.id, "revision": goal.revision},
                "round-limit",
                f"Goal reached its configured limit of {goal.max_goal_rounds} rounds.",
            )
            return "stop"
        return "continue"

    async def build_next_round_input(self) -> dict:
        """推进 rounds_started 并构造下一条 <goal_round> 续跑用户消息（携 goal-round 标记）。"""
        goal = await self._service.get()
        if goal is None or goal.phase != "active":
            raise RuntimeError("build_next_round_input 需要当前 active 目标")
        next_round = goal.rounds_started + 1
        # 推进计数器（不改变 revision / armed / phase）
        await self._service.advance_round({"id": goal.id, "revision": goal.revision})
        content = render_goal_round_prompt(goal.objective, next_round, goal.max_goal_rounds)
        return {
            "messages": [
                HumanMessage(
                    content=content,
                    additional_kwargs={
                        "goal_round": {
                            "goal_id": goal.id,
                            "revision": goal.revision,
                            "round": next_round,
                        }
                    },
                )
            ]
        }
