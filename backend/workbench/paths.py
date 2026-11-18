"""Path_Guard: contain every file-worker path inside a Project_Root.

Pure and synchronous. Used only inside the file worker.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import unquote

MAX_PATH_CHARS = 4096

_ENCODED_FORBIDDEN = re.compile(r"%(2f|5c|00)", re.IGNORECASE)
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_SEPARATORS = re.compile(r"[/\\]")


class PathNotPermitted(Exception):
    """Raised when a path is malformed or would leave the Project_Root."""


class PathGuard:
    @staticmethod
    def _validate_string(rel: str) -> list[str]:
        """Check the raw string and return its non-empty segments."""
        if not isinstance(rel, str):
            raise PathNotPermitted("path must be a string")
        if len(rel) > MAX_PATH_CHARS:
            raise PathNotPermitted("path too long")
        if "\x00" in rel:
            raise PathNotPermitted("NUL in path")
        if rel.startswith(("/", "\\")) or _DRIVE_PREFIX.match(rel):
            raise PathNotPermitted("absolute path")
        if _ENCODED_FORBIDDEN.search(rel):
            raise PathNotPermitted("encoded separator or NUL")
        decoded = unquote(rel)
        if "\x00" in decoded:
            raise PathNotPermitted("NUL in path")
        raw_segments = _SEPARATORS.split(rel)
        if ".." in raw_segments:
            raise PathNotPermitted("parent segment")
        for raw, dec in zip(raw_segments, _SEPARATORS.split(decoded)):
            if dec == ".." or (raw != dec and dec in (".", "")):
                raise PathNotPermitted("encoded dot segment")
        if len(_SEPARATORS.split(decoded)) != len(raw_segments):
            raise PathNotPermitted("encoded separator")
        return [s for s in raw_segments if s not in ("", ".")]

    @staticmethod
    def _check_inside(real_root: str, real_path: str) -> None:
        try:
            common = os.path.commonpath([real_root, real_path])
        except ValueError as exc:
            raise PathNotPermitted("path outside root") from exc
        if common != real_root:
            raise PathNotPermitted("path outside root")

    @classmethod
    def resolve(cls, root: str | os.PathLike, rel: str, *, create_leaf: bool = False) -> Path:
        """Return the resolved path for ``rel`` under ``root`` or raise.

        An empty ``rel`` resolves to the root itself. With ``create_leaf`` the
        final component may not exist yet: the parent is resolved and checked,
        then the validated final component is appended. An existing symlink at
        the leaf is rejected in that mode.
        """
        segments = cls._validate_string(rel)
        real_root = os.path.realpath(os.fspath(root))
        if not segments:
            if create_leaf:
                raise PathNotPermitted("no leaf component")
            return Path(real_root)
        if not create_leaf:
            real = os.path.realpath(os.path.join(real_root, *segments))
            cls._check_inside(real_root, real)
            return Path(real)
        *parents, leaf = segments
        real_parent = os.path.realpath(os.path.join(real_root, *parents))
        cls._check_inside(real_root, real_parent)
        target = os.path.join(real_parent, leaf)
        if os.path.islink(target):
            raise PathNotPermitted("leaf is a symlink")
        return Path(target)

    @staticmethod
    def open_leaf(path: str | os.PathLike, flags: int = os.O_RDONLY, mode: int = 0o644) -> int:
        """Open ``path`` refusing to follow a symlink at the leaf (O_NOFOLLOW)."""
        try:
            return os.open(os.fspath(path), flags | os.O_NOFOLLOW, mode)
        except OSError as exc:
            if getattr(exc, "errno", None) == 40:  # ELOOP (Linux)
                raise PathNotPermitted("leaf is a symlink") from exc
            raise
