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
