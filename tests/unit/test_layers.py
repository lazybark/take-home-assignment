"""The dependency rule between layers (DDD): the domain depends on nothing else of ours, the
application only on the domain, infrastructure on the domain (and settings), and the API on
the rest. A new import that breaks it fails here, not in review."""

import ast
from pathlib import Path

import payments

ROOT = Path(payments.__file__).parent
ALLOWED = {
    "domain": {"domain"},
    "application": {"application", "domain"},
    "infrastructure": {"infrastructure", "domain", "config"},
    "api": {"api", "application", "domain", "infrastructure"},
}


def layer(module: str) -> str:
    return module.removeprefix("payments.").split(".")[0]


def imports_by_module() -> dict[str, set[str]]:
    found = {}

    for path in ROOT.rglob("*.py"):
        module = ".".join(("payments", *path.relative_to(ROOT).with_suffix("").parts))
        found[module] = {
            node.module
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("payments.")
        }

    return found


def test_every_import_follows_the_dependency_rule():
    violations = [
        f"{module} imports {target}"
        for module, targets in imports_by_module().items()
        if layer(module) in ALLOWED
        for target in targets
        if layer(target) not in ALLOWED[layer(module)]
    ]
    assert violations == []


def test_the_rule_covers_every_layer():
    """A new top-level package must be placed in a layer (or be app/config/log wiring)."""

    packages = {p.name for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith("_")}
    assert packages == set(ALLOWED)
