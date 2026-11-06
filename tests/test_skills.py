from pathlib import Path

from repofix.skills import SkillLoader


def test_builtin_skills_use_progressive_disclosure():
    root = Path(__file__).parents[1] / "src" / "repofix" / "builtin_skills"
    loader = SkillLoader([root])
    catalog = loader.catalog()
    assert "pytest-debugging" in catalog
    assert "当前已知范围最小" not in catalog
    assert "当前已知范围最小" in loader.load("pytest-debugging")
