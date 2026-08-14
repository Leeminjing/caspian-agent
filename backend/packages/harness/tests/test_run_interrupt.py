"""
本文件对外提供 RunInterruptApiTests 测试类，验证 interrupt 端点的状态映射。

输入:
    FastAPI app（仅挂载 RunManager 到 app.state + thread_runs router）

输出:
    无 — unittest 断言结果

具体工作流:
    (1) 每个用例构造独立 RunManager 与 ASGI app
    (2) 构造不同状态的 RunRecord
    (3) 发起 POST /api/threads/{thread_id}/runs/{run_id}/interrupt 断言状态码与响应体

示例:
    python -m unittest tests.test_run_interrupt -v
"""

import unittest

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.app.gateway.routers.thread_runs import router
from caspian.runtime.runs.manager import RunManager
from caspian.runtime.runs.schemas import RunStatus


class RunInterruptApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mgr = RunManager()
        app = FastAPI()
        app.state.run_manager = self.mgr
        app.include_router(router, prefix="/api/threads")
        self.client = AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    def _create_run(self, thread_id: str = "th-1", status: RunStatus = RunStatus.running):
        record = self.mgr.create(thread_id=thread_id)
        self.mgr.update(record.run_id, status=status)
        return record

    async def test_interrupt_running_run(self):
        record = self._create_run()
        resp = await self.client.post(
            f"/api/threads/th-1/runs/{record.run_id}/interrupt"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json(),
            {"run_id": record.run_id, "status": "interrupted"},
        )
        self.assertEqual(self.mgr.get(record.run_id).status, RunStatus.interrupted)

    async def test_interrupt_is_idempotent(self):
        record = self._create_run(status=RunStatus.interrupted)
        resp = await self.client.post(
            f"/api/threads/th-1/runs/{record.run_id}/interrupt"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "interrupted")

    async def test_interrupt_missing_run(self):
        resp = await self.client.post("/api/threads/th-1/runs/does-not-exist/interrupt")
        self.assertEqual(resp.status_code, 404)

    async def test_interrupt_cross_thread_returns_404(self):
        record = self._create_run(thread_id="th-1")
        resp = await self.client.post(
            f"/api/threads/th-2/runs/{record.run_id}/interrupt"
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.mgr.get(record.run_id).status, RunStatus.running)

    async def test_interrupt_terminal_run_returns_409(self):
        for status in (RunStatus.success, RunStatus.error, RunStatus.timeout):
            with self.subTest(status=status):
                record = self._create_run(status=status)
                resp = await self.client.post(
                    f"/api/threads/th-1/runs/{record.run_id}/interrupt"
                )
                self.assertEqual(resp.status_code, 409)
                self.assertEqual(self.mgr.get(record.run_id).status, status)


if __name__ == "__main__":
    unittest.main()
