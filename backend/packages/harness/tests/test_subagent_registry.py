import unittest
from types import SimpleNamespace

from caspian.config.subagents_config import (
    CustomSubagentConfig,
    SubagentOverrideConfig,
    SubagentsAppConfig,
    clamp_subagent_concurrency,
    clamp_total_subagents_per_run,
)
from caspian.subagents.config import BUILTIN_SUBAGENTS, SubagentConfig, resolve_subagent_model_name
from caspian.subagents.registry import (
    get_available_subagent_names,
    get_subagent_config,
)


def _app_config(subagents):
    return SimpleNamespace(subagents=subagents)


class RegistryLayerTests(unittest.TestCase):
    def test_builtin_general_purpose(self):
        config = get_subagent_config("general-purpose")
        self.assertIsNotNone(config)
        self.assertEqual(config.max_turns, 150)

    def test_builtin_bash(self):
        config = get_subagent_config("bash")
        self.assertIsNotNone(config)
        self.assertEqual(config.max_turns, 60)

    def test_unknown_type_returns_none(self):
        self.assertIsNone(get_subagent_config("not-a-real-type"))

    def test_custom_agent_resolution(self):
        subagents = SubagentsAppConfig(
            custom_agents={
                "my-analyst": CustomSubagentConfig(
                    description="分析师",
                    system_prompt="你是分析师",
                    max_turns=20,
                    timeout_seconds=120,
                )
            }
        )
        config = get_subagent_config("my-analyst", app_config=_app_config(subagents))
        self.assertIsNotNone(config)
        self.assertEqual(config.max_turns, 20)
        self.assertEqual(config.timeout_seconds, 120)
        self.assertEqual(config.system_prompt, "你是分析师")

    def test_custom_agent_in_available_names(self):
        subagents = SubagentsAppConfig(
            custom_agents={"my-analyst": CustomSubagentConfig(description="d", system_prompt="p")}
        )
        names = get_available_subagent_names(app_config=_app_config(subagents))
        self.assertIn("my-analyst", names)
        self.assertIn("general-purpose", names)

    def test_per_agent_override_wins(self):
        subagents = SubagentsAppConfig(
            timeout_seconds=1800,
            agents={"general-purpose": SubagentOverrideConfig(timeout_seconds=300)},
        )
        config = get_subagent_config("general-purpose", app_config=_app_config(subagents))
        self.assertEqual(config.timeout_seconds, 300)

    def test_global_default_applies_to_builtin(self):
        subagents = SubagentsAppConfig(timeout_seconds=1800)
        config = get_subagent_config("general-purpose", app_config=_app_config(subagents))
        self.assertEqual(config.timeout_seconds, 1800)

    def test_global_default_does_not_override_custom(self):
        subagents = SubagentsAppConfig(
            timeout_seconds=1800,
            custom_agents={
                "my-agent": CustomSubagentConfig(description="d", system_prompt="p", timeout_seconds=900)
            },
        )
        config = get_subagent_config("my-agent", app_config=_app_config(subagents))
        self.assertEqual(config.timeout_seconds, 900)


class ModelInheritanceTests(unittest.TestCase):
    def test_inherit_parent_model(self):
        config = SubagentConfig(name="x", description="d", model="inherit")
        self.assertEqual(
            resolve_subagent_model_name(config, parent_model="deepseek-v4-flash"), "deepseek-v4-flash"
        )

    def test_explicit_model_wins(self):
        config = SubagentConfig(name="x", description="d", model="other-model")
        self.assertEqual(
            resolve_subagent_model_name(config, parent_model="deepseek-v4-flash"), "other-model"
        )

    def test_fallback_to_default_model(self):
        app_config = SimpleNamespace(models=[SimpleNamespace(name="default-model")])
        config = SubagentConfig(name="x", description="d", model="inherit")
        self.assertEqual(
            resolve_subagent_model_name(config, parent_model=None, app_config=app_config),
            "default-model",
        )


class ClampTests(unittest.TestCase):
    def test_clamp_concurrency(self):
        self.assertEqual(clamp_subagent_concurrency(99), 4)
        self.assertEqual(clamp_subagent_concurrency(0), 1)
        self.assertEqual(clamp_subagent_concurrency(2), 2)

    def test_clamp_total(self):
        self.assertEqual(clamp_total_subagents_per_run(0), 1)
        self.assertEqual(clamp_total_subagents_per_run(99), 50)
        self.assertEqual(clamp_total_subagents_per_run(6), 6)


class BuiltinDefaultsTests(unittest.TestCase):
    def test_builtin_disable_task_by_default(self):
        self.assertIn("task", BUILTIN_SUBAGENTS["general-purpose"].disallowed_tools)


if __name__ == "__main__":
    unittest.main()
