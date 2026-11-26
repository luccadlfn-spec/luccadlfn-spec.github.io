"""read_file, write_file, list_files: confined to the workspace directory.

The sandbox shares this directory and runs untrusted code as the same user, so a
path can turn into a symlink at any moment. Two layers:
1. resolve the real path and refuse anything outside the workspace;
2. open the resolved path one component at a time with O_NOFOLLOW, so a link
   swapped in after the check makes the open fail instead of escaping.
"""

from __future__ import annotations

import os
import stat

from .base import MAX_RESULT_CHARS, ToolError

MAX_WRITE_BYTES = 2 * 1024 * 1024
MAX_LIST_ENTRIES = 200
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class Workspace:
    def __init__(self, root: str):
        self.root = os.path.realpath(root)

    def resolve(self, path: str) -> list[str]:
        """Return the path's components below the workspace root, or refuse it."""
        if not isinstance(path, str) or "\x00" in path:
            raise ToolError("invalid path")
        path = path.strip() or "."
        if len(path) > 1000:
            raise ToolError("path is too long")
        # Absolute paths are accepted only when they already point into the workspace.
        candidate = path if os.path.isabs(path) else os.path.join(self.root, path)
        real = os.path.realpath(candidate)
        if real != self.root and not real.startswith(self.root + os.sep):
            raise ToolError("path is outside the workspace")
        rel = os.path.relpath(real, self.root)
        return [] if rel == "." else rel.split(os.sep)

    def _open_dir(self, parts: list[str], create: bool = False) -> int:
        """Open a directory below the root without following any symlink."""
        try:
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError:
            raise ToolError("the workspace directory is not available") from None
        try:
            for part in parts:
                try:
                    nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, 0o755, dir_fd=fd)
                    nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = nxt
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _open_file(self, parts: list[str], flags: int, create_dirs: bool = False) -> int:
        if not parts:
            raise ToolError("path is a directory")
        dir_fd = self._open_dir(parts[:-1], create=create_dirs)
        try:
            # O_NONBLOCK: opening a FIFO must not hang; regular files ignore it.
            fd = os.open(parts[-1], flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o644, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
            os.close(fd)
            raise ToolError("path is a directory" if is_dir else "not a regular file")
        return fd

    # ---- tools ----
    def read(self, path: str, max_chars: int) -> str:
        max_chars = max(100, min(MAX_RESULT_CHARS, max_chars))
        parts = self.resolve(path)
        try:
            fd = self._open_file(parts, os.O_RDONLY)
        except ToolError:
            raise
        except OSError as exc:
            raise ToolError(_reason(exc)) from None
        with os.fdopen(fd, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            data = fh.read(max_chars * 4 + 4)        # enough bytes for max_chars of any UTF-8 text
        if b"\x00" in data[:4096]:
            return f"[binary file, {size} bytes; not shown]"
        text = data.decode("utf-8", "replace")
        if len(text) > max_chars or len(data) < size:
            shown = text[:max_chars]
            return f"{shown}\n[file is {size} bytes; showing the first {len(shown)} characters]"
        return text if text else "[empty file]"

    def write(self, path: str, content: str, append: bool) -> str:
        data = content.encode("utf-8", "replace")
        if len(data) > MAX_WRITE_BYTES:
            raise ToolError("content is larger than the 2 MB limit")
        parts = self.resolve(path)
        flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else 0)
        try:
            fd = self._open_file(parts, flags, create_dirs=True)
        except ToolError:
            raise
        except OSError as exc:
            raise ToolError(_reason(exc)) from None
        try:
            if append and os.fstat(fd).st_size + len(data) > MAX_WRITE_BYTES:
                raise ToolError("the file would grow past the 2 MB limit")
            if not append:
                os.ftruncate(fd, 0)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        except OSError as exc:
            raise ToolError(_reason(exc)) from None
        finally:
            os.close(fd)
        verb = "Appended" if append else "Wrote"
        return f"{verb} {len(data)} bytes to {'/'.join(parts)}"

    def list(self, path: str) -> str:
        parts = self.resolve(path or ".")
        try:
            fd = self._open_dir(parts)
        except ToolError:
            raise
        except OSError as exc:
            raise ToolError(_reason(exc)) from None
        lines: list[str] = []
        total = 0
        try:
            with os.scandir(fd) as entries:
                for entry in sorted(entries, key=lambda e: e.name):
                    total += 1
                    if len(lines) >= MAX_LIST_ENTRIES:
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        lines.append(f"{entry.name}/")
                    elif stat.S_ISLNK(st.st_mode):
                        lines.append(f"{entry.name} (link)")
                    else:
                        lines.append(f"{entry.name} ({st.st_size} bytes)")
        finally:
            os.close(fd)
        if not lines:
            return "(empty directory)"
        if total > len(lines):
            lines.append(f"[{total - len(lines)} more entries not shown]")
        return "\n".join(lines)


def _reason(exc: OSError) -> str:
    if isinstance(exc, FileNotFoundError):
        return "no such file or directory"
    if isinstance(exc, (NotADirectoryError, IsADirectoryError)):
        return "that path is not the right kind (file vs. directory)"
    if isinstance(exc, PermissionError):
        return "permission denied"
    if exc.errno in (40,):   # ELOOP: a symlink appeared where a plain name was expected
        return "path is outside the workspace"
    return f"file operation failed ({exc.strerror or type(exc).__name__})"
