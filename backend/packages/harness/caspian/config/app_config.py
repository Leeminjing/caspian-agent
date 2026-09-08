"""
本文件对外提供 get_app_config、reload_app_config 两个公开函数，以及 AppConfig 配置聚合类。

AppConfig: 声明式配置数据模型，聚合 models / tools / skills / sandbox / commitment / runtime 与持久化配置
get_app_config: 组合根入口，将 config.yaml 加载为全局单例 AppConfig 对象
reload_app_config: 强制刷新全局单例，修改 config.yaml 后立即生效

完整加载工作流：
_load_yaml 读取 YAML 文件 → _resolve_env_vars 解析 $ENV_VAR 环境变量引用
→ AppConfig.model_validate 由 dict 递归生成 AppConfig + 子 Pydantic 对象
→ 写入模块级 _app_config 单例缓存，后续 get_app_config 直接返回
"""

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from caspian.config.agent_config import AgentConfig
from caspian.config.checkpointer_config import CheckpointerConfig
from caspian.config.commitment_config import CommitmentConfig
from caspian.config.context_compression_config import ContextCompressionConfig
from caspian.config.database_config import DatabaseConfig
from caspian.config.extensions_config import ExtensionsConfig
from caspian.config.goal_mode_config import GoalModeConfig
from caspian.config.knowledge_config import KnowledgeConfig
from caspian.config.langgraph_store_config import LanggraphStoreConfig
from caspian.config.model_config import ModelConfig
from caspian.config.plan_mode_config import PlanModeConfig
from caspian.config.sandbox_config import SandboxConfig
from caspian.config.skills_config import SkillsConfig
from caspian.config.stream_bridge_config import StreamBridgeConfig
from caspian.config.subagents_config import SubagentsAppConfig
from caspian.config.tool_config import ToolConfig
from caspian.config.tool_group_config import ToolGroupConfig


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    models: list[ModelConfig]
    tool_groups: list[ToolGroupConfig]
    tools: list[ToolConfig]
    skills: SkillsConfig
    sandbox: SandboxConfig
    stream_bridge: StreamBridgeConfig = StreamBridgeConfig()
    database: DatabaseConfig | None = None
    checkpointer: CheckpointerConfig = CheckpointerConfig()
    langgraph_store: LanggraphStoreConfig = LanggraphStoreConfig()
    extensions: ExtensionsConfig = ExtensionsConfig(mcp_servers={})
    commitment: CommitmentConfig = CommitmentConfig()
    context_compression: ContextCompressionConfig = ContextCompressionConfig()
    subagents: SubagentsAppConfig = SubagentsAppConfig()
    agent: AgentConfig = AgentConfig()
    plan_mode: PlanModeConfig = PlanModeConfig()
    goal_mode: GoalModeConfig = GoalModeConfig()
    knowledge: KnowledgeConfig = KnowledgeConfig()

    def _normalize_name(self, name: str) -> str:
        return name.strip()

    @staticmethod
    def _get_model_path(use: str) -> tuple[str, str]:
        parts = use.split(":", 1)
        if len(parts) != 2:
            raise ValueError(f"无效的 use 格式: '{use}'，应为 'module:Class'")
        return parts[0], parts[1]

    def get_model(self, name: str) -> ModelConfig:
        normalized = self._normalize_name(name)
        for m in self.models:
            if m.name == normalized:
                return m
        raise KeyError(f"未找到模型配置: '{name}'")

    def get_tools_by_group(self, group: str) -> list[ToolConfig]:
        return [t for t in self.tools if t.group == group]


_app_config: AppConfig | None = None


def _load_yaml(path: str) -> dict:
    yaml_path = Path(path)
    if not yaml_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with open(yaml_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _resolve_env_vars(data: dict) -> dict:
    resolved = {}
    for key, value in data.items():
        if isinstance(value, dict):
            resolved[key] = _resolve_env_vars(value)
        elif isinstance(value, list):
            resolved[key] = [
                _resolve_env_vars(item) if isinstance(item, dict) else _resolve_env_item(item)
                for item in value
            ]
        else:
            resolved[key] = _resolve_env_item(value)
    return resolved


_ENV_WITH_DEFAULT_RE = re.compile(
    r"^\$\{([A-Z_][A-Z0-9_]*):-(.*)\}$|^\$([A-Z_][A-Z0-9_]*):-(.*)$"
)
_BARE_ENV_RE = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)\}$|^\$([A-Z_][A-Z0-9_]*)$")


def _resolve_env_item(value):
    if isinstance(value, str) and len(value) > 1 and value.startswith("$"):
        # 支持 ${VAR:-default} 或 $VAR:-default：未设置时回落默认值，不抛错。
        m = _ENV_WITH_DEFAULT_RE.match(value)
        if m:
            var_name = m.group(1) or m.group(3)
            default = m.group(2) or m.group(4)
            env_value = os.environ.get(var_name)
            return env_value if env_value is not None else default
        # 纯 ${VAR} 或 $VAR：缺失则抛 KeyError（保持既有严格行为）。
        m = _BARE_ENV_RE.match(value)
        if m:
            var_name = m.group(1) or m.group(2)
            env_value = os.environ.get(var_name)
            if env_value is None:
                raise KeyError(f"环境变量未设置: {var_name}")
            return env_value
        return value
    return value


def _resolve_config_path(yaml_path: str) -> str:
    """解析 config.yaml 路径，使其不依赖进程工作目录。

    输入:
        yaml_path: str — 配置路径（可为相对/绝对）

    输出:
        str — 解析后的绝对路径

    工作流:
        (1) 传入路径已存在 → 原样返回（保持既有行为，cwd 优先）
        (2) 相对路径且 cwd 不存在 → 回退到 CASPIAN_HOME 托管 app 目录下的 config.yaml
        (3) 均不存在 → 原样返回，由调用方抛 FileNotFoundError
    """
    p = Path(yaml_path)
    if p.exists():
        return yaml_path
    if not p.is_absolute():
        raw_home = os.environ.get("CASPIAN_HOME")
        base = Path(raw_home).expanduser() if raw_home else (Path.home() / ".caspian")
        alt = base / "app" / p
        if alt.exists():
            return str(alt)
    return yaml_path


def get_app_config(yaml_path: str) -> AppConfig:
    global _app_config
    if _app_config is None:
        raw = _load_yaml(_resolve_config_path(yaml_path))
        resolved = _resolve_env_vars(raw)
        _app_config = AppConfig.model_validate(resolved)
    return _app_config


def reload_app_config(yaml_path: str) -> AppConfig:
    global _app_config
    raw = _load_yaml(_resolve_config_path(yaml_path))
    resolved = _resolve_env_vars(raw)
    _app_config = AppConfig.model_validate(resolved)
    return _app_config
