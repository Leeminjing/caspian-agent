"""
本文件对外提供 CommitmentConfig，声明承诺层的启停与 Context7 远程地址。

输入:
    config.yaml 中 commitment 段

输出:
    CommitmentConfig — 供 lead agent 装配层判断是否注册 CommitmentMiddleware
"""

from pydantic import BaseModel


class CommitmentConfig(BaseModel):
    enabled: bool = False
    context7_url: str = "https://mcp.context7.com/mcp"
