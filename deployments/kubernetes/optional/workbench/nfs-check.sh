#!/usr/bin/env bash
# Verify the Workbench NFS exports: list, write marker, read back, check
# ownership equals WORKBENCH_UID/WORKBENCH_GID, remove marker.
# Exit 0 when every export passes; otherwise exit 1 naming the failing export.
#
# Env:
#   WORKBENCH_UID / WORKBENCH_GID     expected owner (default 1000 / 1000)
#   WORKBENCH_OPENCODE_PATH           path of the opencode export mount
#                                     (default /mnt/workbench/opencode)
#   WORKBENCH_MENAGERIE_PATH          path of the menagerie export mount
#                                     (default /mnt/workbench/menagerie)
#   WORKBENCH_NFS_SERVER              NFS server, used only with mounting
#                                     (default 192.168.42.8)
#   WORKBENCH_OPENCODE_EXPORT         export path on the server
#   WORKBENCH_MENAGERIE_EXPORT        export path on the server
#   WORKBENCH_NFS_MOUNT=1             mount an export (nfsvers=4.1,hard) when its
#                                     path is not already a mount point.
#                                     Needs root. Mounts made here are unmounted.
set -u

uid="${WORKBENCH_UID:-1000}"
gid="${WORKBENCH_GID:-1000}"
server="${WORKBENCH_NFS_SERVER:-192.168.42.8}"

names=(opencode menagerie)
paths=(
  "${WORKBENCH_OPENCODE_PATH:-/mnt/workbench/opencode}"
  "${WORKBENCH_MENAGERIE_PATH:-/mnt/workbench/menagerie}"
)
exports_=(
  "${WORKBENCH_OPENCODE_EXPORT:-/volume1/Kubernetes/opencode/projects}"
  "${WORKBENCH_MENAGERIE_EXPORT:-/volume1/Kubernetes/menagerie/projects}"
)

owner_of() {
  stat -c '%u:%g' "$1" 2>/dev/null || stat -f '%u:%g' "$1" 2>/dev/null
}

fail() {
  echo "FAIL export=$1 path=$2: $3" >&2
  exit 1
}

check_export() {
  local name="$1" dir="$2" nfs_export="$3" mounted_here=0 marker content got owner

  if [ "${WORKBENCH_NFS_MOUNT:-0}" = "1" ] && ! mountpoint -q "$dir" 2>/dev/null; then
    mkdir -p "$dir" || fail "$name" "$dir" "cannot create mount point"
    mount -t nfs -o nfsvers=4.1,hard "${server}:${nfs_export}" "$dir" \
      || fail "$name" "$dir" "mount of ${server}:${nfs_export} failed"
    mounted_here=1
  fi

  [ -d "$dir" ] || fail "$name" "$dir" "not a directory"
  ls -- "$dir" >/dev/null 2>&1 || fail "$name" "$dir" "cannot list"

  marker="$dir/.workbench-nfs-check.$$"
  content="workbench-nfs-check $name $$ $(date +%s)"
  if ! printf '%s\n' "$content" >"$marker" 2>/dev/null; then
    [ "$mounted_here" = 1 ] && umount "$dir"
    fail "$name" "$dir" "cannot write marker"
  fi

  got="$(cat -- "$marker" 2>/dev/null)"
  if [ "$got" != "$content" ]; then
    rm -f -- "$marker"
    [ "$mounted_here" = 1 ] && umount "$dir"
    fail "$name" "$dir" "read-back content differs"
  fi

  owner="$(owner_of "$marker")"
  if [ "$owner" != "${uid}:${gid}" ]; then
    rm -f -- "$marker"
    [ "$mounted_here" = 1 ] && umount "$dir"
    fail "$name" "$dir" "ownership is ${owner:-unknown}, expected ${uid}:${gid}"
  fi

  rm -f -- "$marker" 2>/dev/null
  if [ -e "$marker" ]; then
    [ "$mounted_here" = 1 ] && umount "$dir"
    fail "$name" "$dir" "cannot remove marker"
  fi

  [ "$mounted_here" = 1 ] && umount "$dir"
  echo "OK   export=$name path=$dir"
}

for i in "${!names[@]}"; do
  check_export "${names[$i]}" "${paths[$i]}" "${exports_[$i]}"
done

echo "All Workbench NFS exports passed."
exit 0
