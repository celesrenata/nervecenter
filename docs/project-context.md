# Project context and the restricted MCP endpoint

NerveCenter holds shared project context (`project_contexts` plus append-only
`project_context_notes`). It is the source of truth. ChatGPT Projects on the $20 plan
are not readable through the API, so context is not synced from them.

- Web: Projects view. API (behind SSO): `/api/projects/*`.
- OpenAI engine: `POST /api/projects/{slug}/ask` sends the project's context in the
  system message to OpenAI. Key: Secret `nervecenter-openai` -> `NERVE_OPENAI_KEY`
  (optional; model `NERVE_OPENAI_MODEL`, default `gpt-4o-mini`). Without a key the
  endpoint answers 503 "not configured".
- Restricted MCP: `POST /api/mcp/projects`, JSON-RPC (initialize, ping, tools/list,
  tools/call), tools `list_projects`, `get_project_context`, `add_project_note`.
  Reachable in-cluster at `http://api:8000/api/mcp/projects` and, through SSO, on the
  normal host. There is no public route.

## Auth (pluggable verifiers)

Every request needs `Authorization: Bearer ...`; any failure is one generic 401 and
nothing is read first. Verifiers are tried in order:

1. Bearer tokens: Secret `nervecenter-mcp-projects`, key `tokens` = `client:token,...`
   (token at least 32 characters, e.g. `openssl rand -hex 32`). The client name tags
   the notes written with it (`chatgpt`, `openai`, ...).
2. OIDC resource-server validation, off by default. Enable with
   `NERVE_MCP_OIDC_ENABLED=1`, `NERVE_MCP_OIDC_ISSUER`, `NERVE_MCP_OIDC_AUDIENCE`,
   optional `NERVE_MCP_OIDC_JWKS_URI` (default `<issuer>/protocol/openid-connect/certs`)
   and `NERVE_MCP_OIDC_CLIENT` (default `chatgpt`).

Limits: 64 KiB body, 5 requests/s (burst 10) per client. Audit log: tool, project,
outcome, client; never content.

## Enabling a public route later

See `deployments/kubernetes/optional/mcp-public/mcp-projects-public-ingress.yaml`
(not in the kustomization). ChatGPT Plus cannot add custom MCP servers; full support is
Business/Enterprise/Edu, with Pro limited. When a plan allows it, enable that file and
turn on the OIDC verifier (ChatGPT expects OAuth).

## OpenAI Secure MCP Tunnel (no public route)

Owner tunnel: `tunnel_6afad59db3f481918919526c820f3e2a` (an identifier, not a secret).
`deployments/kubernetes/base/tunnel-client.yaml` runs OpenAI's `tunnel-client`
(`ghcr.io/openai/tunnel-client:v0.0.21`, pinned by digest). It only makes outbound
HTTPS calls to OpenAI and forwards MCP requests to
`http://api.nervecenter.svc.cluster.local:8000/api/mcp/projects`. It adds
`Authorization: Bearer <mcp_token>` to what it forwards, and the endpoint still checks
the token itself.

Owner steps (never put values in the repo or in chat):

1. Create a token for the tunnel client: `openssl rand -hex 32`. Add it to the
   `tokens` value of Secret `nervecenter-mcp-projects` as `openai:<token>` (comma
   separated with any existing entries; the client name `openai` tags its notes).
2. Create Secret `nervecenter-openai-tunnel` with keys `runtime_api_key` (runtime key
   with Tunnels Read + Use) and `mcp_token` (the same token as step 1).
3. Deploy; the pod becomes Ready. Check `kubectl -n nervecenter logs deploy/tunnel-client`
   and `/readyz` on port 8080. Until the Secret exists the pod shows
   `CreateContainerConfigError` and nothing else is affected.

NetworkPolicy `tunnel-client-egress` allows DNS, the api pod on 8000 and TCP 443 to
non-private addresses only (a policy cannot name `api.openai.com`; use
`mtls.api.openai.com` only if control-plane mTLS is configured, it is also on 443).
The pod can reach no other in-cluster service.

### Using the tunnel from NerveCenter's own OpenAI calls

Responses API calls pass the tunnel id in the MCP tool definition, not a URL:

```json
{"type": "mcp", "server_label": "nerve_projects",
 "tunnel_id": "tunnel_6afad59db3f481918919526c820f3e2a",
 "authorization": "<mcp_token>"}
```

Do not set `server_url` for it. Because headers an app forwards override
`tunnel-client`'s static ones, send the token in `authorization` (the Responses MCP tool
field) as well. Keep `allowed_tools` to `list_projects`, `get_project_context`,
`add_project_note`. The existing `POST /api/projects/{slug}/ask` path injects context
directly and does not need the tunnel; the tunnel is for model-driven tool use.
