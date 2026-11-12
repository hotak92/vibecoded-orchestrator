# RL reranker (`vct-rl-reranker`)

Reinforcement-learning reranker for the orchestrator's knowledge graph. Part of
the orchestrator (AGPL-3.0) since v0.2.106; no licence is needed.

## What this is

A small HTTP server (`rl_server.py`) that the Weaviate MCP server calls
after every hybrid search to rerank candidate results before returning
them to the agent. Reranking is online-learned: the server watches the
agent's output, detects which retrieved nodes were actually cited, and
trains an `RLModel` (small PyTorch network) on those signals. The MCP-side
client is `claude_mcp_servers/rl_client`.

Whether the scores are USED is decided on the client side, by the shipped RL
scoring lock (`vco_lib/rl_scoring_lock.toml`, set until a trained model ships)
and the per-project RL toggle. Retrieval and citation events are logged either
way, so there is data to train on.

## HTTP API

The container exposes a small aiohttp server on port `11450` by default.
There are 8 endpoints — 3 inherited from v0.1.0 plus 5 added in v0.1.1 for
launcher-driven model-lifecycle management.

### v0.1.0 — retrieval + online training

| Method | Path           | Purpose |
|--------|----------------|---------|
| GET    | `/health`      | Liveness + weights state: `{ "ok": true, "model": "RLModel loaded" \| "not trained", "trained": bool, "weights": { "profile", "loaded_version", "bundled_version" }, ... }`. |
| POST   | `/cache_nodes` | Score one search's candidates with the loaded model and return the reranked order + scores. The client sends the vectors it already logs (query vector, node vectors, linked vectors, node types) as float16 in a `features` block (`claude_mcp_servers/rl_client/rerank_wire.py`, wire protocol 2); the container embeds nothing. A body without `features` gets 422. |
| POST   | `/rl_update`   | Submit Claude's `agent_output` to trigger citation detection + online training for the cached task_ids. |

### v0.1.1 — model lifecycle (launcher-driven)

| Method | Path                | Purpose |
|--------|---------------------|---------|
| POST   | `/rotate_weights`   | Hot-swap the loaded RLModel to a different `.pt` checkpoint without restarting the container. |
| POST   | `/reset`            | Fork (or fork + offline-specialize) this project's local model from the global checkpoint, or (`"strategy": "bundled"`) re-seed it from the shipped checkpoint — 404 "not trained" when none ships. |
| POST   | `/finetune`         | Spawn an async offline-training pass on the project's events log. Returns `job_id`. |
| GET    | `/finetune_status`  | Poll the state of a `job_id` returned by `/finetune` or `/reset` or `/global/retrain`. |
| POST   | `/global/retrain`   | Async retrain of the global model from N projects' events (online or offline). Returns `job_id`. |

#### Wire shapes (v0.1.1)

`POST /rotate_weights`

```json
// Request
{ "weights_path": "/data/state/rl_model_arctic_2026-05-23.pt" }

// 200 OK
{ "ok": true, "loaded": "/data/state/rl_model_arctic_2026-05-23.pt", "emb_dim": 1024 }

// 400 — missing / outside /data/state/ or /data/global/
{ "ok": false, "error": "weights_path must be an absolute path under /data/state/ or /data/global/" }

// 500 — torch couldn't load the checkpoint
{ "ok": false, "error": "load failed: <torch exception>" }
```

`POST /reset`

```json
// Request — strategy="fork": just copy global → project, reload.
{ "strategy": "fork", "days": 30 }

// Request — strategy="fork_and_specialize": fork + kick off background offline training.
{ "strategy": "fork_and_specialize", "days": 30 }

// 200 OK (fork)
{ "ok": true, "strategy": "fork", "weights_path": "/data/state/rl_model_arctic_1024.pt", "job_id": null }

// 200 OK (fork_and_specialize)
{ "ok": true, "strategy": "fork_and_specialize", "weights_path": "/data/state/rl_model_arctic_1024.pt", "job_id": "8f1c…" }

// 404 — no global weights file for this (tag, dim)
{ "ok": false, "error": "global weights not found at /data/global/rl_model_arctic_1024.pt. Set RL_GLOBAL_WEIGHTS_DIR or place the file before /reset." }
```

`POST /finetune`

```jsonc
// Request — DB-only (v0.2.11+). The project is taken from the
// `X-VCT-Project-ID` HEADER (not the body); events are read from launcher.db
// via the hub. A request with no header / the `_base` sentinel → 400.
// Header:  X-VCT-Project-ID: <project-id>
{
  "base_weights_path": "/data/state/rl_model_arctic_1024.pt",
  "days":              30
  // "events_path" is accepted but IGNORED (back-compat with pre-cutover launchers)
}

// 202 Accepted — job is running in the background
{ "job_id": "8f1c4b6d…" }
```

`GET /finetune_status?job_id=<uuid>`

