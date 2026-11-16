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


# ---------------------------------------------------------------------------
# A SQLite file that DATABASE_URL names (take_database, check_database)
# ---------------------------------------------------------------------------

OTHER_UID, OTHER_GID = UID + 1, GID + 1  # a target owner that differs from the current one
as_root = pytest.mark.skipif(os.geteuid() != 0, reason="changing the owner needs root")
not_root = pytest.mark.skipif(os.geteuid() == 0, reason="root passes every access check")


@pytest.fixture
def umask_077():
    old = os.umask(0o077)
    yield
    os.umask(old)


@pytest.mark.parametrize("url", [
    "sqlite:///data/auth.db",
    "sqlite:///sub/dir/auth.db",
    "sqlite:////abs/dir/auth.db",
    "sqlite:",
    "sqlite:///data/auth.db?mode=ro",
    "sqlite:///a/../b/auth.db",
])
def test_the_path_is_the_one_the_service_opens(url, tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import aiosqlite
    from app import db as app_db

    opened = []

    async def connect(path, **kwargs):
        opened.append(path)
        raise RuntimeError("stop")

    monkeypatch.setattr(aiosqlite, "connect", connect)
    monkeypatch.setattr(Path, "mkdir", lambda *a, **k: None)
    monkeypatch.chdir(tmp_path)
    import asyncio
    with pytest.raises(RuntimeError):
        asyncio.run(app_db.create_database(url))
    assert data_dir.sqlite_path(url, str(tmp_path)) == os.path.join(str(tmp_path), opened[0])


def test_only_sqlite_urls_name_a_file():
    assert data_dir.sqlite_path(None, "/app") is None
    assert data_dir.sqlite_path("postgres://h/db", "/app") is None
    assert data_dir.sqlite_path("sqlite:///x/auth.db", "/w") == "/w/x/auth.db"


@pytest.mark.parametrize("path,reason", [
    ("/auth.db", "system directory"),
    ("/etc/auth/auth.db", "system directory"),
    ("/usr/local/lib/auth.db", "system directory"),
    ("/app/auth.db", "image's own files"),
    ("/app/static/public/images/favicon.svg", "image's own files"),
    ("/db/../etc/auth.db", "'..'"),
    ("/db//auth.db", "empty"),
    ("/db/./auth.db", "'.'"),
])
def test_directories_that_are_never_given_away(path, reason):
    with pytest.raises(data_dir.Refused, match=reason):
        data_dir._database_target(path)


def test_the_data_directory_and_var_are_allowed():
    assert data_dir._database_target("/app/data/sub/auth.db")[2] == "/app/data/sub"
    assert data_dir._database_target("/var/lib/auth-master/auth.db")[2] == "/var/lib/auth-master"


def test_missing_directories_are_created_searchable_under_a_strict_umask(tmp_path, umask_077):
    db = tmp_path / "new" / "sub" / "auth.db"
    data_dir.take_database(str(db), UID, GID)
    assert mode(tmp_path / "new") == 0o755
    assert mode(tmp_path / "new" / "sub") == 0o755
    assert os.umask(0o077) == 0o077  # restored


def test_a_directory_already_owned_gets_its_owner_bits_back(tmp_path):
    # An earlier run stopped between fchown and fchmod.
    d = tmp_path / "db"
    d.mkdir()
    (d / "auth.db").write_text("x")
    (d / "auth.db.bak").write_text("b")  # accepted: the directory is already the service's
    os.chmod(d / "auth.db", 0o400)
    os.chmod(d, 0o500)
    try:
        data_dir.take_database(str(d / "auth.db"), UID, GID)
        assert mode(d) == 0o700
        assert mode(d / "auth.db") == 0o600
    finally:
        os.chmod(d, 0o700)


def test_a_directory_named_like_the_database_is_not_changed(tmp_path):
    d = tmp_path / "db"
    (d / "auth.db").mkdir(parents=True)
    os.chmod(d / "auth.db", 0o500)
    try:
        data_dir.take_database(str(d / "auth.db"), UID, GID)
        assert mode(d / "auth.db") == 0o500
    finally:
        os.chmod(d / "auth.db", 0o700)


def test_a_symlinked_or_hard_linked_database_is_not_changed(tmp_path):
    d = tmp_path / "db"
    d.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "real.db").write_text("r")
    os.chmod(outside / "real.db", 0o400)
    os.symlink(outside / "real.db", d / "auth.db")
    (outside / "wal").write_text("w")
    os.link(outside / "wal", d / "auth.db-wal")
    os.chmod(outside / "wal", 0o400)
    data_dir.take_database(str(d / "auth.db"), UID, GID)
    assert mode(outside / "real.db") == 0o400
    assert mode(outside / "wal") == 0o400


def test_a_symlink_in_the_path_is_refused(tmp_path):
    (tmp_path / "real").mkdir()
    os.symlink(tmp_path / "real", tmp_path / "link")
    with pytest.raises(data_dir.Refused, match="is a symlink"):
        data_dir.take_database(str(tmp_path / "link" / "auth.db"), UID, GID)


