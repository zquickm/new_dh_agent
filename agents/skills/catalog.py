from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml


_FRONTMATTER = re.compile(
    r"\A---\s*\r?\n(?P<metadata>.*?)\r?\n---\s*(?:\r?\n|\Z)",
    re.DOTALL,
)
_VALID_NAME = re.compile(r"^[a-z0-9\u4e00-\u9fff]+(?:-[a-z0-9\u4e00-\u9fff]+)*$")


@dataclass(frozen=True)
class SkillSummary:
    name: str
    description: str


@dataclass(frozen=True)
class SkillDocument:
    name: str
    description: str
    instructions: str

    @property
    def summary(self) -> SkillSummary:
        return SkillSummary(name=self.name, description=self.description)


class SkillCatalog:
    """Discover and load local skills without exposing filesystem paths."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()

    def summaries(self) -> tuple[SkillSummary, ...]:
        return tuple(skill.summary for skill in self._discover().values())

    def load(self, name: str) -> SkillDocument:
        skill = self._discover().get(name)
        if skill is None:
            raise LookupError(f"Unknown skill: {name}")
        return skill

    def _discover(self) -> dict[str, SkillDocument]:
        if not self.directory.exists():
            return {}
        if not self.directory.is_dir():
            raise ValueError(f"Skill directory is not a directory: {self.directory}")

        skills: dict[str, SkillDocument] = {}
        for skill_file in sorted(self.directory.glob("*/SKILL.md")):
            self._ensure_inside_catalog(skill_file)
            skill = self._parse(skill_file)
            if skill.name in skills:
                raise ValueError(f"Duplicate skill name: {skill.name}")
            skills[skill.name] = skill
        return skills

    def _ensure_inside_catalog(self, skill_file: Path) -> None:
        try:
            skill_file.resolve().relative_to(self.directory)
        except ValueError as exc:
            raise ValueError("Skill file must stay inside the skill directory") from exc

    @staticmethod
    def _parse(skill_file: Path) -> SkillDocument:
        content = skill_file.read_text(encoding="utf-8")
        match = _FRONTMATTER.match(content)
        if match is None:
            raise ValueError(f"Skill is missing YAML frontmatter: {skill_file}")

        metadata = yaml.safe_load(match.group("metadata"))
        if not isinstance(metadata, dict):
            raise ValueError(f"Skill frontmatter must be an object: {skill_file}")

        name = metadata.get("name")
        description = metadata.get("description")
        if not isinstance(name, str) or not _VALID_NAME.fullmatch(name):
            raise ValueError(f"Skill has an invalid name: {skill_file}")
        if name != skill_file.parent.name:
            raise ValueError(f"Skill name must match its directory: {skill_file}")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"Skill has no description: {skill_file}")

        instructions = content[match.end():].strip()
        if not instructions:
            raise ValueError(f"Skill has no instructions: {skill_file}")
        return SkillDocument(
            name=name,
            description=" ".join(description.split()),
            instructions=instructions,
        )
