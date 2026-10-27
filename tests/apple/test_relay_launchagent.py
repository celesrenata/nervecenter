"""LaunchAgent plist template tests (workstream D — guided install).

Covers the `nerve-personal-relay install` surface that is Python-testable:

  1. The shipped template
     (`apple/Relay/Distribution/relay_launchagent.plist.template`)
     renders a VALID plist after placeholder substitution (parsed with
     `plistlib`, no network, no Swift build required).
  2. The rendered plist carries the required LaunchAgent semantics:
     `KeepAlive: true`, `RunAtLoad: true`, stdout/stderr under
     `~/Library/Logs/nervcenter/`, ProgramArguments pointing at the
     built binary with the `run` subcommand.
  3. The inline fallback template embedded in `apple/Relay/CLI/main.swift`
     stays in sync with the shipped template (same key set, same
     placeholders) so a missing Distribution/ file cannot silently
     change the agent's behavior.

No network calls; pure file reads + `plistlib`.
"""

import plistlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATE = REPO_ROOT / "apple" / "Relay" / "Distribution" / "relay_launchagent.plist.template"
MAIN_SWIFT = REPO_ROOT / "apple" / "Relay" / "CLI" / "main.swift"

BINARY_PATH = "/Users/tester/nervcenter/apple/.build/release/NervePersonalRelay"


def _render(template_text: str, *, binary_path: str = BINARY_PATH) -> str:
    """Apply the same placeholder substitutions `install` performs."""
    rendered = template_text
    rendered = rendered.replace("{{BINARY_PATH}}", binary_path)
    rendered = rendered.replace("{{LOGS_DIR}}", "~/Library/Logs/nervcenter")
    rendered = rendered.replace("{{STANDARD_OUT}}", "~/Library/Logs/nervcenter/relay.out.log")
    rendered = rendered.replace("{{STANDARD_ERROR}}", "~/Library/Logs/nervcenter/relay.err.log")
    rendered = rendered.replace("{{PID_FILE}}", "~/Library/Logs/nervcenter/relay.pid")
    assert "{{" not in rendered, "unsubstituted placeholder remains in rendered plist"
    return rendered


def _rendered_plist() -> dict:
    assert TEMPLATE.exists(), f"missing template: {TEMPLATE}"
    text = TEMPLATE.read_text(encoding="utf-8")
    rendered = _render(text)
    # plistlib is lenient about comments/DOCTYPE; it parses the plist body.
    return plistlib.loads(rendered.encode("utf-8"))


def test_template_renders_valid_plist():
    """The rendered plist must parse cleanly via plistlib."""
    data = _rendered_plist()
    assert isinstance(data, dict)
    assert data["Label"] == "com.nervcenter.relay"


def test_template_has_keepalive_and_runatload():
    data = _rendered_plist()
    assert data["KeepAlive"] is True, "KeepAlive must be true for a durable daemon"
    assert data["RunAtLoad"] is True, "RunAtLoad must be true so the agent starts at login"


def test_template_program_arguments_point_at_built_binary():
    data = _rendered_plist()
    program = data["ProgramArguments"]
    assert program[0] == BINARY_PATH, "ProgramArguments[0] must be the substituted binary path"
    assert program[1:] == ["run"], "the agent must run the `run` subcommand (the durable loop)"


def test_template_logs_under_user_logs_dir():
    data = _rendered_plist()
    out = data["StandardOutPath"]
    err = data["StandardErrorPath"]
    assert out.startswith("~/Library/Logs/nervcenter/")
    assert err.startswith("~/Library/Logs/nervcenter/")
    assert out != err, "stdout and stderr must use distinct log files"


def test_template_has_no_placeholders_after_render():
    rendered = _render(TEMPLATE.read_text(encoding="utf-8"))
    assert "{{" not in rendered
    assert "}}" not in rendered


def _extract_inline_fallback_template() -> str:
    """Pull the inline fallback plist literal out of main.swift.

    The inline template carries the ``<?xml`` prolog on the line
    immediately above the DOCTYPE (inside the multi-line string
    literal); plistlib requires that prolog, so the extraction must
    start there — not at the DOCTYPE.
    """
    text = MAIN_SWIFT.read_text(encoding="utf-8")
    doctype = text.index('<!DOCTYPE plist PUBLIC')
    start = text.rfind('<?xml', 0, doctype)
    assert start != -1, "inline fallback template lost its <?xml prolog"
    end = text.index('</plist>', start) + len('</plist>')
    return text[start:end]


def test_inline_fallback_template_in_sync_with_shipped_template():
    """The inline fallback in main.swift must carry the same keys and
    placeholders as the shipped template (a drift would change agent
    behavior when the template file is missing)."""
    shipped = _rendered_plist()
    fallback_raw = _extract_inline_fallback_template()
    fallback = plistlib.loads(fallback_raw.encode("utf-8"))
    assert set(fallback) == set(shipped), (
        f"inline fallback keys {sorted(fallback)} != shipped keys {sorted(shipped)}"
    )
    # Same required semantics.
    assert fallback["KeepAlive"] is True
    assert fallback["RunAtLoad"] is True
    assert fallback["Label"] == "com.nervcenter.relay"
    # Same placeholder set (so renderPlistTemplate works on both).
    placeholders = {
        "{{BINARY_PATH}}",
        "{{LOGS_DIR}}",
        "{{STANDARD_OUT}}",
        "{{STANDARD_ERROR}}",
        "{{PID_FILE}}",
    }
    assert placeholders <= set(re.findall(r"\{\{[A-Z_]+\}\}", fallback_raw))


def test_install_subcommand_refuses_missing_binary():
    """`install` must refuse (exit .refused) when the binary is not
    executable — check the guard in the Swift source (actionable message)."""
    text = MAIN_SWIFT.read_text(encoding="utf-8")
    case_block = text.split('case "install":', 1)[1].split('case "uninstall":', 1)[0]
    assert "fail(.refused," in case_block, "install must refuse via Exit.refused"
    assert "isExecutableFile(atPath: binaryPath)" in case_block
    assert "swift build" in case_block, "refusal message must tell the user to build first"


def test_template_ships_under_distribution():
    """The template must live at the documented path (install resolves it
    relative to the source tree / executable dir)."""
    assert TEMPLATE.parent.name == "Distribution"
    assert TEMPLATE.name == "relay_launchagent.plist.template"
