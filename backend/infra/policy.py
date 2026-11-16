"""What agents may run against infrastructure (NS-ADR-120). Pure functions only.

Plan mode (any agent with the infra tools, no approval): read-only ``kubectl``
verbs and a fixed menu of read-only SSH commands. Kubernetes Secrets can never
be read, and no flag may change the identity, cluster or credentials in use.

Act mode (only the steps of a plan the owner approved): the approved
``kubectl`` arguments or SSH command run exactly as written, still without
identity/credential flags and without interactive sessions.

Output from either mode is redacted (keys, tokens, passwords) and capped
before an agent or the UI sees it.
"""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import Any

MAX_ARGS = 32
MAX_ARG_CHARS = 256
MAX_COMMAND_CHARS = 2_000
MAX_STDIN_CHARS = 200_000
MAX_OUTPUT_CHARS = 20_000

KUBECTL_READ_VERBS = frozenset(
    {"get", "describe", "logs", "top", "events", "api-resources", "api-versions",
     "explain", "version", "cluster-info"}
)
#: Never allowed, in either mode: they change identity, target or interactivity.
KUBECTL_FORBIDDEN_VERBS = frozenset(
    {"config", "proxy", "port-forward", "attach", "edit", "plugin", "auth", "certificate",
     "debug", "completion", "kustomize", "alpha"}
)
_KUBECTL_FORBIDDEN_FLAGS = (
    "--token", "--kubeconfig", "--server", "-s", "--as", "--as-group", "--as-uid",
    "--username", "--password", "--client-key", "--client-certificate",
    "--certificate-authority", "--insecure-skip-tls-verify", "--context", "--cluster",
    "--user", "--tls-server-name", "--stdin", "-i", "--tty", "-t", "-it", "-ti",
)
#: Read-only extras: no raw API reads, no dumps, no local files, no watching.
_KUBECTL_READ_FORBIDDEN = ("--raw", "dump", "-f", "--filename", "-k", "--kustomize",
                           "--watch", "-w", "--watch-only")

_UNIT = re.compile(r"^[A-Za-z0-9@._:-]{1,128}$")

#: The read-only SSH menu: name -> (template, parameters).
SSH_READ_COMMANDS: MappingProxyType[str, tuple[str, tuple[str, ...]]] = MappingProxyType({
    "uname": ("uname -a", ()),
    "uptime": ("uptime", ()),
    "disk": ("df -h", ()),
    "memory": ("free -m", ()),
    "os_release": ("cat /etc/os-release", ()),
    "hostname": ("hostnamectl", ()),
    "addresses": ("ip -br addr", ()),
    "listening": ("ss -tlnp", ()),
    "processes": ("ps -eo pid,user,pcpu,pmem,etime,comm --sort=-pcpu", ()),
    "services": ("systemctl list-units --type=service --state=running --no-pager --plain", ()),
    "failed_services": ("systemctl --failed --no-pager --plain", ()),
    "service_status": ("systemctl status {unit} --no-pager --lines=50", ("unit",)),
    "journal": ("journalctl -u {unit} -n {lines} --no-pager --output=short-iso", ("unit", "lines")),
    "kernel_log": ("journalctl -k -n {lines} --no-pager --output=short-iso", ("lines",)),
    "nixos_version": ("nixos-version", ()),
})


