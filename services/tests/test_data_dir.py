"""docker/data_dir.py, which the container entrypoint runs before the service.

The tests run as the current user and give the data directory to that same user,
so they check which entries the walk changes (the owner bits it adds) rather
than the owner itself; .github/image-check.sh checks real ownership changes in
the built image. Linux only (O_PATH and /proc/self/fd).
"""
from __future__ import annotations

import importlib.util
import os
import stat
import sys
from pathlib import Path

import pytest

if not sys.platform.startswith("linux"):
    pytest.skip("Linux only", allow_module_level=True)

_SPEC = importlib.util.spec_from_file_location(
    "data_dir", Path(__file__).resolve().parents[2] / "docker" / "data_dir.py"
)
data_dir = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(data_dir)

UID, GID = os.getuid(), os.getgid()


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def take(top: Path) -> None:
    data_dir.take_ownership(str(top), UID, GID)


def test_adds_owner_bits_to_directories_and_files(tmp_path):
    top = tmp_path / "data"
    (top / "sub").mkdir(parents=True)
    (top / "auth.db").write_text("x")
    (top / "sub" / "f").write_text("y")
    os.chmod(top / "auth.db", 0o444)
    os.chmod(top / "sub" / "f", 0o440)
    os.chmod(top / "sub", 0o555)
    take(top)
    assert mode(top / "auth.db") == 0o644
    assert mode(top / "sub") == 0o755
    assert mode(top / "sub" / "f") == 0o640


def test_creates_the_directory(tmp_path):
    take(tmp_path / "new" / "data")
    assert (tmp_path / "new" / "data").is_dir()


def test_never_changes_symlinks_hard_links_or_fifos(tmp_path):
    top = tmp_path / "data"
    top.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "target"
    target.write_text("t")
    os.chmod(target, 0o000)
    os.symlink(target, top / "link")
    os.symlink(outside, top / "dirlink")
    linked = top / "linked"
    linked.write_text("l")
    os.link(linked, outside / "other-name")
    os.chmod(linked, 0o000)
    os.mkfifo(top / "fifo", 0o000)
    take(top)
    assert mode(target) == 0o000
    assert mode(linked) == 0o000
    assert mode(top / "fifo") == 0o000


def test_a_directory_swapped_for_a_symlink_after_its_test_is_not_followed(tmp_path, monkeypatch):
    top = tmp_path / "data"
    (top / "sub").mkdir(parents=True)
    (top / "sub" / "inside").write_text("i")
    os.chmod(top / "sub" / "inside", 0o400)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "inside").write_text("o")
    os.chmod(outside / "inside", 0o000)
    real = data_dir._open_tested

    def swap_after_test(dir_fd, name):
        opened = real(dir_fd, name)
        if name == "sub":
            os.rename(top / "sub", top / "sub.moved")
            os.symlink(outside, top / "sub")
        return opened

    monkeypatch.setattr(data_dir, "_open_tested", swap_after_test)
    take(top)
    assert mode(outside / "inside") == 0o000
    assert mode(top / "sub.moved" / "inside") == 0o600


def test_a_name_swapped_before_its_test_is_never_opened_or_changed(tmp_path, monkeypatch):
    top = tmp_path / "data"
    (top / "sub").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    fifo = outside / "fifo"
    os.mkfifo(fifo, 0o000)
    real = data_dir._open_tested

    def swap_before_test(dir_fd, name):
        if name == "sub":
            os.rename(top / "sub", top / "sub.moved")
            os.symlink(fifo, top / "sub")
        return real(dir_fd, name)

    monkeypatch.setattr(data_dir, "_open_tested", swap_before_test)
    take(top)
    assert mode(fifo) == 0o000
    assert os.path.islink(top / "sub")


def test_a_file_swapped_for_a_hard_link_before_its_test_is_not_changed(tmp_path, monkeypatch):
    top = tmp_path / "data"
    top.mkdir()
    (top / "auth.db").write_text("x")
    outside = tmp_path / "outside"
    outside.mkdir()
    shared = outside / "shared"
    shared.write_text("s")
    os.chmod(shared, 0o000)
    real = data_dir._open_tested

    def swap_before_test(dir_fd, name):
        if name == "auth.db":
            os.link(shared, top / "auth.db.new")
            os.replace(top / "auth.db.new", top / "auth.db")
        return real(dir_fd, name)

    monkeypatch.setattr(data_dir, "_open_tested", swap_before_test)
    take(top)
    assert mode(shared) == 0o000


def test_vanished_entries_are_skipped(tmp_path, monkeypatch):
    top = tmp_path / "data"
    top.mkdir()
    (top / "gone").write_text("g")
    real = data_dir._open_tested

    def remove_first(dir_fd, name):
        if name == "gone":
            os.unlink(top / "gone")
        return real(dir_fd, name)

    monkeypatch.setattr(data_dir, "_open_tested", remove_first)
    take(top)


@pytest.mark.skipif(os.geteuid() == 0, reason="root passes every access check")
def test_check_lists_what_the_user_cannot_read_or_write(tmp_path):
    top = tmp_path / "data"
    (top / "sub").mkdir(parents=True)
    (top / "ok").write_text("o")
    (top / "write-only").write_text("w")
    os.chmod(top / "write-only", 0o200)
    os.chmod(top / "sub", 0o500)
    os.symlink("/nonexistent/target", top / "dangling")
    try:
        problems = data_dir.check(str(top))
    finally:
        os.chmod(top / "sub", 0o700)
    assert any(p.startswith(f"{top / 'write-only'}:") for p in problems)
    assert any(p.startswith(f"{top / 'sub'}:") for p in problems)
    assert not any(p.startswith((f"{top / 'ok'}:", f"{top / 'dangling'}:")) for p in problems)
    assert not any(n.startswith(".write-check-") for n in os.listdir(top))


@pytest.mark.skipif(os.geteuid() == 0, reason="root can search every directory")
def test_check_lists_a_symlink_whose_target_cannot_be_reached(tmp_path):
    top = tmp_path / "data"
    top.mkdir()
    closed = tmp_path / "closed"
    closed.mkdir()
    (closed / "auth.db").write_text("x")
    os.symlink(closed / "auth.db", top / "auth.db")
    os.chmod(closed, 0o600)
    try:
        problems = data_dir.check(str(top))
    finally:
        os.chmod(closed, 0o700)
    assert any(p.startswith(f"{top / 'auth.db'}: cannot reach it") for p in problems)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_check_fails_when_no_file_can_be_created(tmp_path):
    top = tmp_path / "data"
    top.mkdir()
    os.chmod(top, 0o500)
    try:
        problems = data_dir.check(str(top))
    finally:
        os.chmod(top, 0o700)
    assert any("cannot create a file" in p for p in problems)


def test_check_passes_on_a_usable_directory(tmp_path):
    top = tmp_path / "data"
    (top / "sub").mkdir(parents=True)
    (top / "auth.db").write_text("x")
    assert data_dir.check(str(top)) == []
    assert sorted(os.listdir(top)) == ["auth.db", "sub"]
