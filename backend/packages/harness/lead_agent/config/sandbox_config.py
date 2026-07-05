"""
本文件定义 SandboxConfig Pydantic 配置模型。

输入: config.yaml 中 sandbox 段的原始数据
输出: SandboxConfig 实例，字段 use 指定沙箱实现类的模块路径
"""

from pydantic import BaseModel


class SandboxConfig(BaseModel):
    use: str
