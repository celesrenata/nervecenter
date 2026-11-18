"""Workbench Phase 1 validators and request models."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
TITLE_MIN = 1
TITLE_MAX = 120
KINDS = ("opencode", "menagerie")
GIT_SCHEMES = ("https", "ssh")
LOW_SPACE_BYTES = 5_368_709_120


def validate_slug(slug: object) -> bool:
    return isinstance(slug, str) and SLUG_RE.fullmatch(slug) is not None


def validate_title(title: object) -> bool:
    return isinstance(title, str) and TITLE_MIN <= len(title) <= TITLE_MAX


def validate_kind(kind: object) -> bool:
    return isinstance(kind, str) and kind in KINDS


def validate_git_url(url: object) -> bool:
    """True only for https or ssh URLs that name a host."""
    if not isinstance(url, str) or not url or url != url.strip():
        return False
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    return parts.scheme in GIT_SCHEMES and bool(host)


def low_space(free_bytes: int) -> bool:
    """True when free space is below the 5 GiB threshold."""
    return free_bytes < LOW_SPACE_BYTES


class ProjectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    title: str = Field(min_length=TITLE_MIN, max_length=TITLE_MAX)
    kind: Literal["opencode", "menagerie"]
    git_url: str | None = None

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not validate_slug(v):
            raise ValueError("slug must match ^[a-z0-9][a-z0-9-]{0,62}$")
        return v

    @field_validator("git_url")
    @classmethod
    def _git_url(cls, v: str | None) -> str | None:
        if v is not None and not validate_git_url(v):
            raise ValueError("git_url must be an https or ssh URL")
        return v


class DeleteIn(BaseModel):
    """Delete body. The route compares `confirm` with the slug (Req 2.10, 2.11)."""

    model_config = ConfigDict(extra="forbid")

    confirm: str


# ---- Phase 2: files, sessions, policy ----

MAX_OPEN_BYTES = 2_097_152
MESSAGE_MAX = 20_000
SELECTION_MAX = 100_000
INSTRUCTION_MAX = 10_000

LANGUAGES = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".ts": "typescript", ".tsx": "typescript", ".json": "json",
    ".md": "markdown", ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".sh": "shell", ".bash": "shell",
    ".zsh": "shell", ".go": "go", ".rs": "rust", ".java": "java", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp", ".rb": "ruby", ".php": "php",
    ".sql": "sql", ".xml": "xml", ".nix": "nix", ".lua": "lua", ".swift": "swift",
    ".kt": "kotlin", ".cs": "csharp", ".dockerfile": "dockerfile",
}


@dataclass(frozen=True)
class FileClass:
    openable: bool
    size: int
    detected_type: str  # "text" or "binary"


def classify_file(data: bytes, size: int | None = None) -> FileClass:
    """Openable only when size <= 2 MiB and valid UTF-8 (Req 4.6).

    `size` is the true on-disk size when `data` is only a prefix; it defaults
    to len(data). A refused file carries its exact size and detected type.
    """
    actual = len(data) if size is None else size
    try:
        data.decode("utf-8")
        is_text = True
    except UnicodeDecodeError as exc:
        # A truncated prefix may cut a multibyte character at its very end.
        is_text = (
            size is not None and size > len(data) and exc.start >= len(data) - 3
            and exc.reason == "unexpected end of data"
        )
    detected = "text" if is_text else "binary"
    return FileClass(actual <= MAX_OPEN_BYTES and is_text, actual, detected)


def language_for(path: str) -> str:
    name = path.rsplit("/", 1)[-1].lower()
    if name == "dockerfile":
        return "dockerfile"
    dot = name.rfind(".")
    if dot <= 0:
        return "plaintext"
    return LANGUAGES.get(name[dot:], "plaintext")


def sort_tree(entries: list[dict]) -> list[dict]:
    """Directories first, then names ascending within each group (Req 4.1)."""
    return sorted(entries, key=lambda e: (not e.get("is_dir", False), e.get("name", "")))


def validate_message(text: object, session_running: bool) -> bool:
    return (
        isinstance(text, str) and 1 <= len(text) <= MESSAGE_MAX and bool(session_running)
    )


def validate_selection_action(
    selection: object, instruction: object, session_active: bool
) -> bool:
    return (
        isinstance(selection, str)
        and len(selection) <= SELECTION_MAX
        and isinstance(instruction, str)
        and 1 <= len(instruction) <= INSTRUCTION_MAX
        and bool(session_active)
    )


def is_local_route(model: str | None) -> bool:
    from backend.inference.local_first import is_local

    return bool(is_local(model))


def needs_nonlocal_confirm(model: str | None, confirmed: bool) -> bool:
    """True when a non-local model is chosen without explicit confirmation."""
    return model is not None and not is_local_route(model) and not confirmed


_REMOTE_MUTATORS = {
    "add", "set-url", "rename", "remove", "rm", "set-head", "set-branches",
}
_GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


def classify_git_command(argv: list[str], push_approved: bool) -> Literal["allow", "deny"]:
    """Deny `push` and remote add/set-url/rename/remove unless approved (Req 7.13)."""
    args = list(argv)
    if args and args[0] == "git":
        args = args[1:]
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _GIT_OPTS_WITH_VALUE else 1
    if i >= len(args):
        return "allow"
    sub, rest = args[i], args[i + 1:]
    if push_approved:
        return "allow"
    if sub == "push":
        return "deny"
    if sub == "remote":
        action = next((a for a in rest if not a.startswith("-")), None)
        if action in _REMOTE_MUTATORS:
            return "deny"
    return "allow"


class FileSaveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    content: str
    base_mtime_ns: int


class SessionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    small_model: str | None = None
    confirm_nonlocal: bool = False


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MESSAGE_MAX)


class SelectionActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection: str = Field(max_length=SELECTION_MAX)
    path: str
    instruction: str = Field(min_length=1, max_length=INSTRUCTION_MAX)
