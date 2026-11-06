from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel


class SkillInfo(BaseModel):
    name: str
    description: str
    path: str


class SkillLoader:
    def __init__(self, roots: list[Path]):
        self.roots = [root.resolve() for root in roots if root.exists()]

    def discover(self) -> list[SkillInfo]:
        found: dict[str, SkillInfo] = {}
        for root in self.roots:
            for file in root.glob("**/SKILL.md"):
                text = file.read_text(encoding="utf-8", errors="replace")
                name = file.parent.name
                description = ""
                header = re.match(r"^---\s*\n(.*?)\n---", text, flags=re.DOTALL)
                if header:
                    for line in header.group(1).splitlines():
                        key, _, value = line.partition(":")
                        if key.strip() == "name" and value.strip():
                            name = value.strip().strip("'\"")
                        if key.strip() == "description":
                            description = value.strip().strip("'\"")
                if not description:
                    body = re.sub(r"^---.*?---\s*", "", text, flags=re.DOTALL).strip()
                    description = body.split("\n\n", 1)[0].replace("\n", " ")[:240]
                found[name] = SkillInfo(name=name, description=description, path=str(file))
        return sorted(found.values(), key=lambda item: item.name)

    def load(self, name: str) -> str:
        match = next((item for item in self.discover() if item.name == name), None)
        if not match:
            raise KeyError(f"Unknown skill: {name}")
        return Path(match.path).read_text(encoding="utf-8", errors="replace")

    def catalog(self) -> str:
        items = self.discover()
        if not items:
            return "No project skills are available."
        return "Available skills (load only when relevant):\n" + "\n".join(
            f"- {item.name}: {item.description}" for item in items
        )
