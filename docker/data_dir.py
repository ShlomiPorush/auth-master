"""Prepare the data directory for the service user when the container starts.

docker-entrypoint.sh runs this file with ``python -I`` before the service, so no
application code is imported.

``take-ownership`` (run as root): gives uid and gid 10001 the data directory and
the directories and regular files in it, and adds the owner's read and write
bits (and search, on directories). It never changes a symlink or its target, a
regular file with more than one hard link (another link can be outside the data
directory), any other file type, or anything on another filesystem. Each entry
is opened without following a symlink, tested, and changed through that same
file descriptor, and the walk enters a directory only through the descriptor it
tested. So an entry that is renamed or replaced while the walk runs cannot
redirect a change outside the data directory. Entries that already have the
right owner and bits are not touched, so a restart finishes an interrupted run.

``check`` (run as the service user): the service can create a file in the data
directory, and can read and write every entry in it (and search every
directory). Otherwise it lists the paths and exits 1, before the service starts.

See "Data directory and file ownership" in README.md.
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile

DATA = "/app/data"
SERVICE_UID = 10001
SERVICE_GID = 10001
SHOWN = 10
HELP = 'See "Data directory and file ownership" in README.md.'


class Problem(Exception):
    pass


def _open_tested(dir_fd: int, name: str) -> tuple[int, os.stat_result] | None:
    """Open ``name`` in ``dir_fd`` without following a symlink, for fchown,
    fchmod and (directories) as the next directory of the walk.  None for an
    entry that is never changed.  The entry is first opened with O_PATH, which
    does no I/O on it, and only a directory or a single-link regular file is
    then reopened, through /proc/self/fd, so the reopened object is the tested
    one and a device or FIFO is never opened."""
    path_fd = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        tested = os.fstat(path_fd)
        if stat.S_ISDIR(tested.st_mode):
            flags = os.O_RDONLY | os.O_DIRECTORY
        elif stat.S_ISREG(tested.st_mode) and tested.st_nlink == 1:
            flags = os.O_RDONLY | os.O_NONBLOCK
        else:
            return None
        fd = os.open(f"/proc/self/fd/{path_fd}", flags | os.O_CLOEXEC)
    finally:
        os.close(path_fd)
    current = os.fstat(fd)
    if (current.st_dev, current.st_ino) != (tested.st_dev, tested.st_ino):
        os.close(fd)
        raise Problem(f"{name} changed while it was opened")
    return fd, current


def _take(fd: int, st: os.stat_result, uid: int, gid: int) -> None:
    if st.st_uid != uid or st.st_gid != gid:
        os.fchown(fd, uid, gid)
        st = os.fstat(fd)  # chown can clear the set-user-ID and set-group-ID bits
    wanted = stat.S_IRWXU if stat.S_ISDIR(st.st_mode) else stat.S_IRUSR | stat.S_IWUSR
    if st.st_mode & wanted != wanted:
        os.fchmod(fd, stat.S_IMODE(st.st_mode) | wanted)


def _walk(dir_fd: int, path: str, dev: int, uid: int, gid: int) -> None:
    for name in os.listdir(dir_fd):
        entry = os.path.join(path, name)
        try:
            opened = _open_tested(dir_fd, name)
        except FileNotFoundError:
            continue  # removed while the walk ran
        except (OSError, Problem) as e:
            raise Problem(f"{entry}: {getattr(e, 'strerror', None) or e}") from None
        if opened is None:
            continue
        fd, st = opened
        try:
            if st.st_dev != dev:
                continue  # another filesystem mounted inside the data directory
            try:
                _take(fd, st, uid, gid)
            except OSError as e:
                raise Problem(f"{entry}: {e.strerror}") from None
            if stat.S_ISDIR(st.st_mode):
                _walk(fd, entry, dev, uid, gid)
        finally:
            os.close(fd)


def take_ownership(top: str = DATA, uid: int = SERVICE_UID, gid: int = SERVICE_GID) -> None:
    try:
        os.makedirs(top, exist_ok=True)
        top_fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as e:
        raise Problem(f"{top}: {e.strerror}") from None
    try:
        st = os.fstat(top_fd)
        try:
            _take(top_fd, st, uid, gid)
        except OSError as e:
            raise Problem(f"{top}: {e.strerror}") from None
        _walk(top_fd, top, st.st_dev, uid, gid)
    finally:
        os.close(top_fd)


def check(top: str = DATA) -> list[str]:
    """The paths in ``top`` the current user cannot use, with the reason."""
    problems: list[str] = []
    try:
        fd, probe = tempfile.mkstemp(dir=top, prefix=".write-check-")
        os.close(fd)
        os.unlink(probe)
    except OSError as e:
        problems.append(f"{top}: cannot create a file there ({e.strerror})")
    if not os.access(top, os.R_OK | os.W_OK | os.X_OK):
        problems.append(f"{top}: cannot read, write or search the directory")

    def unreadable(e: OSError) -> None:
        problems.append(f"{e.filename}: cannot read the directory ({e.strerror})")

    for root, dirs, files in os.walk(top, onerror=unreadable):
        for name in dirs + files:
            path = os.path.join(root, name)
            try:
                st = os.stat(path)  # follows a symlink
            except FileNotFoundError:
                continue  # a dangling symlink, or removed meanwhile
            except OSError as e:
                problems.append(f"{path}: cannot reach it ({e.strerror})")
                continue
            if stat.S_ISDIR(st.st_mode):
                if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
                    problems.append(f"{path}: cannot read, write or search the directory")
            elif not os.access(path, os.R_OK | os.W_OK):
                problems.append(f"{path}: cannot read and write the file")
    return problems


def main(argv: list[str]) -> int:
    if argv == ["take-ownership"]:
        try:
            take_ownership()
        except Problem as e:
            print(
                f"Cannot give the service user (uid {SERVICE_UID}) the data directory: {e}. "
                f"A read-only mount or an NFS export with root_squash prevents this. Mount the "
                f"directory writable; on NFS, give uid {SERVICE_UID} the directory on the server "
                f'and start the container with user: "{SERVICE_UID}:{SERVICE_GID}". {HELP}',
                file=sys.stderr,
            )
            return 1
        return 0
    if argv == ["check"]:
        problems = check()
        if not problems:
            return 0
        lines = [f"The service user (uid {os.getuid()}) cannot use the data directory {DATA}:"]
        lines += [f"  - {p}" for p in problems[:SHOWN]]
        if len(problems) > SHOWN:
            lines.append(f"  - and {len(problems) - SHOWN} more")
        lines.append(
            f"Give uid {SERVICE_UID} (or the user the container runs as) these paths on the host. "
            "Files with more than one hard link and symlink targets are never changed "
            f"automatically: copy them into place. {HELP}"
        )
        print("\n".join(lines), file=sys.stderr)
        return 1
    print("Usage: data_dir.py take-ownership | check", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
