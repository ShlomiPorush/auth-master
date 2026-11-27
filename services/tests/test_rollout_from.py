from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / ".github" / "rollout-from.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None or shutil.which("git") is None,
    reason="needs bash and git",
)


class Releases:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.git("init", "-q")
        (path / "docker").mkdir()

    def git(self, *args: str) -> None:
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
                       cwd=self.path, env=env, check=True, capture_output=True)

    def declare(self, *lines: str) -> None:
        (self.path / "docker" / "rollout.txt").write_text("# comment\n" + "".join(f"{line}\n" for line in lines))

    def release(self, version: str, *lines: str) -> None:
        self.declare(*lines)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", version)
        self.git("tag", f"v{version}")

    def plain_release(self, version: str) -> None:
        (self.path / "docker" / "rollout.txt").unlink(missing_ok=True)
        (self.path / "file").write_text(version)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", version)
        self.git("tag", f"v{version}")

    def rollout_from(self, version: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(SCRIPT), version], cwd=self.path, capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Releases:
    return Releases(tmp_path)


def resolves(repo: Releases, version: str, expected: str) -> None:
    result = repo.rollout_from(version)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{expected}\n"


def refused(repo: Releases, version: str, text: str) -> None:
    result = repo.rollout_from(version)
    assert result.returncode != 0
    assert result.stdout == ""
    assert text in result.stderr


def test_the_first_release_with_declarations_is_its_own_floor(repo):
    repo.plain_release("2.5.0")
    repo.declare("schema aaaaaaaaaaaa")
    resolves(repo, "2.6.0", "2.6.0")


def test_releases_with_the_same_declarations_keep_the_floor(repo):
    repo.plain_release("2.5.0")
    repo.release("2.6.0", "schema aaaaaaaaaaaa")
    repo.release("2.7.0", "schema aaaaaaaaaaaa")
    repo.declare("schema aaaaaaaaaaaa")
    resolves(repo, "2.8.0", "2.6.0")


def test_a_schema_change_starts_a_new_floor_that_later_releases_keep(repo):
    repo.release("2.6.0", "schema aaaaaaaaaaaa")
    repo.release("2.7.0", "schema bbbbbbbbbbbb")
    repo.declare("schema bbbbbbbbbbbb")
    resolves(repo, "2.8.0", "2.7.0")
    repo.declare("schema cccccccccccc")
    resolves(repo, "2.8.0", "2.8.0")


def test_a_revert_to_an_older_schema_is_a_schema_change(repo):
    repo.release("2.6.0", "schema aaaaaaaaaaaa")
    repo.release("2.7.0", "schema bbbbbbbbbbbb")
    repo.declare("schema aaaaaaaaaaaa")
    resolves(repo, "2.8.0", "2.8.0")


def test_a_new_change_line_starts_a_new_floor(repo):
    repo.release("2.6.0", "schema aaaaaaaaaaaa")
    repo.release("2.7.0", "schema aaaaaaaaaaaa", "change session-format")
    repo.declare("schema aaaaaaaaaaaa", "change session-format")
    resolves(repo, "2.8.0", "2.7.0")
    repo.declare("schema aaaaaaaaaaaa", "change session-format", "change data-files")
    resolves(repo, "2.8.0", "2.8.0")


def test_an_interleaved_tag_that_lacks_a_line_ends_the_run(repo):
    repo.release("2.6.0", "schema aaaaaaaaaaaa", "change session-format")
    repo.release("2.8.0", "schema aaaaaaaaaaaa", "change session-format")
    repo.release("2.7.0", "schema aaaaaaaaaaaa")
    repo.declare("schema aaaaaaaaaaaa", "change session-format")
    resolves(repo, "2.9.0", "2.8.0")


def test_a_removed_change_line_is_refused_also_when_a_later_tag_dropped_it(repo):
    repo.release("2.6.0", "schema aaaaaaaaaaaa", "change session-format")
    repo.release("2.7.0", "schema aaaaaaaaaaaa")
    repo.declare("schema aaaaaaaaaaaa")
    refused(repo, "2.8.0", "v2.6.0 declares a change line")


def test_a_backport_that_differs_from_a_newer_release_is_refused(repo):
    repo.release("2.6.0", "schema aaaaaaaaaaaa")
    repo.release("2.8.0", "schema aaaaaaaaaaaa")
    repo.declare("schema bbbbbbbbbbbb")
    refused(repo, "2.7.0", "v2.8.0 is newer")
    repo.declare("schema aaaaaaaaaaaa")
    resolves(repo, "2.7.0", "2.6.0")


def test_a_backport_that_adds_a_change_line_a_newer_release_lacks_is_refused(repo):
    repo.release("2.6.0", "schema aaaaaaaaaaaa")
    repo.release("2.8.0", "schema aaaaaaaaaaaa")
    repo.declare("schema aaaaaaaaaaaa", "change session-format")
    refused(repo, "2.7.0", "v2.8.0 is newer")


def test_tags_that_are_not_plain_versions_are_ignored(repo):
    repo.release("2.6.0", "schema aaaaaaaaaaaa")
    repo.release("2.7.0-rc.1", "schema bbbbbbbbbbbb")
    repo.declare("schema aaaaaaaaaaaa")
    resolves(repo, "2.7.0", "2.6.0")


@pytest.mark.parametrize("version", ["v2.5.0", "2.5", "2.6.0-rc.1", "2.5.01", "02.5.0", "", "2.5.0 "])
def test_a_version_that_is_not_a_plain_semver_core_is_refused(repo, version):
    repo.declare("schema aaaaaaaaaaaa")
    result = repo.rollout_from(version)
    assert result.returncode != 0
    assert result.stdout == ""


@pytest.mark.parametrize("lines", [(), ("schema aaaaaaaaaaaa", "schema bbbbbbbbbbbb")])
def test_a_file_without_exactly_one_schema_line_is_refused(repo, lines):
    repo.declare(*lines)
    refused(repo, "2.6.0", "exactly one schema line")
