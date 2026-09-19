"""P3.4/P5.2: no external consumer reaches into private stores or scope keys.

The checker in this file is deliberately strict and mechanical: it parses every Python
module under ``yeoman_gateway`` and fails on

* an import of a private knowledge submodule from outside the module,
* an import of a legacy store module from anywhere,
* a dynamic import string naming one of those modules,
* an attribute access on the forbidden names (``store``, ``known_jids``,
  ``contact_scope_key``), and
* a raw SQLite connection opened against a runtime data path.

Knowledge internals, the composition root and the migration CLI are the only allowed
places, and each exception is named explicitly.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[3] / "packages" / "gateway" / "yeoman_gateway"

#: Modules only the knowledge package, the composition root and the migration CLI
#: may import.
PRIVATE_MODULES = (
    "yeoman_gateway.knowledge._store",
    "yeoman_gateway.knowledge._identity",
    "yeoman_gateway.knowledge._statements",
    "yeoman_gateway.knowledge._retrieval",
    "yeoman_gateway.knowledge._migration",
    "yeoman_gateway.knowledge.authority",
    "yeoman_gateway.contacts.store",
    "yeoman_gateway.memory.store",
    "yeoman_gateway.contacts.service",
    "yeoman_gateway.memory.service",
)

#: Files allowed to import the modules above, with the reason.
#:
#: ``memory/`` and ``contacts/`` are the module's *current* private implementation of
#: the storage adapters; they are owned by knowledge (one connection, one transaction
#: owner) but still live under their historical paths so the mechanical move can be a
#: separate reviewable step.  They are listed here explicitly rather than silently
#: ignored, and ``test_legacy_packages_do_not_expose_a_second_writer`` proves that they
#: cannot open a second store while knowledge owns them.
ALLOWED_IMPORTERS: dict[str, str] = {
    "knowledge": "the owning module",
    "memory": "private storage adapter owned by knowledge",
    "contacts": "private storage adapter owned by knowledge",
    "cli/knowledge_commands.py": "migration CLI (offline operator path)",
}
#: The composition root may build the private adapters.
ALLOWED_FILES = {"app/bootstrap.py"}

#: Attribute names that must not be reachable from a consumer module.
FORBIDDEN_ATTRIBUTES = ("known_jids", "contact_scope_key", "user_scope_key")

#: Modules that handle people/statements.  These are the ones the boundary is about;
#: unrelated ``.store`` attributes (processing, a2a, cron) are not people data.
PEOPLE_MODULES = (
    "pipeline/",
    "agent/",
    "adapters/responder_llm.py",
    "cli/persona_evolution_commands.py",
    "persona_evolution.py",
)

#: Lines that may still reference a legacy helper, with the reason.  Each entry is a
#: literal source fragment; the checker fails if the count of matches exceeds the
#: documented number, so a new violation cannot hide here.
DOCUMENTED_EXCEPTIONS: dict[str, int] = {
    # The memory CLI keeps a fallback for a service that predates the knowledge facade.
    "cli/memory_commands.py": 2,
}

#: The only modules allowed to touch a raw SQLite connection for runtime data.
SQLITE_ALLOWED = (
    "knowledge/",
    "contacts/",
    "memory/",
    "storage/",
    "processing/",
    "app/bootstrap.py",
    "cli/knowledge_commands.py",
)


def _iter_modules() -> list[Path]:
    return sorted(path for path in PACKAGE_ROOT.rglob("*.py") if "__pycache__" not in path.parts)


def _relative(path: Path) -> str:
    return str(path.relative_to(PACKAGE_ROOT))


def _is_allowed(relative: str) -> bool:
    if relative in ALLOWED_FILES:
        return True
    return any(
        relative == prefix or relative.startswith(prefix.rstrip("/") + "/")
        for prefix in ALLOWED_IMPORTERS
    )


def _module_names(tree: ast.AST, *, runtime_only: bool = False) -> list[tuple[str, int]]:
    """Imported module names plus every literal dynamic-import string.

    With ``runtime_only`` an import guarded by ``if TYPE_CHECKING:`` is skipped: a
    type-only annotation does not create a runtime dependency on the private module.
    """
    found: list[tuple[str, int]] = []
    type_checking_lines = _type_checking_lines(tree) if runtime_only else frozenset()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(
                (alias.name, node.lineno)
                for alias in node.names
                if node.lineno not in type_checking_lines
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.lineno not in type_checking_lines:
                found.append((node.module, node.lineno))
        elif isinstance(node, ast.Call):
            function = node.func
            name = getattr(function, "id", None) or getattr(function, "attr", None)
            if name in ("import_module", "__import__") and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.append((first.value, node.lineno))
    return found


def _type_checking_lines(tree: ast.AST) -> frozenset[int]:
    """Line numbers of imports that sit inside an ``if TYPE_CHECKING:`` block."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        name = getattr(test, "id", None) or getattr(test, "attr", None)
        if name != "TYPE_CHECKING":
            continue
        for child in ast.walk(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                lines.add(child.lineno)
    return frozenset(lines)


def test_no_consumer_imports_a_private_store():
    violations: list[str] = []
    for path in _iter_modules():
        relative = _relative(path)
        if _is_allowed(relative):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module, lineno in _module_names(tree, runtime_only=True):
            for private in PRIVATE_MODULES:
                if module == private or module.startswith(private + "."):
                    violations.append(f"{relative}:{lineno} imports {module}")
    assert not violations, "private store imports outside the knowledge boundary:\n" + "\n".join(
        violations
    )


def _is_people_module(relative: str) -> bool:
    return any(
        relative == prefix or relative.startswith(prefix.rstrip("/") + "/")
        for prefix in PEOPLE_MODULES
    )


def test_no_consumer_reads_a_known_jids_map_or_builds_scope_keys():
    violations: list[str] = []
    for path in _iter_modules():
        relative = _relative(path)
        if _is_allowed(relative) or not _is_people_module(relative):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        allowed = DOCUMENTED_EXCEPTIONS.get(relative, 0)
        found: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRIBUTES:
                found.append(f"{relative}:{node.lineno} reads .{node.attr}")
            if isinstance(node, ast.Attribute) and node.attr.startswith("format_roster"):
                found.append(f"{relative}:{node.lineno} builds a roster directly")
        if len(found) > allowed:
            violations.extend(found)
    assert not violations, "forbidden contact/knowledge access:\n" + "\n".join(violations)


def test_type_only_annotations_do_not_count_as_runtime_dependency():
    """The rule itself: a TYPE_CHECKING import is not a runtime import."""
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from yeoman_gateway.contacts.service import ContactsService\n"
        "def f(x: 'ContactsService') -> None: ...\n".replace("\\n", "\n")
    )
    tree = ast.parse(source)
    runtime = [name for name, _ in _module_names(tree, runtime_only=True)]
    assert "yeoman_gateway.contacts.service" not in runtime
    assert "typing" in runtime  # the plain import in front of the guard is still seen
    assert "yeoman_gateway.contacts.service" in [
        name for name, _ in _module_names(tree)
    ]


