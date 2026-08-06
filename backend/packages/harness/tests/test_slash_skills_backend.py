import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from backend.app.gateway.routers.skills import list_skills
from backend.app.gateway.services import _validated_selected_skills
from caspian.agents.lead.agent import (
    _selected_skill_prompt,
    build_enabled_skill_catalog,
)
from caspian.agents.lead.prompt import apply_prompt_template
from caspian.skills.catalog import SkillCatalog
from caspian.skills.types import Skill


def write_skill(root: Path, dirname: str, name: str, description: str, body: str) -> None:
    path = root / dirname
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )


class SlashSkillsBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.temp.name)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.temp.cleanup()

    def test_catalog_lists_enabled_public_and_custom_sorted(self):
        root = Path(self.temp.name)
        write_skill(root / "skills", "zeta", "zeta", "Z desc", "Z body")
        write_skill(root / "skills", "disabled", "disabled", "D desc", "D body")
        write_skill(root / ".caspian/users/u1/skills", "alpha", "alpha", "A desc", "A body")
        Path("extensions_config.json").write_text(
            json.dumps({
                "skills": {
                    "zeta": {"enabled": True},
                    "alpha": {"enabled": True},
                    "disabled": {"enabled": False},
                }
            }),
            encoding="utf-8",
        )

        response = asyncio.run(list_skills(
            SimpleNamespace(state=SimpleNamespace(current_user=SimpleNamespace(id="u1")))
        ))

        self.assertEqual([skill.name for skill in response.skills], ["alpha", "zeta"])
        self.assertEqual([skill.description for skill in response.skills], ["A desc", "Z desc"])

    def test_invalid_selected_skill_raises_422(self):
        Path("extensions_config.json").write_text(
            json.dumps({"skills": {"docx": {"enabled": True}}}),
            encoding="utf-8",
        )
        write_skill(Path("skills"), "docx", "docx", "Docx desc", "Docx body")

        with self.assertRaises(HTTPException) as raised:
            _validated_selected_skills(
                SimpleNamespace(selected_skills=["docx", "missing"]),
                "u1",
            )

        self.assertEqual(raised.exception.status_code, 422)

    def test_selected_skill_prompt_contains_full_content_in_order(self):
        root = Path(self.temp.name)
        write_skill(root / "skills", "a", "a", "A desc", "A full body")
        write_skill(root / "skills", "b", "b", "B desc", "B full body")
        Path("extensions_config.json").write_text(
            json.dumps({"skills": {"a": {"enabled": True}, "b": {"enabled": True}}}),
            encoding="utf-8",
        )
        catalog = build_enabled_skill_catalog(user_id="u1")

        prompt = _selected_skill_prompt(catalog, ["b", "a"])

        self.assertLess(prompt.index("B full body"), prompt.index("A full body"))
        self.assertIn("later sections take precedence", prompt)

    def test_empty_selection_keeps_base_prompt(self):
        catalog = SkillCatalog()
        base = apply_prompt_template(skill_names="", container_base_path="")
        selected = _selected_skill_prompt(catalog, [])

        self.assertEqual(f"{base}\n\n{selected}" if selected else base, base)

    def test_selected_skill_read_failure_raises(self):
        catalog = SkillCatalog([
            Skill(
                name="gone",
                description="Gone",
                skill_file=Path("missing/SKILL.md"),
                enabled=True,
            )
        ])

        with self.assertRaises(FileNotFoundError):
            _selected_skill_prompt(catalog, ["gone"])


if __name__ == "__main__":
    unittest.main()
