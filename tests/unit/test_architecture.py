"""Keep domain ownership from drifting back into catch-all modules."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "sagasu"
BOUNDARIES = {
    "artifacts": ("sagasu.artifacts", "sagasu.protocol"),
    "cdp": ("sagasu.artifacts.html", "sagasu.cdp", "sagasu.protocol"),
    "sessions": ("sagasu.artifacts", "sagasu.sessions", "sagasu.protocol"),
    "xcontrol": ("sagasu.xcontrol", "sagasu.protocol"),
}


@pytest.mark.parametrize("domain", sorted(BOUNDARIES))
def test_domain_imports_stay_within_declared_boundaries(domain):
    violations: list[str] = []
    for path in sorted((PACKAGE_ROOT / domain).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported = _sagasu_import(node)
            if imported is None:
                continue
            if not any(
                imported == allowed or imported.startswith(f"{allowed}.")
                for allowed in BOUNDARIES[domain]
            ):
                relative = path.relative_to(PACKAGE_ROOT)
                violations.append(f"{relative}:{node.lineno} imports {imported}")
    assert violations == []


def _sagasu_import(node: ast.AST) -> str | None:
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        return module if module == "sagasu" or module.startswith("sagasu.") else None
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name == "sagasu" or alias.name.startswith("sagasu."):
                return alias.name
    return None

