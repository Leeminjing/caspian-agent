"""
本文件验证 lead agent 系统提示词重写后的机械不变量：
占位符渲染、无未转义花括号、无幽灵工具名、工具名引用可解析、
动态段 XML 封装（subagent / goal policy）。

这些断言是 prose 提示词唯一能机械守护的约束：一旦未来编辑重新引入
错误工具名或破坏 str.format() 占位符，本测试即失败。
"""

import re
import unittest
from types import SimpleNamespace

from caspian.agents.lead.prompt import (
    apply_prompt_template,
    build_subagent_section,
)
from caspian.config.subagents_config import SubagentsAppConfig
from caspian.goal.guidance import goal_guidance

# 真实 @tool 注册名契约（与 config.yaml / 各 builtins / web / goal 工具一致）。
# 若未来重命名工具，须同步此表与提示词。
_KNOWN_TOOL_NAMES = frozenset({
    "read_file_tool",
    "write_file_tool",
    "bash_tool",
    "powershell_tool",
    "cmd_tool",
    "sh_tool",
    "web_search_tool",
    "web_fetch_tool",
    "present_file_tool",
    "view_image_tool",
    "list_uploaded_files",
    "update_decision_table",
    "add_knowledge",
    "knowledge_query",
    "task",
    "describe_skill",
    "exit_plan_mode",
    "get_goal",
    "create_goal",
    "update_goal",
})

# 曾出现在旧提示词里的幽灵工具名，以独立词出现即失败。
_GHOST_TOOL_NAMES = ("read_file", "present_files", "grep")

_WORD_BOUNDARY = r"(?<![A-Za-z0-9_]){name}(?![A-Za-z0-9_])"

_BASE_SECTIONS = (
    "<identity>",
    "<operating_modes>",
    "<discrete_levels>",
    "<thinking_style>",
    "<working_directory>",
    "<knowledge_system>",
    "<skill_system>",
    "<response_style>",
)


def _rendered() -> str:
    return apply_prompt_template(
        agent_name="Caspian",
        skill_names="docx, vision",
        container_base_path="/mnt/skills",
    )


class LeadPromptConsistencyTests(unittest.TestCase):

    def test_template_renders_and_fills_placeholders(self):
        s = _rendered()
        self.assertIn("Caspian", s)
        self.assertIn("docx, vision", s)
        self.assertIn("/mnt/skills", s)

    def test_no_unescaped_braces_remain(self):
        s = _rendered()
        self.assertNotIn("{", s)
        self.assertNotIn("}", s)

    def test_core_sections_present(self):
        s = _rendered()
        for section in _BASE_SECTIONS:
            self.assertIn(section, s)

    def test_no_ghost_tool_names(self):
        s = _rendered()
        for name in _GHOST_TOOL_NAMES:
            self.assertIsNone(
                re.search(_WORD_BOUNDARY.format(name=re.escape(name)), s),
                f"ghost tool name present: {name}",
            )

    def test_referenced_suffixed_tool_names_resolve(self):
        s = _rendered()
        for token in re.findall(r"\b[a-z][a-z0-9_]*_tool\b", s):
            self.assertIn(token, _KNOWN_TOOL_NAMES, f"unregistered _tool name: {token}")

    def test_expected_tool_names_present(self):
        s = _rendered()
        for name in (
            "read_file_tool",
            "write_file_tool",
            "bash_tool",
            "present_file_tool",
            "list_uploaded_files",
            "describe_skill",
            "add_knowledge",
            "knowledge_query",
            "exit_plan_mode",
        ):
            self.assertIn(name, s)

    def test_subagent_section_xml_wrapped(self):
        cfg = SimpleNamespace(subagents=SubagentsAppConfig())
        section = build_subagent_section(app_config=cfg)
        self.assertTrue(section.startswith("<subagent_delegation>"), section[:80])
        self.assertTrue(
            section.rstrip().endswith("</subagent_delegation>"), section[-80:]
        )

    def test_goal_guidance_xml_wrapped(self):
        text = goal_guidance(3)
        self.assertTrue(text.startswith("<goal_policy>"), text[:60])
        self.assertTrue(text.rstrip().endswith("</goal_policy>"), text[-60:])


if __name__ == "__main__":
    unittest.main()
