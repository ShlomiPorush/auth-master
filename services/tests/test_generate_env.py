"""tools/generate-env.sh creates .env once, privately, and never replaces anything.

Runs on Linux or macOS (needs sh); the git cases also need git. Requires pytest.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools" / "generate-env.sh"
NAMES = ("ADMIN_API_KEY", "SESSION_SECRET", "APP_ENCRYPTION_KEY", "REDIS_PASSWORD")

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("sh") is None, reason="needs a POSIX sh"
)
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def run(cwd: Path, *args: str, path_prefix: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env['PATH']}"
    return subprocess.run(["sh", str(SCRIPT), *args], cwd=cwd, env=env, capture_output=True, text=True)


def leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.startswith(".env."))


def git_init(directory: Path, ignore: bool) -> None:
    subprocess.run(["git", "init", "-q", str(directory)], check=True)
    if ignore:
        shutil.copy(REPO / ".gitignore", directory / ".gitignore")


def shim(directory: Path, body: str) -> Path:
    """A fake ``ln`` placed first on PATH; it runs ``body`` and then the real ln."""
    real_ln = shutil.which("ln")
    bin_dir = directory.parent / f"{directory.name}-bin"
    bin_dir.mkdir()
    fake = bin_dir / "ln"
    fake.write_text(f"#!/bin/sh\n{body}\nexec {real_ln} \"$@\"\n")
    fake.chmod(0o755)
    return bin_dir


def test_creates_private_env_with_four_fresh_values(tmp_path):
    result = run(tmp_path)
    assert result.returncode == 0, result.stderr
    env_file = tmp_path / ".env"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    pairs = dict(
        line.split("=", 1) for line in env_file.read_text().splitlines() if line and not line.startswith("#")
    )
    assert sorted(pairs) == sorted(NAMES)
    assert all(re.fullmatch(r"[0-9a-f]{64}", v) for v in pairs.values())
    assert len(set(pairs.values())) == 4
    assert leftovers(tmp_path) == []
    for value in pairs.values():
        assert value not in result.stdout + result.stderr


def test_second_run_changes_nothing(tmp_path):
    assert run(tmp_path).returncode == 0
    before = (tmp_path / ".env").read_bytes()
    result = run(tmp_path)
    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert (tmp_path / ".env").read_bytes() == before
    assert leftovers(tmp_path) == []


@pytest.mark.parametrize("kind", ["file", "symlink", "dangling", "fifo", "directory", "dirlink"])
def test_refuses_anything_already_at_the_target(tmp_path, kind):
    target = tmp_path / ".env"
    other = tmp_path / "other"
    if kind == "file":
        target.write_text("KEEP=1\n")
    elif kind == "symlink":
        other.write_text("KEEP=1\n")
        target.symlink_to(other)
    elif kind == "dangling":
        target.symlink_to(tmp_path / "missing")
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind == "directory":
        target.mkdir()
    elif kind == "dirlink":
        other.mkdir()
        target.symlink_to(other)
    result = run(tmp_path)
    assert result.returncode != 0
    assert "already exists" in result.stderr
    if kind in ("file", "symlink"):
        assert target.read_text() == "KEEP=1\n"
    if kind == "dangling":
        assert not (tmp_path / "missing").exists()
    if kind in ("directory", "dirlink"):
        assert list(target.iterdir()) == []
    assert leftovers(tmp_path) == []


def test_refuses_arguments(tmp_path):
    result = run(tmp_path, ".env.custom")
    assert result.returncode != 0
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / ".env.custom").exists()


def test_directory_appearing_during_the_run_is_detected(tmp_path):
    bin_dir = shim(tmp_path, 'mkdir "$2"')
    result = run(tmp_path, path_prefix=bin_dir)
    assert result.returncode != 0
    assert (tmp_path / ".env").is_dir()
    assert list((tmp_path / ".env").iterdir()) == []
    assert leftovers(tmp_path) == []


def test_parallel_runs_create_exactly_one_file(tmp_path):
    procs = [
        subprocess.Popen(["sh", str(SCRIPT)], cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(8)
    ]
    codes = [p.wait() for p in procs]
    for p in procs:
        p.stdout.close()
        p.stderr.close()
    assert codes.count(0) == 1
    assert (tmp_path / ".env").is_file()
    assert leftovers(tmp_path) == []


@needs_git
def test_refuses_inside_a_git_tree_that_does_not_ignore_env(tmp_path):
    git_init(tmp_path, ignore=False)
    result = run(tmp_path)
    assert result.returncode != 0
    assert "Git does not ignore" in result.stderr
    assert not (tmp_path / ".env").exists()


@needs_git
def test_refuses_when_the_temporary_file_would_not_be_ignored(tmp_path):
    git_init(tmp_path, ignore=False)
    # .env and other .env.* names are ignored, but the six-character names mktemp
    # produces are excluded again.
    (tmp_path / ".gitignore").write_text(".env\n.env.*\n!.env.??????\n")
    result = run(tmp_path)
    assert result.returncode != 0
    assert "Git does not ignore" in result.stderr
    assert not (tmp_path / ".env").exists()
    assert leftovers(tmp_path) == []


@needs_git
def test_works_inside_a_git_tree_with_this_repositorys_ignore_rules(tmp_path):
    git_init(tmp_path, ignore=True)
    assert run(tmp_path).returncode == 0
    status = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True)
    assert ".env" not in status.stdout.replace(".gitignore", "")


@needs_git
def test_file_left_by_a_killed_run_is_private_and_ignored(tmp_path):
    git_init(tmp_path, ignore=True)
    bin_dir = shim(tmp_path, "kill -9 $PPID; exit 1")
    result = run(tmp_path, path_prefix=bin_dir)
    assert result.returncode != 0
    assert not (tmp_path / ".env").exists()
    left = leftovers(tmp_path)
    assert len(left) == 1
    leftover = tmp_path / left[0]
    assert stat.S_IMODE(leftover.stat().st_mode) == 0o600
    ignored = subprocess.run(["git", "check-ignore", "-q", left[0]], cwd=tmp_path)
    assert ignored.returncode == 0
