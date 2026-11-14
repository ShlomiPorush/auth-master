"""The bundled docker-compose.yml passes FORWARDED_ALLOW_IPS and COOKIE_SECURE to the
auth-service container, so values set as stack variables (for example in Portainer)
reach it, not only values in .env.

Docker Compose resolves the file (`docker compose config`), so the test checks what
Compose does, not a reading of the YAML. Needs the Docker CLI with the compose plugin;
without it the test is skipped, except in CI, where it fails.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "docker-compose.yml"
SECRET_NAMES = ("REDIS_PASSWORD", "ADMIN_API_KEY", "SESSION_SECRET", "APP_ENCRYPTION_KEY")
# What the Docker CLI needs to find itself, its plugins and its context, on Linux,
# macOS and Windows. Nothing else is inherited from the caller.
INHERITED = {
    "PATH", "PATHEXT", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "SYSTEMROOT", "SYSTEMDRIVE",
    "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME",
    "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY",
}


def _docker_env() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if name.upper() in INHERITED}


def _compose_available() -> None:
    try:
        ok = shutil.which("docker") is not None and subprocess.run(
            ["docker", "compose", "version"], env=_docker_env(), capture_output=True, text=True, timeout=60
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    if not ok:
        if os.environ.get("CI"):
            pytest.fail("docker compose is not available; CI must run this test")
        pytest.skip("needs the Docker CLI with the compose plugin")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    _compose_available()
    shutil.copy(COMPOSE, tmp_path / "docker-compose.yml")
    (tmp_path / ".env").write_text("", encoding="utf-8")
    return tmp_path


def resolved_env(project: Path, extra: dict[str, str] | None = None, *args: str) -> dict[str, str]:
    """The auth-service environment as Docker Compose resolves it, with fresh random
    values for the required secrets. The secrets never appear in a failure report:
    every failure is reported by a redacted message without a traceback."""
    fresh = {name: secrets.token_hex(32) for name in SECRET_NAMES}
    problem = None
    try:
        result = subprocess.run(
            ["docker", "compose", *args, "config", "--format", "json"],
            cwd=project, env={**_docker_env(), **fresh, **(extra or {})},
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            problem = f"docker compose config failed ({result.returncode}): {result.stderr}"
        else:
            environment = json.loads(result.stdout)["services"]["auth-service"]["environment"]
            return {name: environment.get(name) for name in ("FORWARDED_ALLOW_IPS", "COOKIE_SECURE")}
    except Exception as error:  # noqa: BLE001 - reported below without its traceback
        problem = f"docker compose config could not be read: {type(error).__name__}: {error}"
    for value in fresh.values():
        problem = problem.replace(value, "<redacted>")
    fresh.clear()
    pytest.fail(problem, pytrace=False)


def test_defaults_when_not_set(project):
    # uvicorn 0.54's own default for FORWARDED_ALLOW_IPS, and the app's for COOKIE_SECURE
    assert resolved_env(project) == {"FORWARDED_ALLOW_IPS": "127.0.0.1,::1", "COOKIE_SECURE": "false"}


def test_stack_variables_reach_the_container(project):
    # A stack-manager GUI sets these only as variables for the compose file
    got = resolved_env(project, {"FORWARDED_ALLOW_IPS": "172.18.0.0/16", "COOKIE_SECURE": "true"})
    assert got == {"FORWARDED_ALLOW_IPS": "172.18.0.0/16", "COOKIE_SECURE": "true"}


def test_empty_values_are_passed_as_they_are(project):
    # Empty FORWARDED_ALLOW_IPS means "trust no proxy" to uvicorn; empty COOKIE_SECURE
    # is refused by the app's settings. Neither turns into the default.
    got = resolved_env(project, {"FORWARDED_ALLOW_IPS": "", "COOKIE_SECURE": ""})
    assert got == {"FORWARDED_ALLOW_IPS": "", "COOKIE_SECURE": ""}


def test_values_in_project_env_file_are_used(project):
    (project / ".env").write_text("FORWARDED_ALLOW_IPS=172.18.0.0/16\nCOOKIE_SECURE=true\n", encoding="utf-8")
    assert resolved_env(project) == {"FORWARDED_ALLOW_IPS": "172.18.0.0/16", "COOKIE_SECURE": "true"}


def test_other_env_file_replaces_project_env_file(project):
    # Documented precedence: with --env-file, Compose does not read the project's .env
    # for these values, and the passed default wins over env_file: .env.
    (project / ".env").write_text("FORWARDED_ALLOW_IPS=172.18.0.0/16\nCOOKIE_SECURE=true\n", encoding="utf-8")
    (project / "other.env").write_text("TZ=UTC\n", encoding="utf-8")
    got = resolved_env(project, None, "--env-file", "other.env")
    assert got == {"FORWARDED_ALLOW_IPS": "127.0.0.1,::1", "COOKIE_SECURE": "false"}
