"""Skills read from disk on demand, without scripts.

Skills inform the model; they never grant permissions or run code. A skill that ships scripts is
rejected, and the hash of every loaded SKILL.md is recorded on a span.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from google.adk.skills import Frontmatter, Skill, SkillRegistry, load_skill_from_dir
from opentelemetry import trace

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_tracer = trace.get_tracer("baranda.agents")


class DiskSkillRegistry(SkillRegistry):
    """Loads ``<root>/<name>/SKILL.md`` every time it is asked, so new skills need no restart."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if (p / "SKILL.md").is_file() and _NAME.match(p.name))

    def load(self, name: str) -> Skill:
        if not _NAME.match(name):
            raise ValueError(f"invalid skill name: {name!r}")
        path = self.root / name
        with _tracer.start_as_current_span("baranda.skill.load") as span:
            skill = load_skill_from_dir(path)
            if skill.resources.list_scripts():
                raise ValueError(f"skill {name!r} ships scripts; skills may not run code")
            digest = hashlib.sha256((path / "SKILL.md").read_bytes()).hexdigest()
            span.set_attribute("baranda.skill.name", name)
            span.set_attribute("baranda.skill.sha256", digest)
        return skill

    def load_all(self) -> list[Skill]:
        return [self.load(name) for name in self.names()]

    async def get_skill(self, *, name: str) -> Skill:
        return self.load(name)

    async def search_skills(self, *, query: str) -> list[Frontmatter]:
        words = query.lower().split()
        results = []
        for skill in self.load_all():
            haystack = f"{skill.frontmatter.name} {skill.frontmatter.description}".lower()
            if not words or any(word in haystack for word in words):
                results.append(skill.frontmatter)
        return results
