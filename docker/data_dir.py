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

When ``DATABASE_URL`` names a SQLite file, ``take-ownership`` also prepares the
directory that holds it (see ``take_database``), which can be outside the data
directory.

``check`` (run as the service user): the service can create a file in the data
directory, and can read and write every entry in it (and search every
directory), and can use the SQLite file that ``DATABASE_URL`` names. Otherwise
it lists the paths and exits 1, before the service starts.

See "Data directory and file ownership" in README.md.
"""
from __future__ import annotations

import errno
import os
import stat
import sys
import tempfile

DATA = "/app/data"
SERVICE_UID = 10001
SERVICE_GID = 10001
SHOWN = 10
HELP = 'See "Data directory and file ownership" in README.md.'
# The files SQLite keeps next to a database.
DATABASE_SUFFIXES = ("", "-wal", "-shm", "-journal")
# Directories that are never given to the service user, whatever DATABASE_URL says:
# the system's, and the image's own files in /app (outside the data directory).
SYSTEM_DIRS = ("/proc", "/sys", "/dev", "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot", "/root", "/run")
APP_DIR = "/app"


class Problem(Exception):
    pass


class Refused(Exception):
    """The directory of the SQLite file is not given to the service user."""


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


def sqlite_path(url: str | None, cwd: str) -> str | None:
    """The SQLite file that ``url`` names, as app/db.py create_database reads it,
    made absolute against ``cwd`` (the service runs in the same directory).  None
    when ``url`` is not a SQLite URL."""
    if not url or not url.startswith("sqlite"):
        return None
    path = url.split("///", 1)[-1] if "///" in url else "auth.db"
    return path if path.startswith("/") else os.path.join(cwd, path)


def _inside(path: str, top: str) -> bool:
    return path == top or path.startswith(top + "/")


def _database_target(path: str) -> tuple[list[str], str, str]:
    """The directory components, the file name and the directory of ``path``.
    Refused for a path the preparation does not follow as written: an empty,
    ``.`` or ``..`` component (it is not normalised, so it is prepared exactly
    as the service opens it), or a directory that is never given away."""
    parts = path.split("/")[1:]
    if any(part in ("", ".", "..") for part in parts):
        raise Refused(f"{path}: the path has an empty, '.' or '..' component")
    *dirs, name = parts
    directory = "/" + "/".join(dirs)
    if directory == "/" or any(_inside(directory, d) for d in SYSTEM_DIRS):
        raise Refused(f"{directory} is a system directory")
    if _inside(directory, APP_DIR) and not _inside(directory, DATA):
        raise Refused(f"{directory} holds the image's own files")
    return dirs, name, directory


def _other_entries(dir_fd: int, name: str) -> list[str]:
    """What keeps the directory from holding only the database files: other names,
    and database names that are not regular files with one link (a directory, a
    symlink, a hard link)."""
    allowed = {name + suffix for suffix in DATABASE_SUFFIXES}
    found = []
    for entry in sorted(os.listdir(dir_fd)):
        if entry not in allowed:
            found.append(entry)
            continue
        try:
            st = os.stat(entry, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            found.append(f"{entry} (not a regular file with one link)")
    return found


def _open_directory(parent_fd: int, part: str, path: str) -> int:
    """Open (and create when missing) the directory ``part`` in ``parent_fd``
    without following a symlink."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        return os.open(part, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            try:
                is_link = stat.S_ISLNK(os.stat(part, dir_fd=parent_fd, follow_symlinks=False).st_mode)
            except OSError:
                is_link = False
            raise Refused(f"{path} is {'a symlink' if is_link else 'not a directory'}") from None
        raise Problem(f"{path}: {e.strerror}") from None
    try:
        os.mkdir(part, 0o755, dir_fd=parent_fd)
    except FileExistsError:
        pass  # created meanwhile; opened below like any other
    except OSError as e:
        raise Problem(f"{path}: cannot create it ({e.strerror})") from None
    return _open_directory(parent_fd, part, path)


def _take_database_directory(fd: int, directory: str, name: str, uid: int, gid: int) -> None:
    st = os.fstat(fd)
    if (st.st_uid, st.st_gid) == (uid, gid):
        # Already the service's: used as it is, like the data directory, whatever else it
        # holds. Only the owner bits are completed, in case an earlier run stopped between
        # fchown and fchmod.
        _take(fd, st, uid, gid)
        return
    others = _other_entries(fd, name)
    if others:
        raise Refused(f"{directory} also holds {', '.join(others[:SHOWN])}")
    _take(fd, st, uid, gid)
    # Another writer may have added an entry between the listing and fchown. This
    # catches what it added before this second listing; it is not atomic, so the
    # directory must be used only by this service (README.md).
    others = _other_entries(fd, name)
    if others:
        try:
            os.fchown(fd, st.st_uid, st.st_gid)
            os.fchmod(fd, stat.S_IMODE(st.st_mode))
        except OSError as e:
            raise Problem(
                f"{directory}: {', '.join(others[:SHOWN])} appeared while it was prepared, and its "
                f"owner could not be restored ({e.strerror}): it is still owned by uid {uid}"
            ) from None
        raise Refused(f"{directory}: {', '.join(others[:SHOWN])} appeared while it was prepared")


