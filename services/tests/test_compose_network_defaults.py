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
CONTROLLED = ("FORWARDED_ALLOW_IPS", "COOKIE_SECURE", *SECRET_NAMES)


def _base_env() -> tuple[dict[str, str], list[str]]:
    """The inherited environment (Docker finds its plugins through it), without the
    variables this test controls, plus fresh random values for the required secrets."""
    env = {
        name: value for name, value in os.environ.items()
        if name not in CONTROLLED and not name.startswith("COMPOSE_")
    }
    fresh = [secrets.token_hex(32) for _ in SECRET_NAMES]
    env.update(zip(SECRET_NAMES, fresh))
    return env, fresh


def _compose_available() -> None:
    env, _ = _base_env()
    try:
        ok = shutil.which("docker") is not None and subprocess.run(
            ["docker", "compose", "version"], env=env, capture_output=True, text=True, timeout=60
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
    """The auth-service environment as Docker Compose resolves it."""
    env, fresh = _base_env()
    env.update(extra or {})
    result = subprocess.run(
        ["docker", "compose", *args, "config", "--format", "json"],
        cwd=project, env=env, capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        message = result.stderr
        for value in fresh:
            message = message.replace(value, "<redacted>")
        pytest.fail(f"docker compose config failed ({result.returncode}): {message}")
    environment = json.loads(result.stdout)["services"]["auth-service"]["environment"]
    return {name: environment.get(name) for name in ("FORWARDED_ALLOW_IPS", "COOKIE_SECURE")}


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
