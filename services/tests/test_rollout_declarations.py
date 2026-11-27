from __future__ import annotations

import ast
import hashlib
import inspect
import re
import sys
from pathlib import Path

import pytest

SERVICES = Path(__file__).resolve().parents[1]
REPO = SERVICES.parent
APP = SERVICES / "app"
DECLARATIONS = REPO / "docker" / "rollout.txt"
sys.path.insert(0, str(SERVICES))

SCHEMA_SOURCE_FILES = ("schema.py", "installation.py")
SINGLE_PROCESS_DDL_FILES = {"db_move.py", "db_operations.py"}
DDL = re.compile(r"\b(CREATE|ALTER|DROP)\s+(UNIQUE\s+)?(TABLE|INDEX|EXTENSION|TRIGGER|VIEW)\b")
LINE = re.compile(r"^(schema [0-9a-f]{12}|change [a-z0-9][a-z0-9-]*)$")


def schema_source_parts() -> list[tuple[str, str]]:
    from app import grants, zones

    parts = [(name, (APP / name).read_text(encoding="utf-8")) for name in SCHEMA_SOURCE_FILES]
    for function in (zones.ensure_zone_name_index, zones.duplicate_zone_groups, zones._name_fold_sql, grants.fold_area):
        parts.append((f"{function.__module__}.{function.__qualname__}", inspect.getsource(function)))
    return parts


def fingerprint(parts: list[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for name, text in parts:
        digest.update(name.encode() + b"\0" + text.replace("\r\n", "\n").encode() + b"\0")
    return digest.hexdigest()[:12]


def declarations(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def problems(lines: list[str], current: str) -> list[str]:
    found = [f"malformed line: {line!r}" for line in lines if not LINE.match(line)]
    schema_lines = [line for line in lines if line.startswith("schema ")]
    if len(schema_lines) != 1:
        found.append(f"expected exactly one schema line, found {len(schema_lines)}")
    elif schema_lines[0] != f"schema {current}":
        found.append(
            f"the start-up schema code changed: replace the schema line with 'schema {current}'. "
            "The release that carries it is deployed by hand (docs/deploy.md)."
        )
    changes = [line for line in lines if line.startswith("change ")]
    if len(changes) != len(set(changes)):
        found.append("a change name is declared twice")
    return found


def string_constants_outside_docstrings(tree: ast.AST) -> list[ast.Constant]:
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]


def test_the_declarations_name_the_current_start_up_schema():
    assert problems(declarations(DECLARATIONS.read_text(encoding="utf-8")), fingerprint(schema_source_parts())) == []


def test_schema_statements_live_only_in_the_fingerprinted_or_single_process_modules():
    allowed = set(SCHEMA_SOURCE_FILES) | SINGLE_PROCESS_DDL_FILES | {"zones.py"}
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        if path.name in allowed and path.parent == APP:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in string_constants_outside_docstrings(tree):
            if DDL.search(node.value):
                offenders.append(f"{path.relative_to(SERVICES)}:{node.lineno}")
    assert offenders == []


def test_ddl_in_zones_is_only_in_the_fingerprinted_functions():
    from app import zones

    fingerprinted = {zones.ensure_zone_name_index.__name__, zones.duplicate_zone_groups.__name__, zones._name_fold_sql.__name__}
    tree = ast.parse((APP / "zones.py").read_text(encoding="utf-8"))
    offenders = []
    for function in ast.walk(tree):
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) and function.name not in fingerprinted:
            for node in string_constants_outside_docstrings(function):
                if DDL.search(node.value):
                    offenders.append(f"zones.{function.name}:{node.lineno}")
    assert offenders == []


BASE = [("schema.py", "CREATE TABLE IF NOT EXISTS tokens (id TEXT)\n")]


@pytest.mark.parametrize(
    "edit",
    [
        "ALTER TABLE tokens ADD COLUMN IF NOT EXISTS note TEXT\n",
        "CREATE INDEX IF NOT EXISTS idx_note ON tokens (id)\n",
        "CREATE TABLE IF NOT EXISTS notes (id TEXT)\n",
        "# a comment\n",
    ],
)
def test_any_edit_of_the_schema_source_needs_a_new_schema_line(edit):
    before = fingerprint(BASE)
    after = fingerprint([("schema.py", BASE[0][1] + edit)])
    assert after != before
    assert problems([f"schema {before}"], after) != []
    assert problems([f"schema {after}"], after) == []


def test_line_endings_do_not_change_the_fingerprint():
    assert fingerprint([("schema.py", "a\r\nb\r\n")]) == fingerprint([("schema.py", "a\nb\n")])


@pytest.mark.parametrize(
    "lines",
    [
        [],
        ["schema 0123456789ab", "schema 0123456789ab"],
        ["schema 0123456789ab", "change session-format", "change session-format"],
        ["schema 0123456789ab", "breaking session-format"],
        ["schema 0123456789AB"],
        ["schema 0123456789ab", "change Session"],
    ],
)
def test_malformed_declarations_are_refused(lines):
    assert problems(lines, "0123456789ab") != []


def test_a_new_ddl_string_outside_the_checked_modules_is_found():
    tree = ast.parse('"""CREATE TABLE in a docstring is fine."""\nSQL = "ALTER TABLE tokens ADD COLUMN x TEXT"\n')
    assert [n.value for n in string_constants_outside_docstrings(tree) if DDL.search(n.value)] == [
        "ALTER TABLE tokens ADD COLUMN x TEXT"
    ]
