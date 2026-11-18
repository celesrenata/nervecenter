#!/bin/sh
# Guard rail for Req 7.13, mirrors backend/workbench/validation.classify_git_command.
# Not a security boundary: /usr/bin/git can be called directly. The hard limit is that
# no git credentials are mounted into the pod.
REAL_GIT=/usr/bin/git

# Prints "deny" or "allow". Runs in a function so shifts do not affect the caller's args.
classify() {
  while [ $# -gt 0 ]; do
    case "$1" in
      -C|-c|--git-dir|--work-tree|--namespace|--exec-path) shift 2 2>/dev/null || shift $# ;;
      -*) shift ;;
      *) break ;;
    esac
  done
  [ $# -gt 0 ] || { echo allow; return; }
  sub="$1"
  shift
  case "$sub" in
    push) echo deny; return ;;
    remote)
      for a in "$@"; do
        case "$a" in
          -*) continue ;;
          add|set-url|rename|remove|rm|set-head|set-branches) echo deny; return ;;
        esac
        break
      done
      ;;
  esac
  echo allow
}

if [ "${WORKBENCH_PUSH_APPROVED:-0}" != "1" ] && [ "$(classify "$@")" = "deny" ]; then
  echo "git: command denied by workbench push policy (push is not approved for this project)." >&2
  exit 1
fi
exec "$REAL_GIT" "$@"
