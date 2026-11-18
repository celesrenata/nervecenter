#!/bin/sh
set -eu
mkdir -p "$(dirname "${ROO_CODE_IPC_SOCKET_PATH}")" 2>/dev/null || true
/usr/lib/code-server/lib/node /opt/menagerie/menagerie-status.js &

# Seed the Menagerie provider profile: OmniRoute as an OpenAI-compatible
# endpoint flagged openAiIsOmniRoute, using the per-pod key from the Secret.
# The extension imports it once at activation (menagerie.autoImportSettingsPath).
DATA_DIR=/tmp/code-server/data
mkdir -p "$DATA_DIR/User"
umask 077
/usr/lib/code-server/lib/node -e '
const fs = require("fs");
const key = process.env.OMNIROUTE_API_KEY || "";
const base = process.env.OMNIROUTE_BASE_URL || "http://omniroute.omniroute.svc.cluster.local:20128/v1";
const model = process.env.MENAGERIE_DEFAULT_MODEL || "local/code";
const profile = {
  providerProfiles: {
    currentApiConfigName: "omniroute",
    apiConfigs: {
      omniroute: {
        apiProvider: "openai",
        openAiBaseUrl: base,
        openAiApiKey: key,
        openAiModelId: model,
        openAiIsOmniRoute: true,
      },
    },
  },
};
fs.writeFileSync("/tmp/menagerie-import.json", JSON.stringify(profile));
const settingsPath = process.argv[1];
let settings = {};
try { settings = JSON.parse(fs.readFileSync(settingsPath, "utf8")); } catch {}
settings["menagerie.autoImportSettingsPath"] = "/tmp/menagerie-import.json";
fs.writeFileSync(settingsPath, JSON.stringify(settings, null, 2));
' "$DATA_DIR/User/settings.json"
umask 022
exec /usr/bin/entrypoint.sh \
  --bind-addr 0.0.0.0:8080 \
  --auth none \
  --user-data-dir /tmp/code-server/data \
  --extensions-dir /opt/menagerie/extensions \
  "$@"
