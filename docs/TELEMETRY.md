# Telemetry

Telemetry is **opt-in, off by default, and never sends data without an
explicit `VIBECODED_TELEMETRY=true` environment variable**.

## v0.2.x status: pre-launch deployment

The upload endpoint (`https://ovpdtijpdchzlxbojhsg.supabase.co/functions/v1/telemetry`) hasn't
shipped yet. Until it's live, the uploader writes opted-in events to
`~/.vibecoded/telemetry_pending.jsonl` instead of POSTing them. Users who
turn telemetry on can inspect exactly what the orchestrator would have
shipped — every line is one JSON event in the same shape as the upload body.

You can override the default by pointing the uploader at any deployed
endpoint:

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

The `.env` written by `install.py` defaults to `VIBECODED_TELEMETRY=false`.
To turn it back off after opting in:

```bash
echo "VIBECODED_TELEMETRY=false" >> .env
```

Or simply unset the variable. Both the collector and uploader fail-closed
on anything other than `"true" / "1" / "yes" / "on"`.

## When the endpoint goes live

Operators (us) will:

1. Deploy a Supabase edge function at `https://ovpdtijpdchzlxbojhsg.supabase.co/functions/v1/telemetry`.
2. Bump `VCThelpers/telemetry/uploader.py` to point at it as the new
   default — or leave it; the env var override path already works.
3. Backfill any user's `telemetry_pending.jsonl` by reading the file
   and POSTing it via a one-shot upload script (planned in `scripts/`).
4. Document that flow here.

Until then telemetry stays local, on disk, and visible.
