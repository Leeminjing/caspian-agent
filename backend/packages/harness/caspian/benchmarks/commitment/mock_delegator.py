"""mock delegator:每个阶段返回一个合法 WorkerOutput,零 LLM 跑完 supervisor。"""

from __future__ import annotations

from caspian.agents.commitment.delegation import ReviewedDelegator
from caspian.agents.commitment.schemas import WorkerOutput

# 各阶段(1-7)的最小合法结果;阶段 8/9 由 supervisor 直接计算,不经 delegator
MOCK_RESULTS: dict[int, dict] = {
    1: {"goal": "明确单一主目标"},
    2: {
        "requirements": ["要求A"],
        "discarded_requirements": [],
        "compatibility_checks": [
            {
                "technology": "Web",
                "application_type": "web",
                "ui_surface": "browser",
                "runtime_platform": "any",
                "host_model": "n/a",
                "status": "verified",
            }
        ],
        "conflicts": [],
        "table_conflicts": [],
    },
    3: {"requirements": [{"requirement": "要求A", "priority": 3}]},
    4: {"files": [], "urls": []},
    5: {
        "technologies": [
            {
                "name": "React",
                "project_version": "unresolved",
                "version": "19.0.0",
                "source_url": None,
                "version_basis": "official_docs_explicit",
                "version_evidence": "react.dev 官方发布",
            }
        ]
    },
    6: {
        "knowledge": [
            {
                "technology": "React",
                "version": "19.0.0",
                "source_url": "https://react.dev",
                "content": "React 19 官方文档正文。",
            }
        ]
    },
    7: {"contract_markdown": "# 任务合同\n\n## 要求\n\n- 要求A (priority 3)"},
}


class MockDelegator(ReviewedDelegator):
    """覆盖 run(),直接返回各阶段合法结果,不调用任何 LLM。"""

    def __init__(self) -> None:
        super().__init__(None, [])
        self.run_count = 0

    async def run(self, envelope, supervisor_messages=None):
        self.run_count += 1
        return WorkerOutput(result=dict(MOCK_RESULTS[envelope.stage])), ""