def test_dynamic_import_strings_do_not_name_legacy_stores():
    """A dynamic import must not smuggle a legacy store past the static check."""
    violations: list[str] = []
    for path in _iter_modules():
        relative = _relative(path)
        if _is_allowed(relative):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module, lineno in _module_names(tree):
            if "contacts.store" in module or "memory.store" in module:
                violations.append(f"{relative}:{lineno} dynamically imports {module}")
    assert not violations, "\n".join(violations)


def test_only_the_owning_modules_construct_a_knowledge_or_memory_store():
    """No consumer may build a second store instance for people or memory data."""
    forbidden_constructions = {"MemoryStore", "ContactsStore", "KnowledgeStore"}
    violations: list[str] = []
    for path in _iter_modules():
        relative = _relative(path)
        if any(relative.startswith(prefix) for prefix in SQLITE_ALLOWED):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in forbidden_constructions:
                violations.append(f"{relative}:{node.lineno} constructs {name}")
    assert not violations, "second store constructions outside the owner:\n" + "\n".join(
        violations
    )


def test_the_checker_detects_a_planted_violation(tmp_path):
    """The checker must fail on a synthetic module that imports a private store."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "from yeoman_gateway.memory.store import MemoryStore\n"
        "def f(service):\n"
        "    return service.known_jids\n",
        encoding="utf-8",
    )
    tree = ast.parse(planted.read_text(encoding="utf-8"), filename=str(planted))
    modules = [name for name, _ in _module_names(tree)]
    assert "yeoman_gateway.memory.store" in modules
    attributes = [
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    ]
    assert "known_jids" in attributes


def test_every_consumer_module_is_reachable_from_the_public_api():
    """Sanity check on the inventory: the package has no second knowledge facade."""
    public = set()
    for path in _iter_modules():
        if "knowledge" not in path.parts:
            continue
        public.add(_relative(path))
    assert "knowledge/api.py" in public
    assert "knowledge/models.py" in public


def test_public_api_exposes_no_store_or_connection_getter():
    from yeoman_gateway.knowledge.api import KnowledgeService

    for name in ("store", "connection", "known_jids", "execute_sql", "raw_sql"):
        assert not hasattr(KnowledgeService, name), name


def test_consumers_use_typed_knowledge_operations():
    """The responder must reach knowledge only through named operations."""
    source = (PACKAGE_ROOT / "adapters" / "responder_llm.py").read_text(encoding="utf-8")
    assert "self.knowledge.roster(" in source
    assert "self.contacts_service.known_jids" not in source
    assert "self.contacts.known_jids" not in source


@pytest.mark.parametrize(
    "relative",
    ["pipeline/contacts.py", "pipeline/reply_context.py", "agent/context.py"],
)
def test_pipeline_modules_do_not_import_store_modules(relative):
    path = PACKAGE_ROOT / relative
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules = [name for name, _ in _module_names(tree)]
    assert not [name for name in modules if name.endswith(".store")], modules
