# Per-message registry snapshot (NS-ADR-074) and personal expectedness (NS-ADR-075)

Owner (2026-11-13), answering three items:
- "a temporary cache is fine, as long as it is purged when no longer needed";
- "we can use `/run/secrets/omniroute_zoo_api_key`";
- "sure", to 8.3.

Commits: `eebe11cd` (snapshot) and `b9d9f30d` (expectedness). Migration head is `024_mail_expectedness`.

## 1. Per-message registry snapshot (NS-ADR-074)

**Why.** 8% of observer calls failed closed. The route is checked twice per message (alias selection, then the observer's own guard), which meant six registry GETs, and `/api/combos` occasionally takes more than 3 s.

**What.** `MessageScopedRegistry` wraps the registry transport for **one message**:
- the first read of each path is kept, and a failed read is retried once;
- both route checks see the same snapshot (three GETs, one consistent view).

The runtime builds a new snapshot and a new resolver per message and calls `purge()` in `finally`. Nothing survives between messages, so an OmniRoute edit still applies to the next message. The guard's own "no cache" property is unchanged; the snapshot sits outside it, scoped to a single decision.

**Tests** (4):
- each path is fetched once per message;
- the snapshot is empty after the message;
- the next message re-reads;
- one blip is retried, and a sustained outage still fails closed with nothing sent;
- the snapshot is purged even when recording raises;
- construction requires either a fixed resolver or a snapshot factory.

**Live.** The 29 earlier route-denied messages were re-run: **23 COMPLETED**, 1 registry timeout, the rest invalid model output.

## 2. Zoo key (`/run/secrets/omniroute_zoo_api_key`): not switched, owner decision needed

Probed from the Mac through a port-forward. The key was read into an environment variable and never printed.
- **Allowed:** `/v1/models` 200; `secure/reader` chat works (21 prompt tokens, nothing injected).
- **Denied (403):** `/api/combos`, `/api/providers`, `/api/keys`, `/api/settings`, `/api/memory`, `/api/usage/call-logs`.

The call log identifies it as the key named **`stabulous-m5max`** (scope `self:usage`). Two problems:
1. **The route guard can't read the registry with it.** With this key alone the observer would deny every call.
2. **Another client uses it actively** (`hybrid/code` about every 30 s), and OmniRoute keeps **memory per key**; this key already holds that client's memories. NerveCenter's Ask feature uses the non-secure `auto/best-chat`. Sharing the key would mix NerveCenter's personal context with that agent's memory, in both directions.

Options for the owner:
- (a) a dedicated NerveCenter key for chat, with registry reads kept on a separate credential;
- (b) a read-only registry scope, if OmniRoute offers one;
- (c) keep the current key.

## 3. Personal expectedness, SHADOW (NS-ADR-075)

**Port.** A runtime port of spike 8.3 (`backend/mail_security/expectedness.py`) that does not import the research code.

**Model.**
- Inputs: the observer's request types, plus the sender's registrable domain as a per-account HMAC (`expectedness/v1` label under the ledger key). Counts only.
- **Fixed population prior** (versioned data), not the account's own mix. Spike finding 3: an account-derived prior is dominated by heavy senders.
- Dirichlet posterior with prior strength 4, a ~95% interval and a 5% cutoff, giving UNEXPECTED, EXPECTED or UNCERTAIN.
- Fewer than 5 prior messages from the sender means ABSTAIN.

**Flow.** It runs inside the observer runtime after a completed observation. Each message is **scored against history before it is counted**, so a message never vouches for itself (tested). Scores are stored on the observation as codes. Table `mail_expectedness_counts` (024) holds scope, sender key, request type and count.

**Opt-in and controls.**
- Its own opt-in, `NERVE_MAIL_SECURITY_EXPECTEDNESS_SCOPES`, set to both accounts. It is inert without the ledger key.
- `forget(scope)` erases everything learned.
- Owner tool `seed_expectedness --forget` replays stored observations **in received order**, with no model or Gmail calls.

**Output.** Review queue `mail-review-queue@4` adds `UNEXPECTED_HIGH_IMPACT_ASK` (+2, MEDIUM), only for an UNEXPECTED **high-impact** ask (PAY, CHANGE_PAYMENT_DETAILS, SUBMIT_CREDENTIALS, SHARE_PERSONAL_DATA). It is advisory, and the disposition never changes. The UI label reads "An unusual request for this sender (money, login or personal data)".

**Not built.** Feedback ("this was expected") with undo and expiry needs a UI.

**Seed results:**

| Account | Scored messages | Abstain | Expected | Uncertain | Unexpected high-impact |
|---|---|---|---|---|---|
| 53b03939 | 118 | 111 asks (1 high-impact) | 14 | 1 | 0 |
| fc4e3c57 | 25 | 29 asks | 0 | 0 | 0 |

152 + 17 observations had no request types. As 8.3 predicted, most senders are sparse and abstain, so the engine adds signal only as history builds. Nothing new entered the review queue.

**Tests.** `test_mail_security_expectedness.py` (9). Full suite: 8621 passed, plus the 1 known unrelated failure. e2e: 104 passed.
