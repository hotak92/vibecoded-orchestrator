# Telemetry

Telemetry is **opt-in, off by default, and never sends data without an
explicit `VIBECODED_TELEMETRY=true` environment variable**.

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

# Or via the `vco` CLI (shipped in 0.2.0; binary at launcher/tools/vct-cli/):
vco telemetry status
```

Each line is one event. Safe to delete or hand-edit at any time.

## Disabling telemetry

The `.env` written by `install.py` defaults to `VCT_TELEMETRY=false`
(canonical key since 2026-05-01; `VIBECODED_TELEMETRY` is read as a
back-compat alias). To turn it back off after opting in:

```bash
echo "VCT_TELEMETRY=false" >> .env
```

Or simply unset the variable. Both the collector and uploader fail-closed
on anything other than `"true" / "1" / "yes" / "on"`.

## When the endpoint goes live

Self-host: deploy your own Supabase / equivalent edge function with a compatible request shape, set `VIBECODED_TELEMETRY_URL` to its URL, and the uploader will POST events there instead of writing to the pending file.

Until then telemetry stays local, on disk, and visible.
