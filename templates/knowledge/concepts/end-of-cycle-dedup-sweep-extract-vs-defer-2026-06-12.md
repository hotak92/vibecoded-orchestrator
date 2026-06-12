---
title: End-of-cycle dedup sweep — what to extract pre-tag vs defer to next cycle
type: concept
tags: [mid-level-architecture, modularity, refactoring, release-discipline, code-quality]
created: 2026-06-12T00:00:00Z
updated: 2026-06-12T00:00:00Z
valid_from: 2026-06-12T00:00:00Z
valid_until: null
status: active
---

# End-of-cycle dedup sweep — what to extract pre-tag vs defer to next cycle

## Context (v0.2.54 Track J)

A multi-track release cycle applies "search before add, extract before
duplicate" *per track*: each track extracts the helpers IT would
otherwise duplicate (`secrets_bootstrap`, `gpu_profile`, `boot_token`,
`bundle_globs`, ...). What per-track discipline cannot catch is
duplication that *predates* the cycle or accumulates *across* tracks —
nobody's brief owns it. A dedicated end-of-cycle sweep (Track J) found,
in a codebase that had just been through 12 disciplined tracks:

* 4 Python copies of `_atomic_write_text` + 1 inline sibling — even
  though `vco_lib/atomic.py` had existed for a full release and its own
  docstring queued the consolidation;
* 6 Rust copies of the log-tail truncation recipe (3 per-module consts
  + 3 `floor_char_boundary` fns + 3 capping closures in db writers,
  plus 3 `tail_log` siblings in the command layer) — one copy's comment
  explicitly argued *against* extraction back when there were two;
* 4 byte-identical `error_response` envelope builders across vct-hub
  API modules — each carrying a "match the shape modules_api uses"
  comment, i.e. consistency maintained by copy-paste.

## Finding duplicates cheaply

One grep pays for the whole sweep — histogram of function names:

```bash
grep -rn "def " <roots> | sed 's/.*def \([a-zA-Z_][a-zA-Z0-9_]*\).*/\1/' \
  | sort | uniq -c | sort -rn | awk '$1 > 1'
# Rust: grep "fn " ... same pipeline
```

Then triage each multi-hit name: delegate/adapter (fine), domain
coincidence (fine), standalone-script constraint (fine — e.g. template
scripts copied into user projects can't import the shared lib), or
genuine copy (extract).

## The extract-vs-defer line, days before a tag

EXTRACT pre-tag when ALL hold:
1. The bodies are byte-identical or write-equivalent (you can argue the
   equivalence in one paragraph — e.g. `newline=""` vs `newline="\n"`
   are identical for text-mode *writes*).
2. The consolidated implementation already exists or is < ~100 lines.
3. Existing names can stay as thin delegates, so external call-sites
   and tests keep working unchanged.
4. Targeted tests for every touched surface exist and pass.

DEFER to next cycle (backlog entry, not code) when ANY hold:
1. The block is release-critical orchestration with heavy local mutable
   state (an installer `main()`, a self-heal routine that touches
   launcher.db + a live vector DB).
2. Extraction changes behaviour in a way you'd need new tests to pin.
3. The value is "unit-testability next cycle", not "bug-risk now" —
   that value is identical next week, but the regression risk is not.

v0.2.54 application: the three helper families above were extracted
(all four EXTRACT criteria held); `main()` (1591 lines),
`_self_heal_kg_bindings_on_update` (793) and `_seed_weaviate_impl`
(426) were deferred with documented rationale.

## Re-check delegates, not just copies

A function name appearing N times is a *lead*, not a verdict — read
the body before counting it as duplication. Worked examples from this
sweep:

- `vct-hub::auth::write_token_file` was already a delegate to the
  shared `boot_token` primitive (Track I verifier caught it during
  Wave 2; counted as 1, not 2).
- `generate_token_hex`: the **end-of-cycle audit identified 3 copies**
  in the launcher commands crate (`module_db_client.rs`,
  `module_db.rs`, `module_default_weights.rs`) that the Track I
  verifier had only resolved for `vct-hub::module_db_api.rs`. The
  per-track delegation didn't propagate to siblings in a different
  crate. Track J's own adversarial verifier caught the gap; all 4
  copies now delegate to `boot_token::generate_token` (closing the
  v0.2.32-era pre-existing "move to shared crate" TODO).

The general lesson: even when one verifier resolves a duplication
during its track's review, **the SAME shape may persist in adjacent
crates the verifier didn't scan**. End-of-cycle dedup MUST re-run
the histogram against the merged tree to catch cross-track residues
that no per-track verifier owned.

Related: [[relatedTo::code-modularity-search-before-add-extract-before-duplicate]],
[[relatedTo::architecture-first-dispatch-discipline-2026-06-12]]
