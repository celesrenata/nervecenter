# Phase 4: live OmniRoute registry contract check (read-only)

Date: 2026-11-09. Method: the api pod fetched `GET /v1/models`, `/api/combos` and `/api/providers` from `http://omniroute.omniroute.svc:20128` using the app's existing credential. Fields whose names suggest secrets were redacted in the pod before output; no key was printed or stored. The redacted JSON was fed to the real `LocalOnlyRouteResolver` through a fixture transport on the workstation. No mail content, no model call, no change to the cluster. Temp files deleted.

## Shapes (closes G4 "not proven" item 1)
- `/v1/models`: `{object, data[]}`.
- `/api/combos`: `{combos[], total}`; entries `{name, strategy, models[{kind, model, providerId, connectionId, weight}]}`.
- `/api/providers`: `{connections[], total}` (21 entries); base URL lives in `providerSpecificData.baseUrl`, absent for cloud providers.
All three parse with the existing tolerant parser. No guard change needed.

## Resolver results over all 91 live combos
- 28 ALLOWED_LOCAL_ONLY. 63 denied, all `LEAF_UNKNOWN_LOCALITY` (cloud providers carry no base URL, so they cannot be proven local).
- `hybrid/code`: DENY (openrouter hop). `hybrid/reader`: DENY (mistral hop). `hybrid/coder`: DENY (`ALIAS_UNKNOWN`, does not exist in OmniRoute).
- All `local/*` aliases ALLOW: `local/code, fast, long, any, reader, m5-reader, 5090-reader, planner, reviewer, tester, research, tiny, frontier, 5090, m5max, 4070ti`.
- Local leaves rest on real base URLs: `vllm` at `http://192.168.133.3:2701/v1` and `http://10.1.1.12:2701/v1` (the latter is Granite on gremlin-1), `llama-cpp` at `http://192.168.133.2:7777/v1`. All private (RFC1918).

## Conclusions
1. The spec's `hybrid/*` aliases can never be used for mail: they fall back to cloud providers. The `local/*` aliases are the candidates. Owner decision needed on which alias NerveShield uses (suggest `local/reader`, read-only extraction role). This resolves the alias mismatch question in favour of a third option.
2. "Local" means private-network, not on-host. Those LAN endpoints are the unauthenticated ones (G3 risk). The guard proves content stays on the private network; it does not prove the endpoint authenticates callers.
3. Inactive connections are counted conservatively (any remote or unknown connection of a provider denies it).
4. Still true: no DNS resolution, so a private-IP proxy that forwards to a cloud would pass. Fixed IPs in the registry are the mitigation; re-run this check whenever the registry changes.

## Not done
No model call through any route. `local/reader` contents (which models, quality, context size) not evaluated.
