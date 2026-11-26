from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / ".github" / "rollout-from.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash"
)


def rollout_from(version: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), version], capture_output=True, text=True)


def configured_floor() -> str:
    match = re.search(r"^ROLLOUT_FROM=(\S+)$", SCRIPT.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, "rollout-from.sh sets ROLLOUT_FROM"
    return match.group(1)


def bumped(version: str, part: int) -> str:
    numbers = [int(n) for n in version.split(".")]
    numbers[part] += 1
    numbers[part + 1:] = [0] * (2 - part)
    return ".".join(str(n) for n in numbers)


def test_the_floor_itself_and_newer_versions_get_the_floor():
    floor = configured_floor()
    for version in (floor, bumped(floor, 2), bumped(floor, 1), bumped(floor, 0), floor.replace(".", "1.", 1)):
        result = rollout_from(version)
        assert result.returncode == 0, (version, result.stderr)
        assert result.stdout == f"{floor}\n"


def test_a_version_older_than_the_floor_is_refused():
    result = rollout_from("0.0.1")
    assert result.returncode != 0
    assert "newer than the version" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("version", ["v2.5.0", "2.5", "2.6.0-rc.1", "2.5.01", "02.5.0", "", "2.5.0 "])
def test_a_version_that_is_not_a_plain_semver_core_is_refused(version):
    result = rollout_from(version)
    assert result.returncode != 0
    assert result.stdout == ""
