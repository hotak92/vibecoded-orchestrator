# Telemetry

Telemetry is **opt-in, off by default, and never sends data without an
explicit `VCT_TELEMETRY=true` environment variable** (`VIBECODED_TELEMETRY`
is accepted as a back-compat alias; the canonical key wins when both are
set).

## Endpoint configuration

The upload endpoint is configured via `VIBECODED_TELEMETRY_URL`. When unset, opted-in events are written to `~/.vibecoded/telemetry_pending.jsonl` instead of POSTed. Users who turn telemetry on can inspect exactly what the orchestrator would have shipped — every line is one JSON event in the same shape as the upload body.

To point the uploader at a deployed endpoint:

```bash
export VIBECODED_TELEMETRY_URL=https://my-staging.example/telemetry
```

When `VIBECODED_TELEMETRY_URL` is set to anything other than the default,
the uploader posts to it normally — no diversion through the pending file.

## What's collected (when opted-in)

Canonical schema lives in `VCThelpers/telemetry/collector.py`. Events are
categorised:

| Category | Examples | Why |
|----------|----------|-----|
| Install events | install start/finish, errors | Diagnose first-run failures |
| Hardware profile | OS, CPU class, GPU presence | Right-size default models |
| Embedding mode | gpu / cpu / openai / low_resource | Track which configs are popular |
| Hook firings | counts only — never bodies | Detect broken hooks across users |

The collector does NOT collect source code, file paths inside user
projects, prompt content, KG node bodies, command outputs, or any
user-identifying information beyond an installer-generated UUID hash.

## Inspecting pending events

```bash
# Default location:
cat ~/.vibecoded/telemetry_pending.jsonl | jq .

# Or via the `vco` CLI (built from launcher/tools/vct-cli/):
vco telemetry status
```

Each line is one event. Safe to delete or hand-edit at any time.

## Disabling telemetry

The `.env` written by `install.py` defaults to `VCT_TELEMETRY=false`
(the canonical key; `VIBECODED_TELEMETRY` is read as a back-compat
alias). To turn it back off after opting in:

```bash
echo "VCT_TELEMETRY=false" >> .env
```

Or simply unset the variable. Both the collector and uploader fail-closed
on anything other than `"true" / "1" / "yes" / "on"`.

## When the endpoint goes live

Self-host: deploy your own Supabase / equivalent edge function with a compatible request shape, set `VIBECODED_TELEMETRY_URL` to its URL, and the uploader will POST events there instead of writing to the pending file.

Until then telemetry stays local, on disk, and visible.

## RL retrieval telemetry — what is collected and what it means

The RL retrieval reranker (Pro/MAO modules) keeps a **separate** telemetry path from the install/hardware collector above. This section documents its current-state data posture. Read it before enabling any upload endpoint — no Supabase-side upload path for this data is live.

**What is uploaded on consent.** When RL online training is enabled, an upload carries the **query embedding** (`query_emb`) plus a per-retrieved-node **embedding vector** (`emb`). The raw query **text is stripped** — it is not uploaded. What ships are the numeric vectors and the retrieval outcome labels used to train the reranker.

**Why "no text" is not the same as "no content".** Embeddings are mathematically derived from your text; approximate reconstruction of content is possible in principle (embedding inversion). So this doc does not claim the upload is content-free. It is content-*reduced* — the exact wording, code, and node bodies never leave the machine — but a vector is a lossy projection of the text it came from, not a random token.

**Retention.** The local RL corpus is **bounded**: a client-side prune keeps it from growing without limit, and the hub exposes a prune route for explicit cleanup. Nothing accumulates on disk indefinitely.

**Opt-outs (two levels, two axes).** Both **local logging** and **online training** can be disabled independently, at either the **global** level or **per-project**:

- **Global** — launcher **Preferences** (global RL prefs). Applies to every project unless a project overrides it.
- **Per-project** — the project's settings. Overrides the global default for that one project.

The **local-logging opt-out also skips the embedding COMPUTE**, not just the write — when local logging is off, the embedding is never computed in the first place, so there is no vector on disk and no CPU/GPU spent producing one. Turning training off (while leaving local logging on) keeps the local corpus for your own inspection but uploads nothing.

Local logging is **opt-out**, gated by `RL_LOCAL_LOGGING_DISABLED` (per-project) and `RL_LOCAL_LOGGING_DISABLED_GLOBAL` (machine-wide) — either truthy disables collection for the scope.

### What a logged event carries (local corpus)

The local corpus is written to the `rl_events` table in `launcher.db` by `vct-hub` (the single writer; the POST is soft-fail with no JSONL fallback). Two event types pair on a shared `task_id`:

