"""Owner-only local state file helpers."""

from __future__ import annotations

import errno
import logging.handlers
import os
import stat
from io import TextIOWrapper
from pathlib import Path
from typing import cast


def private_path_has_symlink(path: Path) -> bool:
    """Return whether an existing private-path component is a symlink."""
    current = path.absolute()
    while True:
        if current.is_symlink():
            return True
        if current == current.parent:
            return False
        current = current.parent


def ensure_private_state_directory(path: Path) -> None:
    """Create a private-state directory without traversing symlink components."""
    if private_path_has_symlink(path):
        raise OSError(errno.ELOOP, f"refusing symlinked private state path: {path}", path)
    # mode only applies to newly created directories. Existing caller-owned
    # parents must not have their permissions changed.
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


def open_private_state_file(path: Path, flags: int) -> int:
    """Open a single-link regular state file without blocking on special files."""
    if private_path_has_symlink(path.parent):
        raise OSError(errno.ELOOP, f"refusing symlinked private state path: {path}", path)

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_only = getattr(os, "O_DIRECTORY", 0)
    nonblocking = getattr(os, "O_NONBLOCK", 0)
    if not no_follow or not directory_only or not nonblocking:
        raise OSError(
            errno.ENOTSUP,
            "O_NOFOLLOW, O_DIRECTORY, and O_NONBLOCK are required for private state files",
            path,
        )

    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    parent_descriptor = os.open(
        path.parent,
        os.O_RDONLY | directory_only | no_follow | close_on_exec,
    )
    try:
        try:
            existing = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(existing.st_mode):
                raise OSError(errno.ELOOP, f"refusing symlinked state file: {path}", path)
            if not stat.S_ISREG(existing.st_mode):
                raise OSError(errno.EINVAL, f"state file is not regular: {path}", path)
            if existing.st_nlink != 1:
                raise OSError(errno.EMLINK, f"refusing hard-linked state file: {path}", path)

        # O_NONBLOCK prevents a special file inserted between stat() and open()
        # from hanging the process. Delay O_TRUNC until after fd-based validation.
        open_flags = (flags & ~os.O_TRUNC) | nonblocking
        try:
            descriptor = os.open(
                path.name,
                open_flags | no_follow | close_on_exec,
                0o600,
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            # macOS/APFS may transiently return ENOENT when contenders create
            # the same leaf through openat(). Retry once on the anchored parent.
            if not flags & os.O_CREAT:
                raise
            descriptor = os.open(
                path.name,
                open_flags | no_follow | close_on_exec,
                0o600,
                dir_fd=parent_descriptor,
            )
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError(errno.EINVAL, f"state file is not regular: {path}", path)
            if opened.st_nlink != 1:
                raise OSError(errno.EMLINK, f"refusing hard-linked state file: {path}", path)
            os.fchmod(descriptor, 0o600)
            if flags & os.O_TRUNC:
                os.ftruncate(descriptor, 0)
        except BaseException:
            os.close(descriptor)
            raise
    finally:
        os.close(parent_descriptor)
    return descriptor


class PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotating log handler that refuses symlinked private-state paths."""

    def _open(self) -> TextIOWrapper:
        descriptor = open_private_state_file(
            Path(self.baseFilename),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        )
        try:
            return cast(
                TextIOWrapper,
                os.fdopen(
                    descriptor,
                    self.mode,
                    encoding=self.encoding,
                    errors=self.errors,
                ),
            )
        except BaseException:
            os.close(descriptor)
            raise
