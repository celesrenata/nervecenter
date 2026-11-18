"""Property 21: Git push policy.

Validates: Requirements 7.13
"""
from __future__ import annotations

from hypothesis import given, settings, strategies as st

from backend.workbench.validation import classify_git_command

_word = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=8
)
_tail = st.lists(_word, max_size=4)

# Global options before the subcommand: flags with a value and plain flags.
_global_opts = st.lists(
    st.one_of(
        st.tuples(st.sampled_from(["-C", "-c"]), _word).map(list),
        st.sampled_from([["--no-pager"], ["-p"], ["--bare"]]),
    ),
    max_size=3,
).map(lambda groups: [a for g in groups for a in g])

_prefix = st.sampled_from([[], ["git"]])
_mutators = st.sampled_from(["add", "set-url", "rename", "remove"])
_safe_subs = st.sampled_from(
    ["status", "commit", "log", "diff", "pull", "fetch", "checkout", "branch", "clone"]
)


@settings(max_examples=100)
@given(_prefix, _global_opts, _tail)
def test_push_denied_unless_approved(prefix, opts, tail):
    argv = prefix + opts + ["push"] + tail
    assert classify_git_command(argv, False) == "deny"
    assert classify_git_command(argv, True) == "allow"


@settings(max_examples=100)
@given(_prefix, _global_opts, _mutators, _tail)
def test_remote_mutation_denied_unless_approved(prefix, opts, action, tail):
    argv = prefix + opts + ["remote", action] + tail
    assert classify_git_command(argv, False) == "deny"
    assert classify_git_command(argv, True) == "allow"


@settings(max_examples=100)
@given(_prefix, _global_opts, _safe_subs, _tail, st.booleans())
def test_other_commands_allowed(prefix, opts, sub, tail, approved):
    assert classify_git_command(prefix + opts + [sub] + tail, approved) == "allow"


@settings(max_examples=50)
@given(_prefix, _global_opts, st.sampled_from([[], ["-v"], ["show", "origin"], ["get-url", "origin"]]), st.booleans())
def test_remote_read_only_allowed(prefix, opts, rest, approved):
    assert classify_git_command(prefix + opts + ["remote"] + rest, approved) == "allow"
