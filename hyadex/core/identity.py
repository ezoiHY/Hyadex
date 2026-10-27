"""The server's stable identity, persisted under the state directory and shown to clients."""
from __future__ import annotations

import hashlib
import os
import re
import stat
import threading
import uuid
from contextlib import suppress
from hyadex.core.config import STATE_DIR
from pathlib import Path

SERVER_IDENTITY_FILE = STATE_DIR / "server-identity"


SERVER_IDENTITY_LOCK = threading.Lock()
SERVER_IDENTITY_CACHE: dict[str, str] = {}
SERVER_IDENTITY_RE = re.compile(r"[0-9a-f]{24}")


def legacy_server_identity() -> str:
    """Compute the identity used before it became durable."""

    machine = ""
    for path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        with suppress(Exception):
            machine = path.read_text(encoding="utf-8").strip()
            if machine:
                break
    if not machine:
        machine = os.uname().nodename
    payload = f"{machine}|{STATE_DIR.resolve()}".encode("utf-8", errors="ignore")
    return hashlib.sha256(payload).hexdigest()[:24]


def read_server_identity_file(path: Path) -> str | None:
    """Read one private regular identity file without following links."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(f"could not inspect persisted server identity: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("persisted server identity is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError(f"could not open persisted server identity: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
        ):
            raise RuntimeError("persisted server identity changed while opening")
        if os.name != "nt":
            if opened.st_uid != os.geteuid():
                raise RuntimeError(
                    "persisted server identity is not owned by the current user"
                )
            if stat.S_IMODE(opened.st_mode) != 0o600:
                os.fchmod(descriptor, 0o600)
        raw = os.read(descriptor, 256)
        if os.read(descriptor, 1):
            raise RuntimeError("persisted server identity is too large")
    except OSError as exc:
        raise RuntimeError(f"could not read persisted server identity: {exc}") from exc
    finally:
        os.close(descriptor)
    try:
        persisted = raw.decode("ascii").strip()
    except UnicodeError as exc:
        raise RuntimeError("persisted server identity is not ASCII") from exc
    if SERVER_IDENTITY_RE.fullmatch(persisted) is None:
        raise RuntimeError("persisted server identity is invalid")
    return persisted


def write_server_identity_file(path: Path, identity: str) -> bool:
    """Create one private identity without replacing a concurrent winner."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(identity + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # A hard link publishes the fully-fsynced inode only if the final
            # name is still absent. Unlike os.replace, it cannot overwrite a
            # different identity created by an overlapping server process.
            os.link(tmp, path, follow_symlinks=False)
        except FileExistsError:
            return False
        if os.name != "nt":
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(path.parent, directory_flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return True
    except BaseException:
        raise
    finally:
        with suppress(OSError):
            tmp.unlink()
