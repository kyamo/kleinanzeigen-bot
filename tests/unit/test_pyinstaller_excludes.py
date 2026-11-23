# SPDX-FileCopyrightText: © Sebastian Thomschke and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
"""The frozen binary lacks the modules excluded in pyinstaller.spec, so the app must not import them."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parent.parent.parent


def _excluded_modules() -> set[str]:
    tree = ast.parse((ROOT / "pyinstaller.spec").read_text(encoding = "utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "excluded_modules" for t in node.targets):
            return {elt.value for elt in node.value.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)}  # type: ignore[attr-defined]
    raise AssertionError("excluded_modules not found in pyinstaller.spec")


def test_app_does_not_import_modules_excluded_from_the_binary() -> None:
    excluded = _excluded_modules() - {"mypy", "rich", "toml", "setuptools"}  # dev-only dependencies
    assert "statistics" in excluded
    offenders:list[str] = []
    for source in sorted((ROOT / "src" / "kleinanzeigen_bot").rglob("*.py")):
        for node in ast.walk(ast.parse(source.read_text(encoding = "utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            else:
                continue
            offenders.extend(
                f"{source.relative_to(ROOT)}:{node.lineno} imports {name}" for name in names if name.split(".")[0] in excluded or name in excluded
            )
    assert not offenders, "\n".join(offenders)