class PolicyError(ValueError):
    """A refusal with a stable reason code (never echoes argument values)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _clean_args(args: Any) -> list[str]:
    if not isinstance(args, list) or not args or len(args) > MAX_ARGS:
        raise PolicyError("bad_arguments")
    out: list[str] = []
    for a in args:
        if not isinstance(a, str) or not a or len(a) > MAX_ARG_CHARS or any(
            c in a for c in "\x00\n\r"
        ):
            raise PolicyError("bad_arguments")
        out.append(a)
    return out


def _flag_name(arg: str) -> str:
    return arg.split("=", 1)[0]


def _mentions_secrets(arg: str) -> bool:
    for part in re.split(r"[,/]", arg.lower()):
        base = part.split(".", 1)[0]
        if base in {"secret", "secrets"}:
            return True
    return False


def check_kubectl(args: Any, *, act: bool = False, stdin: str | None = None) -> list[str]:
    """Validated kubectl arguments (without the ``kubectl`` binary), or PolicyError."""
    args = _clean_args(args)
    verb = args[0]
    if verb in KUBECTL_FORBIDDEN_VERBS:
        raise PolicyError("verb_forbidden")
    for a in args:
        if _flag_name(a) in _KUBECTL_FORBIDDEN_FLAGS:
            raise PolicyError("flag_forbidden")
    if not act:
        if verb not in KUBECTL_READ_VERBS:
            raise PolicyError("not_read_only")
        if any(_flag_name(a) in _KUBECTL_READ_FORBIDDEN for a in args):
            raise PolicyError("flag_forbidden")
        if any(_mentions_secrets(a) for a in args[1:]):
            raise PolicyError("secrets_forbidden")
        if stdin is not None:
            raise PolicyError("stdin_forbidden")
        return args
    # Act: local files only as "-f -" with the manifest given in the step's stdin.
    for i, a in enumerate(args):
        name = _flag_name(a)
        if name in ("-f", "--filename"):
            value = a.split("=", 1)[1] if "=" in a else (args[i + 1] if i + 1 < len(args) else "")
            if value != "-":
                raise PolicyError("local_files_forbidden")
        if name in ("-k", "--kustomize"):
            raise PolicyError("local_files_forbidden")
    if stdin is not None and (not isinstance(stdin, str) or len(stdin) > MAX_STDIN_CHARS):
        raise PolicyError("stdin_too_large")
    return args


def ssh_read_command(name: Any, params: Any = None) -> str:
    """The fixed read-only command for a menu entry, or PolicyError."""
    if not isinstance(name, str) or name not in SSH_READ_COMMANDS:
        raise PolicyError("command_not_allowed")
    template, wanted = SSH_READ_COMMANDS[name]
    params = params if isinstance(params, dict) else {}
    values: dict[str, str] = {}
    for key in wanted:
        if key == "unit":
            unit = params.get("unit")
            if not isinstance(unit, str) or not _UNIT.match(unit):
                raise PolicyError("bad_unit")
            values["unit"] = unit
        elif key == "lines":
            lines = params.get("lines", 100)
            if not isinstance(lines, int) or isinstance(lines, bool) or not 1 <= lines <= 1000:
                raise PolicyError("bad_lines")
            values["lines"] = str(lines)
    return template.format(**values)


def check_ssh_act_command(command: Any) -> str:
    """An approved SSH step's command line (run as written), or PolicyError."""
    if not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND_CHARS:
        raise PolicyError("bad_command")
    if "\x00" in command:
        raise PolicyError("bad_command")
    return command


_REDACTIONS = (
    (re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S),
     "[redacted private key]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[redacted jwt]"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{12,}"), r"\1 [redacted]"),
    (re.compile(r"\b(?:sk|pk|ghp|gho|xox[abps])[-_][A-Za-z0-9_-]{16,}"), "[redacted key]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted key]"),
    (re.compile(
        r"(?im)^(\s*[\"']?[\w.-]*(?:password|passwd|secret|token|api[_-]?key|private[_-]?key)"
        r"[\w.-]*[\"']?\s*[:=]\s*)\S.*$"), r"\1[redacted]"),
)


def redact(text: str) -> str:
    """Remove credentials from command output and cap its size."""
    out = text or ""
    for pattern, repl in _REDACTIONS:
        out = pattern.sub(repl, out)
    if len(out) > MAX_OUTPUT_CHARS:
        out = out[:MAX_OUTPUT_CHARS] + "\n[truncated]"
    return out


__all__ = [
    "KUBECTL_READ_VERBS",
    "PolicyError",
    "SSH_READ_COMMANDS",
    "check_kubectl",
    "check_ssh_act_command",
    "redact",
    "ssh_read_command",
]