- **Retrieval event** — the `query` embedding, and per retrieved node: the node embedding, the embeddings of the node's linked/near-chunk context (the matched chunk's neighbours, packed nearest-first), the node type and linked-type names, the matched-chunk indices, and the pre-rerank base score kept separate from any rerank score.
- **Citation event** — the training label: per-title cosine sims computed against the answer, a `literal_cited` boost, the answer-chunk embeddings (`answer_chunk_embs`, rounded) plus a `sha256[:16]` hash per chunk (`answer_chunk_hashes`) for cross-slot dedup, and a `soft_label` flag (with `fire_reason: soft_terminal`) for labels derived from a below-terminal-floor answer window that would otherwise be dropped.

Every event is tagged with its embedding **space** — `embedding_source` / `embedding_dim` / `embedding_model` — in both the payload and denormalized columns, so events from different embedding models never mix in training.

### Dual-embedding write + log (two nets, one search)

When enabled, a single search collects a training example in **two** embedding spaces at once, so a per-model reranker can be trained for each. Three independent per-project toggles, all default off.

> **Enable these in the launcher, not by hand.** The canonical channel is the launcher's per-project **Dual embedding / RL logging** panel (`DualWriteFlagsPanel`). It writes the choices into `launcher.db` (`module_settings`), which is the single source of truth. On every install/update run — and whenever you toggle a checkbox — the launcher **projects** those DB values into `.claude/settings.json` and `.claude/env` as the env vars below. **Do NOT set these env vars by hand**: `config_projection.py` re-derives all three from the DB on every update and overwrites any manual edit (a hand-set value is silently reset on the next update). Editing `launcher.db` via the panel is the only setting that survives.

The projected on-disk env representation (read by the indexer / RL telemetry path — informational, not the thing you set):

- `DUAL_EMBEDDING_WRITE_ALL_SLOTS` (DB: `orchestrator-core / dual_embedding_write_all_slots`) — the KG write embeds each chunk into every configured text slot (not just the active one), so the second slot's named vectors stay populated for retrieval.
- `DUAL_RL_LOG_ENABLED` (DB: `vct-rl-reranker / dual_rl_log_enabled`) — emits a **second** retrieval + citation event in the other slot's space, on a slot-suffixed `task_id` tagged with that slot's `embedding_source`. **Depends on** dual-write: the panel force-enables dual-write when you turn this on, and disabling dual-write turns this off (the DB can never hold the incoherent log=on/write=off pair).
- `DUAL_EMBEDDING_ARCTIC_SECONDARY` (DB: `orchestrator-core / dual_embedding_arctic_secondary`) — adds `snowflake-arctic-embed2` (the `arctic2_embed` named vector) as the secondary slot alongside the active model. **Independent** of the other two (no dependency either way).

> **One-time migration (v0.2.88):** `DUAL_EMBEDDING_ARCTIC_SECONDARY` used to be env-only (no DB backing, no panel toggle) — it survived updates only by being unknown to the projection. It is now a normal projected key, so the launcher DB is the source of truth. **If you previously enabled it by hand-editing `.claude/env` or `.claude/settings.json`, it will read OFF after this update** (the DB default is off, and the DB now wins). Re-enable it ONCE via the panel — the toggle then persists across all future updates.

`DUAL_EMBEDDING_WRITE_ALL_SLOTS` and `DUAL_RL_LOG_ENABLED` must both be truthy for a dual (other-slot) event to be written. Chunk boundaries always follow the **active** model's own preset — dual-write never changes the active slot's chunk fidelity. When a chunk exceeds a secondary model's `num_ctx`, only that secondary's vector is computed over a bounded sub-window and the chunk is tagged in the persisted `secondary_truncated_slots` property, so truncated-vs-full secondary vectors can be partitioned at training time. A single-slot (non-dual) install keeps its single-model chunk sizing byte-for-byte.

### Payload-size guard (drop priority)

Before a POST, the event is measured against `RL_HUB_PAYLOAD_MAX_BYTES` (default ~15.3 MiB, just below the hub's explicit 16 MiB body limit on the ingest route). This is a pathological-case backstop — normal events never approach the cap. If an event exceeds it, only the optional heavy fields are dropped, in a fixed priority: per-node `linked_embs` (whole-field) first, then `answer_chunk_embs`. The **core label fields and core net inputs (`query_emb`, each node's own `emb`) are never dropped** — an event still over the cap after both trims is posted anyway and the resulting 413 is logged at WARNING, so label loss is never silent.

### Answer privacy

The answer **text is never stored** — not in the citation event, not anywhere on disk. Only the answer-chunk **embeddings** (one-way, lossy) and their **hashes** (one-way) are persisted, so labels can be re-derived for a second embedding space or a retuned formula without keeping the answer. As with query embeddings, an embedding is a lossy projection of the text, not a random token — this is content-reduced, not content-free.
