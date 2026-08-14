"""开发启动入口脚本。

对外提供:
    main() — 以 uvicorn 启动 Caspian 网关

输入: 可选命令行参数指定端口（默认 8000）

输出: 无（阻塞运行 HTTP 服务，Ctrl+C 退出）

具体工作流:
    (1) 从命令行参数解析端口，构造 uvicorn.Config（loop="none"，不委托 uvicorn 创建事件循环）
    (2) Windows 上 psycopg 异步驱动要求 SelectorEventLoop（ProactorEventLoop
        不兼容），因此用 loop_factory 显式创建后运行 server.serve()
    (3) 非 Windows 平台直接 asyncio.run

示例:
    python run_dev.py
    python run_dev.py 8002
"""

import asyncio
import selectors
import sys

import uvicorn


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    config = uvicorn.Config(
        "backend.app.gateway.app:app",
        host="127.0.0.1",
        port=port,
        loop="none",
    )
    server = uvicorn.Server(config)
    if sys.platform == "win32":
        asyncio.run(
            server.serve(),
            loop_factory=lambda: asyncio.SelectorEventLoop(
                selectors.SelectSelector()
            ),
        )
    else:
        asyncio.run(server.serve())


if __name__ == "__main__":
    main()
