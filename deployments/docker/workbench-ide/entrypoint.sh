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
# The Owner's skills, rules and skillhub (ConfigMap workbench-menagerie-settings,
# built by scripts/workbench_menagerie_bundle.py) go into ~/.roo and ~/.agents.
if [ -f /etc/menagerie/home.tgz ]; then
  tar -xzf /etc/menagerie/home.tgz -C "$HOME" --no-same-owner 2>/dev/null || true
fi
/usr/lib/code-server/lib/node -e '
const fs = require("fs");
// Trim, and JSON-escape before substituting: a stray newline or quote in the
// key must not break the JSON (a parse error would print the key in the log).
const key = (process.env.OMNIROUTE_API_KEY || "").trim();
const esc = (v) => JSON.stringify(v).slice(1, -1);
const base = process.env.OMNIROUTE_BASE_URL || "http://omniroute.omniroute.svc.cluster.local:20128/v1";
const model = process.env.MENAGERIE_DEFAULT_MODEL || "local/code";
let owner = null;
try { owner = fs.readFileSync("/etc/menagerie/settings.json", "utf8"); } catch {}
const fallback = {
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
let profile = fallback;
if (owner) {
  try {
    profile = JSON.parse(
      owner.split("__OMNIROUTE_API_KEY__").join(esc(key)).split("__OMNIROUTE_BASE_URL__").join(esc(base))
    );
  } catch {
    console.error("menagerie settings: invalid JSON after substitution; using the default profile");
  }
}
fs.writeFileSync("/tmp/menagerie-import.json", JSON.stringify(profile));
const settingsPath = process.argv[1];
let settings = {};
try { settings = JSON.parse(fs.readFileSync(settingsPath, "utf8")); } catch {}
try { Object.assign(settings, JSON.parse(fs.readFileSync("/etc/menagerie/vscode-settings.json", "utf8"))); } catch {}
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
