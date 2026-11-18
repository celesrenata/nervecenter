#!/bin/sh
set -eu
mkdir -p "$(dirname "${ROO_CODE_IPC_SOCKET_PATH}")" 2>/dev/null || true
/usr/lib/code-server/lib/node /opt/menagerie/menagerie-status.js &
exec /usr/bin/entrypoint.sh \
  --bind-addr 0.0.0.0:8080 \
  --auth none \
  --user-data-dir /tmp/code-server/data \
  --extensions-dir /opt/menagerie/extensions \
  "$@"
