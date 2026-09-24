"""
本文件验证模型 route capability 的类型化加载与显式选择。

输入为 ModelConfig 字典或模型列表，输出为校验后的 capability/route；工作流覆盖旧配置缺省、
显式 in-history、非法值拒绝及 DeepSeek 名称不触发推断。

示例: pytest tests/test_model_capabilities.py
"""

from types import SimpleNamespace
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from caspian.config.model_config import ModelConfig, SystemPromptUpdate
from caspian.models.factory import resolve_model_config


def _config(**overrides):
    values = {
        "name": "deepseek-looking-route",
        "display_name": "DeepSeek looking route",
        "use": "caspian.models.deepseek:DeepSeekChatOpenAI",
        "model": "deepseek-v4-flash",
        "api_key": "test",
        "base_url": "https://api.deepseek.com",
    }
    values.update(overrides)
    return ModelConfig.model_validate(values)


def test_existing_config_defaults_to_replace_without_name_inference():
    config = _config()
    assert config.capabilities.system_prompt_update is SystemPromptUpdate.REPLACE


def test_repository_config_yaml_loads_unchanged_with_safe_default():
    root = Path(__file__).resolve().parents[4]
    raw = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    config = ModelConfig.model_validate(raw["models"][0])
    assert config.capabilities.system_prompt_update is SystemPromptUpdate.REPLACE


def test_explicit_in_history_is_preserved_by_route_selection():
    config = _config(capabilities={"system_prompt_update": "in-history"})
    app_config = SimpleNamespace(models=[config])
    selected = resolve_model_config("deepseek-looking-route", app_config=app_config)
    assert selected is config
    assert selected.capabilities.system_prompt_update is SystemPromptUpdate.IN_HISTORY


def test_invalid_capability_fails_at_validation():
    with pytest.raises(ValidationError):
        _config(capabilities={"system_prompt_update": "append-ish"})


def test_unknown_capability_key_fails_at_validation():
    with pytest.raises(ValidationError):
        _config(capabilities={"system_prompt_update": "replace", "magic": True})
