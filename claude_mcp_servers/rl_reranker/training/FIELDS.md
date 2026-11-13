# RL training export: what is in it

This file describes every file and field the RL training export writes
(`python -m claude_mcp_servers.rl_reranker.training.export`). Read it before you share
an export. The export copies this file into its output directory.

## How to run it

From the orchestrator root, with its venv:

```
# Linux / macOS
.venv/bin/python -m claude_mcp_servers.rl_reranker.training.export --list-projects
.venv/bin/python -m claude_mcp_servers.rl_reranker.training.export --out <new folder> [--exclude-project <name>] ...

# Windows (PowerShell)
.venv\Scripts\python.exe -m claude_mcp_servers.rl_reranker.training.export --list-projects
.venv\Scripts\python.exe -m claude_mcp_servers.rl_reranker.training.export --out <new folder> [--exclude-project <name>] ...
```

`--list-projects` prints one line per project: whether it would be exported,
its hashed id, how many retrieval and citation rows it has per embedding
profile, and its name. The name is printed on your screen only and is never
written to the export. Choose with `--project <name or hashed id>` (export
only these; repeatable) and `--exclude-project <name or hashed id>` (leave
these out; repeatable). Run `--list-projects` again with the same options to
check the selection before you export. `--profile qwen3` or `--profile arctic`
limits the export to one embedding profile.

The export only reads the launcher database. It opens it read-only and never
changes, compacts or deletes anything in it.

## What is never exported

- query text, answer text, node titles, node content, link names;
- project names, folder paths, project ids, session ids, task ids;
- node-type names other than the six built-in ones (`concept`, `project`,
  `tool`, `model`, `hardware`, `research`); any other type is exported as the
  code `other`.

Projects are identified only by a salted hash. The salt is a random value
created on first use in `rl_export_salt` in the VCO state directory
(`~/.vct/` by default). It stays on your machine and is not part of the
export, so nobody can turn a hashed id back into a project.

## What is exported, and what it can reveal

The export contains embedding vectors: the numeric representation of your
queries, of the knowledge-graph chunks they retrieved, and of linked chunks.
Vectors are not text, but they are derived from text: someone with the same
embedding model could test whether a guessed sentence is close to one of your
vectors. If a project's content must not leave your machine in any form,
exclude that project.

Timestamps are rounded down to the hour.

## Files

```
<out>/index.json        summary, code tables, per-profile counts
<out>/FIELDS.md         this file
<out>/<profile>/vectors.npy   float16 vectors, one per row
<out>/<profile>/queries.npz   one row per query
<out>/<profile>/samples.npz   one row per (query, node) pair
```

`<profile>` is `qwen3` or `arctic`. Load the `.npy`/`.npz` files with numpy
(`numpy.load`). All vector indices below are row numbers in that profile's
`vectors.npy`.

### index.json

| field | meaning |
|---|---|
| `format_version` | version of this layout (currently 1) |
| `exported_at` | UTC time the export finished |
| `elapsed_s` | how long the export took |
| `ts_granularity_ms` | timestamp rounding (3 600 000 = one hour) |
| `max_linked` | number of linked-vector slots per sample (5) |
| `type_codes` | node-type code table: the six built-in types 0-5, `null_padding` 6, `unknown` 8, `other` 9 |
| `task_types` | the retrieval paths (code-defined names such as `pre_bash_kg_search`), indexed by `queries.task_type` |
| `label_regimes` | the citation-label computation version, indexed by `queries.label_regime` |
| `projects` | the hashed project ids, indexed by `queries.project` |
| `profiles.<p>.queries` / `samples` / `vectors` | row counts |
| `profiles.<p>.vector_dim` | vector width (1024) |
| `profiles.<p>.vector_lookups` | vectors seen before de-duplication |
| `profiles.<p>.dropped` | rows left out, by reason (see below) |
| `profiles.<p>.kept_queries_by_project` | kept queries per hashed project id |
| `profiles.<p>.bytes` | size on disk |

### vectors.npy

A `(count, dim)` float16 array. Identical vectors are stored once.

### queries.npz (one entry per query)

| array | type | meaning |
|---|---|---|
| `vec` | int32 | the query vector |
| `project` | int16 | index into `index.json` `projects` |
| `ts_hour_ms` | int64 | when the search ran, milliseconds since 1970, rounded down to the hour |
| `task_type` | int16 | index into `task_types` |
| `label_regime` | int16 | index into `label_regimes` |
| `soft_label` | int8 | 1 when the citation was a weaker "soft" one (short answer window) |
| `weight` | float32 | training weight of the query's samples (1.0, or the soft-label weight) |
| `first_sample` | int64 | first row of this query in `samples.npz` |
| `n_samples` | int32 | how many consecutive sample rows belong to it |

### samples.npz (one entry per retrieved node that has a label)

| array | type | meaning |
|---|---|---|
| `query` | int32 | row in `queries.npz` |
| `node_vec` | int32 | the node's vector |
| `linked_vec` | int32 (n, 5) | linked vectors (other chunks of the node, then linked nodes); -1 = empty slot |
| `linked_type` | int8 (n, 5) | type code of each linked slot (`null_padding` when empty) |
| `node_type` | int8 | type code of the node |
| `target` | float32 | training target in [0, 1], computed from how closely the answer used the node |
| `cos_citation` | float32 | the raw answer-to-node cosine the target is computed from (NaN if absent) |
| `literal_cited` | int8 | 1 when the node's title appeared literally in the answer |
| `cited` | int8 | the logged cited flag: 1, 0, or -1 when absent |
| `cos_qn` | float32 | the query-to-node cosine logged at search time (NaN if absent) |
| `score` | float32 | the search engine's score for the node (NaN if absent) |
| `shown_rank` | int16 | the position the node was shown at (-1 if absent) |
| `emb_truncated` | int8 | 1 when the node's text was cut to fit the embedding model, 0 when not, -1 when unknown |

## Why rows are dropped (`dropped` reasons)

| reason | meaning |
|---|---|
| `retrieval_no_citation` | the search was never followed by a citation event (nothing to learn from) |
| `retrieval_soft_label_short_window` | a soft citation over an answer window under the training floor (the label is noise) |
| `retrieval_arctic_before_prefix_fix` | an arctic twin written before the project ran v0.2.101 (its query vector lacks the query prefix) |
| `retrieval_duplicate_superseded` / `citation_duplicate_superseded` | an older copy of a logged event (the latest is kept) |
| `retrieval_no_query_emb`, `retrieval_query_dim_mismatch`, `retrieval_unparseable`, `citation_unparseable` | malformed rows |
| `retrieval_no_scorable_nodes` | no labelled node of the search had a vector |
| `retrieval_not_kg` | a code-graph search (not part of this reranker) |
| `citation_orphan` | a citation whose search is not in the database |
| `sample_node_without_vector`, `sample_node_dim_mismatch`, `linked_dim_mismatch` | single nodes or linked vectors left out |

Test and fixture projects (names the test suite uses) and rows with no
project id are never exported.