@pytest.mark.parametrize("extra", ["notes.txt", "auth.db-passwords", "auth.db.bak"])
def test_a_directory_with_other_files_is_refused_and_unchanged(tmp_path, extra):
    d = tmp_path / "db"
    d.mkdir()
    (d / "auth.db").write_text("x")
    (d / extra).write_text("e")
    os.chmod(d, 0o500)
    try:
        with pytest.raises(data_dir.Refused, match=extra.replace(".", r"\.")):
            data_dir.take_database(str(d / "auth.db"), OTHER_UID, OTHER_GID)
        assert mode(d) == 0o500
        assert os.stat(d).st_uid == UID
    finally:
        os.chmod(d, 0o700)


@pytest.mark.parametrize("kind", ["directory", "symlink", "hard link"])
def test_a_directory_whose_database_names_are_not_plain_files_is_refused_and_unchanged(tmp_path, kind):
    d = tmp_path / "db"
    d.mkdir()
    (d / "auth.db").write_text("x")
    if kind == "directory":
        (d / "auth.db-wal").mkdir()
    elif kind == "symlink":
        (tmp_path / "elsewhere").write_text("e")
        os.symlink(tmp_path / "elsewhere", d / "auth.db-wal")
    else:
        os.link(d / "auth.db", tmp_path / "second-name")
    os.chmod(d, 0o500)
    try:
        with pytest.raises(data_dir.Refused, match="not a regular file with one link"):
            data_dir.take_database(str(d / "auth.db"), OTHER_UID, OTHER_GID)
        assert mode(d) == 0o500
    finally:
        os.chmod(d, 0o700)


@as_root
def test_a_database_directory_is_given_to_the_service_user(tmp_path):
    d = tmp_path / "db"
    d.mkdir()
    for name in ("auth.db", "auth.db-wal", "auth.db-shm"):
        (d / name).write_text("x")
    data_dir.take_database(str(d / "auth.db"), 10001, 10001)
    for p in (d, d / "auth.db", d / "auth.db-wal", d / "auth.db-shm"):
        assert (os.stat(p).st_uid, os.stat(p).st_gid) == (10001, 10001)
    assert os.stat(tmp_path).st_uid == 0  # parents are never changed


@as_root
def test_an_entry_that_appears_during_the_change_restores_the_owner(tmp_path, monkeypatch):
    d = tmp_path / "db"
    d.mkdir()
    (d / "auth.db").write_text("x")
    os.chmod(d, 0o750)
    real = data_dir._other_entries
    calls = []

    def appearing(dir_fd, name):
        calls.append(1)
        if len(calls) == 2:
            (d / "intruder").write_text("i")
        return real(dir_fd, name)

    monkeypatch.setattr(data_dir, "_other_entries", appearing)
    with pytest.raises(data_dir.Refused, match="intruder appeared"):
        data_dir.take_database(str(d / "auth.db"), 10001, 10001)
    assert (os.stat(d).st_uid, mode(d)) == (0, 0o750)


@as_root
def test_a_failed_restore_says_the_directory_is_still_the_service_users(tmp_path, monkeypatch):
    d = tmp_path / "db"
    d.mkdir()
    (d / "auth.db").write_text("x")
    real_entries, real_fchown = data_dir._other_entries, os.fchown
    calls = []

    def appearing(dir_fd, name):
        calls.append(1)
        if len(calls) == 2:
            (d / "intruder").write_text("i")
        return real_entries(dir_fd, name)

    def fchown(fd, uid, gid):
        if uid == 0:
            raise OSError(5, "Input/output error")
        return real_fchown(fd, uid, gid)

    monkeypatch.setattr(data_dir, "_other_entries", appearing)
    monkeypatch.setattr(data_dir.os, "fchown", fchown)
    with pytest.raises(data_dir.Problem, match="still owned by uid 10001"):
        data_dir.take_database(str(d / "auth.db"), 10001, 10001)


def test_check_accepts_missing_directories_the_service_can_create(tmp_path):
    assert data_dir.check_database(str(tmp_path / "a" / "b" / "auth.db")) == []


@not_root
def test_check_names_a_parent_the_service_cannot_pass(tmp_path):
    d = tmp_path / "closed" / "db"
    d.mkdir(parents=True)
    os.chmod(tmp_path / "closed", 0o600)
    try:
        problems = data_dir.check_database(str(d / "auth.db"))
    finally:
        os.chmod(tmp_path / "closed", 0o700)
    assert problems == [f"{tmp_path / 'closed'}: cannot pass this directory to reach {d}"]


@not_root
def test_check_names_a_database_file_it_cannot_read(tmp_path):
    (tmp_path / "auth.db").write_text("x")
    os.chmod(tmp_path / "auth.db", 0o200)
    problems = data_dir.check_database(str(tmp_path / "auth.db"))
    assert problems == [f"{tmp_path / 'auth.db'}: cannot read and write the file"]