def take_database(path: str | None, uid: int = SERVICE_UID, gid: int = SERVICE_GID) -> None:
    """Give the service user the directory of the SQLite file ``path`` and the
    database files in it.

    ``DATABASE_URL`` is the operator's choice of that file, and SQLite must create
    files next to it, so the service needs its directory. The directory is given
    only when it is missing, empty, holds only the database files (``<name>``,
    ``-wal``, ``-shm``, ``-journal``), or is already the service's; never a system
    directory or the image's own. The path is followed from ``/`` one component
    at a time without following a symlink, missing directories are created 0755
    (root-owned, except the database's own), and existing parents are never
    changed. Every change goes through a descriptor of the tested object.
    """
    if path is None:
        return
    dirs, name, directory = _database_target(path)
    old_umask = os.umask(0o022)
    try:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            walked = ""
            for part in dirs:
                walked += "/" + part
                next_fd = _open_directory(fd, part, walked)
                os.close(fd)
                fd = next_fd
            try:
                _take_database_directory(fd, directory, name, uid, gid)
            except OSError as e:
                raise Problem(f"{directory}: {e.strerror}") from None
            for suffix in DATABASE_SUFFIXES:
                file_path = f"{directory}/{name}{suffix}"
                try:
                    opened = _open_tested(fd, name + suffix)
                except FileNotFoundError:
                    continue
                except (OSError, Problem) as e:
                    raise Problem(f"{file_path}: {getattr(e, 'strerror', None) or e}") from None
                if opened is None:
                    continue  # a symlink or a file with other links: never changed (check reports it)
                file_fd, file_st = opened
                try:
                    if stat.S_ISREG(file_st.st_mode):
                        _take(file_fd, file_st, uid, gid)
                except OSError as e:
                    raise Problem(f"{file_path}: {e.strerror}") from None
                finally:
                    os.close(file_fd)
        finally:
            os.close(fd)
    finally:
        os.umask(old_umask)


def check_database(path: str | None) -> list[str]:
    """What keeps the current user from using the SQLite file ``path``."""
    if path is None:
        return []
    directory, _, name = path.rpartition("/")
    directory = directory or "/"
    problems: list[str] = []
    try:
        st = os.stat(directory)
    except FileNotFoundError:
        # The service creates the missing directories: the nearest existing one must let it.
        ancestor = directory
        while not os.path.exists(ancestor):
            ancestor = os.path.dirname(ancestor)
        if not os.access(ancestor, os.W_OK | os.X_OK):
            problems.append(f"{ancestor}: cannot create {directory} there")
        return problems
    except OSError as e:
        # Name the directory on the way that the user cannot pass, when there is one.
        walked = ""
        for part in directory.split("/")[1:-1]:
            walked += "/" + part
            if os.path.isdir(walked) and not os.access(walked, os.X_OK):
                return [f"{walked}: cannot pass this directory to reach {directory}"]
        return [f"{directory}: cannot reach it ({e.strerror})"]
    if not stat.S_ISDIR(st.st_mode):
        return [f"{directory}: not a directory"]
    if not os.access(directory, os.R_OK | os.W_OK | os.X_OK):
        problems.append(f"{directory}: cannot read, write or search the directory")
    for suffix in DATABASE_SUFFIXES:
        file_path = f"{directory.rstrip('/')}/{name}{suffix}"
        try:
            file_st = os.stat(file_path)
        except FileNotFoundError:
            continue
        except OSError as e:
            problems.append(f"{file_path}: cannot reach it ({e.strerror})")
            continue
        if not stat.S_ISREG(file_st.st_mode):
            problems.append(f"{file_path}: not a regular file")
        elif not os.access(file_path, os.R_OK | os.W_OK):
            problems.append(f"{file_path}: cannot read and write the file")
    return problems


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
        database = sqlite_path(os.environ.get("DATABASE_URL"), os.getcwd())
        try:
            take_database(database)
        except Refused as e:
            print(
                f"DATABASE_URL names the SQLite file {database}, but its directory is not given to "
                f"the service user (uid {SERVICE_UID}): {e}. Point DATABASE_URL at a file in a "
                f"directory used only for the database, for example on a volume. {HELP}",
                file=sys.stderr,
            )
            return 1
        except Problem as e:
            print(
                f"Cannot give the service user (uid {SERVICE_UID}) the SQLite file that DATABASE_URL "
                f"names: {e}. {HELP}",
                file=sys.stderr,
            )
            return 1
        return 0
    if argv == ["check"]:
        failed = False
        problems = check()
        if problems:
            failed = True
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
        database = sqlite_path(os.environ.get("DATABASE_URL"), os.getcwd())
        problems = check_database(database)
        if problems:
            failed = True
            lines = [f"The service user (uid {os.getuid()}) cannot use the SQLite file that DATABASE_URL names:"]
            lines += [f"  - {p}" for p in problems[:SHOWN]]
            lines.append(
                "A directory on the way that the service cannot pass is never changed automatically: "
                f"let uid {SERVICE_UID} (or the user the container runs as) search it. {HELP}"
            )
            print("\n".join(lines), file=sys.stderr)
        return 1 if failed else 0
    print("Usage: data_dir.py take-ownership | check", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