```json
// 200 OK — in progress
{ "state": "running", "percent": 42, "message": "Running training…" }

// 200 OK — terminal success. ``state == "done"`` matches the launcher's
// `run_finetune_then_rotate` poll-loop in rl_service.rs.
{
  "state":               "done",
  "percent":             100,
  "message":             "Fine-tune complete",
  "result_weights_path": "/data/state/rl_model_arctic_1024_finetuned.pt"
}

// 200 OK — failure. The launcher treats "failed" or "error" as terminal failure.
{ "state": "failed", "reason": "training error: <details>" }

// 404 — unknown job_id (e.g. container restarted)
{ "error": "unknown job_id" }
```

`POST /global/retrain`

```jsonc
// Request (v0.2.11+, DB-only). project_ids is the DATA DRIVER: each project's
// events are read from launcher.db via the hub. mode="online" replays each
// project's pairs through the global model; mode="offline" trains on the union.
{
  "mode":         "online",
  "project_ids":  ["proj-a", "proj-b"],
  "days":         30
  // "events_paths" is no longer used (DB-only); ignored if present
}

// 202 Accepted — poll via /finetune_status?job_id=…
{ "job_id": "9d2e7…" }
```

#### Security notes

All paths in v0.1.1 request bodies are validated against an allowlist of
container-side bind-mount roots (`/data/state/`, `/data/global/`, `/data/logs/`)
*after* `Path.resolve()` so `..` traversal is rejected. Anything outside
those roots returns `400` with a clear error message. This matters because
the endpoints are reachable from the host loopback by anything that knows
the container's port — including code that isn't the orchestrator launcher.

#### Job lifecycle

`/finetune`, `/reset?strategy=fork_and_specialize`, and `/global/retrain`
spawn an in-process `asyncio` task and register it under a UUID in the
server's `_jobs` registry. Jobs are NOT persisted: a container restart
drops every in-flight job's state. Callers (the launcher) treat a 404
from `/finetune_status` as "job vanished — fall through to the next
step". The launcher already implements this fallback path in
`run_finetune_then_rotate`.

The result weights from `/finetune` are written to
`<state>/rl_model_<tag>_finetuned.pt` — a sibling of the active weights
file. The caller decides when to swap them in via a deliberate
`/rotate_weights` call; the server doesn't auto-rotate.

## Per-embedding-source networks

Each KG text-embedding source gets its own RLModel weights file:

| ACTIVE_EMBEDDING | RL tag             | dim  |
|------------------|--------------------|------|
| qwen3            | qwen3_1024         | 1024 |
| arctic           | arctic_1024        | 1024 |
| openai           | openai_3small_1536 | 1536 |

Different embedding models live in different latent spaces; reusing one
network across sources causes catastrophic interference. The mapping is
applied at process startup (`rl_server.py::_resolve_model_tag_and_dim`)
and the resolved tag drives both the loaded `.pt` file and the v2 log
schema's `embedding_source` field.

**Scope**: KG retrieval only (text embeddings). Code-graph reranking
will ship in a separate future module (`vct-code-reranker`) with its
own global model and per-project forks. The `codesage` 2048-dim entry
was removed from this table on 2026-05-19. Open to adding future text
embedders (e.g. `jina`); the table is data-driven.

## v2 log schema

Every retrieval event in `rl_events.jsonl` carries `schema_version: 2`
plus `embedding_source`, `embedding_dim`, `embedding_model`. The offline
trainer filters events by `embedding_source` to match its target network.

Legacy v1 events (no embedding metadata) are upgraded by
`migrate_rl_log_v1_to_v2.py`, which infers the source from `query_emb`
length plus the qwen3 cutover date (2026-04-14).

## Files

| File | Role |
|---|---|
| `rl_server.py` | aiohttp server, per-project routing, model-lifecycle endpoints, hub client |
| `retrieval_rl.py` | online learner (rerank + citation-driven update) |
| `rl_model.py` / `rl_model_v1.py` | the v2 network (default) / the v1 snapshot (`RL_ARCH_VERSION=v1`) |
| `offline_trainer.py`, `hub_event_loader.py` | offline training over launcher.db events read through the hub |
| `bundled_weights.py` | the ONE reader of `weights/manifest.json` (stdlib only) |
| (imported, not copied) | `rl_client.rl_logger` (`claude_mcp_servers/rl_client/rl_logger.py`), `rl_client.rerank_wire` (the `/cache_nodes` feature format) plus the stdlib-only `vco_lib.rl_training_targets`, `vco_lib.atomic` (every atomic write) and `vco_lib.log_setup` (logging setup) — one copy each; the image COPYs them from where they live (`tests/rl_reranker/test_image_sources.py` pins the COPY set) |
| `migrate_rl_log_v1_to_v2.py` | ports legacy JSONL events to the v2 schema |
| `db/` | the module's own launcher.db migrations (`rl_state`, `rl_weights_state`, …) |
| `scripts/` | maintainer training scripts over the frozen JSONL bake-off corpus (`RL_TRAIN_WORKDIR`, see `scripts/_workdir.py`) |
| `training/` | export → shard trainer → evaluation for the shipped weights (see "Training the shipped weights"; not in the image) |
| `vct-module.json` | the module manifest the launcher installs from |

## Tests

They run in the orchestrator's suite, under `tests/rl_reranker/`:

```bash
PYTHONPATH=.:claude_mcp_servers python -m pytest tests/rl_reranker -q
```

## Container build

The launcher builds the image when the module is installed or updated
(`install.method = "container_build"`): the same build the standalone compose
service (`infrastructure/docker-compose.yml`, service `rl_reranker`, profile
`rl`) runs. The build context is the CHECKOUT ROOT (the image COPYs the two
shared modules above from where they live); the root `.dockerignore`
whitelists what enters it. By hand, from the checkout root:

```bash
podman build --build-arg VARIANT=cpu  -f claude_mcp_servers/rl_reranker/Dockerfile -t localhost/vco-rl-reranker:dev-cpu  .
podman build --build-arg VARIANT=cuda -f claude_mcp_servers/rl_reranker/Dockerfile -t localhost/vco-rl-reranker:dev-cuda .
podman build --build-arg VARIANT=rocm -f claude_mcp_servers/rl_reranker/Dockerfile -t localhost/vco-rl-reranker:dev-rocm .
```

PyTorch's CPU / CUDA / ROCm wheels are mutually exclusive, so each variant is
its own image. The launcher picks the variant from the manifest's
`runtime.gpu_image_variants` and the host's GPU, and tags the image
`localhost/vco-rl-reranker:<module version>-<variant>`.

## Running standalone

```bash
cd infrastructure && podman-compose --profile rl up -d rl_reranker
curl http://localhost:11450/health
```

Do not run it next to a launcher-managed RL module: both bind host port 11450.

## Weights

Trained checkpoints are bundled in `weights/`:

```
weights/manifest.json
weights/<profile>/<version>/rl_model_<profile>_<version>.pt
```

`manifest.json` lists, per embedding profile (`qwen3`, `arctic` — they ship
together), the versions present with their sha256 and the `current` one the
image serves (`null` = none ships: the server runs fresh weights and reports
`"model": "not trained"`). `bundled_weights.py` is its only reader; the image
build runs `python -m rl_reranker.bundled_weights verify` and fails on any
mismatch, and `tests/rl_reranker/test_bundled_weights.py` runs the same check
in the suite.

On first start the server copies the current checkpoint for the active profile
into `state/` as `rl_model_<profile>_<version>.pt` and never overwrites one
that is already there. It then serves that copy unless a local checkpoint has
earned its place (`rl_server.choose_local_checkpoint`): a checkpoint with no
training history (a fresh `rl_model_<profile>_local.pt`) or an older base never
shadows the bundled one; one with its own training on top of the current base,
or a newer base, is kept. Which local files count as the user's own is decided
by `vco_lib/rl_weights_ownership.toml`, the table the launcher's "Download
default weights" reads as well. Shipping new weights is a commit to `weights/`;
an orchestrator update then rebuilds the image.

## Training the shipped weights

`training/` (kept out of the image) holds the offline pipeline:

```bash
# 1. export: launcher.db (read-only) -> text-free float16 shards; FIELDS.md documents every field
python -m claude_mcp_servers.rl_reranker.training.export --list-projects
python -m claude_mcp_servers.rl_reranker.training.export --out <dir>
# 2. train + evaluate one profile against plain cosine ordering (both hold-outs)
python -m claude_mcp_servers.rl_reranker.training.shard_trainer train \
    --data <dir> [--data <volunteer dir> ...] --profile qwen3 --out <run dir> --version <semver>
# 3. install the checkpoints into weights/ — refused unless BOTH profiles pass the unlock bar
python -m claude_mcp_servers.rl_reranker.training.shard_trainer ship \
    --report <run dir>/report_qwen3.json --report <run dir>/report_arctic.json \
    --weights-root claude_mcp_servers/rl_reranker/weights
```

The unlock bar (`training/eval_harness.py`): NDCG@10 at least 2% above plain
cosine ordering on a later time window AND on a project never trained on, and
no project worse than cosine.

One held-out project cannot tell a model that transfers from one that
memorised which nodes of the training projects were useful (many nodes are
shared across projects through the shared KG). Before shipping, check a design
with leave-one-project-out, and see what learning per project would give:

```bash
# one fold per project with >= 3000 queries; smaller judged projects stay out of every fold;
# passes only if every fold passes the bar AND every unseen project gains >= 2% on its own
python -m claude_mcp_servers.rl_reranker.training.transfer lopo \
    --data <dir> --profile qwen3 --out <run dir> [training options, e.g. --rotation-augment --init cosine]
# online per-project adaptation from a cosine-equivalent start (and from each fold's network)
python -m claude_mcp_servers.rl_reranker.training.transfer adapt \
    --data <dir> --profile qwen3 --out <run dir> --base-report <run dir>/lopo_qwen3.json --purge-hours 24
```

Both commands take the `train` options (`--init none|cosine|<checkpoint>`,
`--l2sp`, `--entity-dropout`, `--arch v2|v1`, `--purge-hours`, the loss
weights) and report MRR@10 of literally cited nodes next to NDCG@10: the
training target rewards nodes that are typical of the whole candidate set, so
an NDCG gain alone can come with the named node ranked lower.

## License

AGPL-3.0-or-later, as the rest of the orchestrator.
