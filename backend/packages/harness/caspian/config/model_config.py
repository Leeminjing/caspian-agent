"""
本文件对外提供 SystemPromptUpdate、ModelCapabilities 与 ModelConfig 配置模型。

输入:
    ModelCapabilities.system_prompt_update — route 的 system 更新语义，允许 replace 或 in-history
    ModelConfig — 模型名称、实现类、endpoint、凭据、多模态标记与 route capabilities

输出:
    经 Pydantic 校验的类型化模型配置；未声明 capability 时安全返回 replace

具体工作流:
    YAML 字典先生成 ModelCapabilities，再生成 ModelConfig；非法 capability 在配置加载期失败，
    既有配置因 default_factory 自动获得 replace，不依赖模型名、provider 名、类名或 URL 推断。

示例:
    ModelConfig(...).capabilities.system_prompt_update is SystemPromptUpdate.REPLACE
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class SystemPromptUpdate(str, Enum):
    """route 支持的完整 system prompt 更新方式。"""

    REPLACE = "replace"
    IN_HISTORY = "in-history"


class ModelCapabilities(BaseModel):
    """一个模型/endpoint route 的显式能力声明。"""

    model_config = ConfigDict(extra="forbid")

    system_prompt_update: SystemPromptUpdate = SystemPromptUpdate.REPLACE


class ModelConfig(BaseModel):
    """模型 route 配置。"""

    name: str
    display_name: str
    use: str
    model: str
    api_key: str
    base_url: str
    vision: bool = False
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
