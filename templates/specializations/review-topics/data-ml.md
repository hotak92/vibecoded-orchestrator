# Data & ML review topic

Lens for reviewing data pipelines, model integrations, embeddings/retrieval,
and analysis code. Findings carry severity, `file:line`, and evidence.

## Data handling

- Provenance: every dataset records where it came from, when it was pulled,
  and under what license — an unlicensed or license-unknown dataset entering
  a shipped pipeline is a finding.
- Splits: train/validation/test split BEFORE any fitting or feature
  statistics; leakage (test information reaching training) invalidates every
  downstream number.
- Determinism: seeds set for anything stochastic; a result that cannot be
  reproduced from the committed code + data reference is UNVERIFIED.
- PII/sensitive data: minimized, access-controlled, never logged; check
  sample outputs and error messages too.

## Model & inference integration

- Model identity pinned (exact version/revision, not a floating tag); a
  model swap is a change that re-validates downstream behavior.
- Context/window and input limits respected: truncation behavior explicit —
  silent truncation of inputs is a finding.
- Cost/latency budgets stated for inference calls; batching and caching used
  where the call shape allows.
- Fallback paths: a fallback model or degraded mode must not silently change
  output semantics; when it fires, the output says so or logs it.
- Outputs parsed defensively: structured-output extraction validates shape
  before use; unparseable model output fails loud, not empty-and-green.

## Evaluation

- Metrics match the decision they gate: accuracy on a balanced set says
  nothing about a 99:1 production split; name the metric, the set, and the
  baseline in the same breath.
- Baselines present: a model result without a trivial-baseline comparison
  (majority class, keyword match, previous version) is a finding.
- Eval sets are versioned and not reused for tuning until they are stale —
  document when the eval was last refreshed.
- Regression: model/prompt changes run the existing eval suite; improvements
  on one slice must not silently regress another (report per-slice).

## Retrieval / embeddings specifics

- Embedding model and dimension recorded with the stored vectors; mixing
  embedding spaces in one index is a finding.
- Chunking parameters documented and versioned — a chunk-size change without
  re-embedding produces mixed-granularity indexes.
- Retrieval quality claims carry the query set they were measured on.

## Reproducibility of analysis

- Notebook/script analysis that feeds a decision is committed with its
  outputs or regenerable from committed inputs; numbers quoted in docs trace
  to a runnable artifact.
