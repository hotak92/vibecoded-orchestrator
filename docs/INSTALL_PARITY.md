<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<!-- Copyright (c) 2026 VibeCoded Tools -->

# Install parity — root and project install through ONE engine

*Added v0.2.85 (ruling R-A: "installing a project and the root project should be
a pretty similar procedure — make sure they share code where possible").*

## The one-sentence contract

There is exactly **one** install-bundle engine (`vco_lib.project_init`
`install-bundle`), and **every** installer — the launcher's add-project flow, the
launcher's update flow, and `install.py` installing the orchestrator root into
itself — is a **subprocess client of the same CLI with the same argv shape**. No
installer has its own second enumerator, classifier, manifest writer, or settings
merge.

Before v0.2.85 the root had its own parallel implementation (install.py Steps
5b + 9b: a 4-action classifier, a hooks/scripts-only manifest writer, three
settings merges). A user PROJECT consumed the `--json` contract; the root did
not. That asymmetry is what hid the v0.2.84 stdout-pollution bug — nobody parsed
the root's output, so the corrupted envelope was invisible on the root path.

## The five "ONE home" invariants

| Concern            | The ONE home                                                        |
|--------------------|---------------------------------------------------------------------|
| **Enumeration**    | `vco_lib.project_init._enumerate_bundle_files`                       |
| **Classifier**     | `vco_lib.project_init._file_action` (9-action, incl. `adopt`)       |
| **File writer**    | `vco_lib.project_init._write_file_atomic` (atomic, symlink-guarded) |
| **Manifest writer**| `vco_lib.project_init._write_manifest_atomic`                       |
| **Settings merge** | `vco_lib.project_init._merge_settings_template_for_bundle`          |

The root path stopped having its own copies of each of these in v0.2.85 — it
consumes the project path's, by construction (the WP-1 delegation deletes Steps
5b + 9b and replaces them with one subprocess call). That deletion also fixes
the root **manifest-clobber** defect (F-NEW-1): with only one manifest writer
left, `install.py --update` can no longer rebuild the root manifest from just
hooks/scripts and drop the agents/skills/knowledge entries the launcher wrote.

## stdout under `--json` is a MACHINE CONTRACT

Under `--json`, **nothing** may reach **stdout** except the single JSON document
the clients `json.loads`. Human-facing lines (the adoption NOTICE, audit rows,
progress) go to **stderr** — the stream whose tail the launcher already surfaces.

* The result-envelope schema is declared ONCE in `project_init`, imported by
  every client and by the parity tests: `BUNDLE_RESULT_TOP_KEYS` is a
  **frozenset** (the always-present required FLOOR; the live envelope is a
  superset, so consumers assert `⊇`), and `BUNDLE_ACTION_KEYS` is an **ordered
  tuple** (deliberately, not a frozenset — the `actions` dict is built by
  iterating it and `json.dumps` must emit deterministic byte order under
  `--json`).
* The v0.2.84 incident (the NOTICE printed to stdout → launcher "produced
  unparseable output") is pinned by `tests/test_v0284_json_stdout_contract.py`
  (subprocess incident-shape + a structural no-bare-`print(` guard). v0.2.85's
  parity suite **composes** with that file — it does not duplicate the guard.

## The three clients + their shared argv shape

All three emit the same base argv (the python binary is set separately by each):

```
# update mode  — mode flag BEFORE --json:
-m vco_lib.project_init install-bundle \
   --folder <F> --orchestrator-root <O> --project-folder <F> --update --json
# create mode  — --json first, optional --safe-add AFTER it:
-m vco_lib.project_init install-bundle \
   --folder <F> --orchestrator-root <O> --project-folder <F> --json [--safe-add]
```

The mode-flag position is MODE-SPECIFIC and byte-pinned (it differs because the
two pre-D12 Rust mirrors differed): update emits `--update --json`, create emits
`--json` then the optional `--safe-add`. argparse is order-insensitive, but the
argv is a pinned machine contract (Rust `pin_p3b` + the cross-language
`root_bundle_argv` parity test lock both orders).

| Client                          | Where                                                        | Mode flag         | F vs O            |
|---------------------------------|-------------------------------------------------------------|-------------------|-------------------|
| Launcher add-project (create)   | `projects_v2.rs::run_install_bundle`                        | `--safe-add`* / — | F = project, O = orchestrator |
| Launcher update                 | `projects_v2.rs::run_install_bundle_update_with_root`      | `--update`        | F = project, O = orchestrator |
| `install.py` root self-install  | `vco_lib/self_install.py::run_root_bundle_install`         | `--update`†       | **F = O = root**  |

\* `--safe-add` only when the per-add "Safe add" toggle is on; the default
create argv is byte-identical to pre-v0.2.63.
† update by construction — a re-run of `install.py` over an installed root is an
update because the `.vco-manifest.json` marker exists (D4).

### The two launcher clients share ONE Rust core (v0.2.85 D12 / R-C)

The create and update Rust builders were ~120-line mirrors. They now delegate to
one private `run_install_bundle_core(folder, override, BundleMode)` in
`projects_v2.rs`; `run_install_bundle` (create) and
`run_install_bundle_update_with_root` (update) are thin wrappers.
`build_bundle_argv` is the ONE argv builder; `BundleMode` carries the whole flag
+ warning-string delta. `run_root_bundle_install` (Python) is the THIRD client of
the same CLI contract — a cross-language re-implementation of the spawn+parse
posture, **not** a caller of the Rust core (the parity mechanism is the shared
argv shape + parse posture, not a shared function across the language boundary;
in-process consolidation was deliberately rejected as E7).

## Root adoption policy (D3)

The root migrates onto the shared 9-action classifier **uniformly, `adopt`
included**:

* The root's `.claude/{hooks,scripts}` are RENDERED RUNTIME ARTIFACTS. The
  maintainer's supported edit home is `templates/` (git-tracked, ships to
  everyone) — and **no install flow ever writes under `<root>/templates/`**
  (pinned by `tests/test_v0285_install_parity.py::test_templates_tree_byte_identical_after_root_run`).
  A drifted runtime copy is adopted (converged on the rendered template) while a
  timestamped backup under `.claude/backups/bundle-adoptions/<ts>/` preserves the
  original bytes.
* `knowledge/**` divergence stays **preserve** (never adopt user knowledge);
  `settings.json` flows through the merge path (never the classifier);
  CLAUDE.md / CONTEXT_STATE / MEMORY / `.env` are outside the ops set.
* The launcher path already adopted on the root since v0.2.84; keeping
  `install.py` on preserve would recreate the exact asymmetry R-A bans. The old
  Step 9b already destroyed hook edits with **no** backup — adopt-with-backup is
  strictly safer. Consequently `orchestrator_self_user_modified_preserved` is
  retired (producer gone; id kept for drop-when-absent self-clear).

## Launcher summary honesty (D9)

`UpdateSummary` gained an `adopted: u32` field (`count_for("adopt")`), tallied in
the SHARED Rust core so create and update surface it **uniformly**. Without it an
adoption-only update toasted "0 preserved / 0 changed" — dishonest-by-omission.
The kg/docs re-embed gate (`change_detect::BUNDLE_CONTENT_CHANGING_BUCKETS`)
deliberately **excludes** `adopt`: by D3's carve-out an adoption can never touch
`knowledge/**` or `docs/**`, so it can never be a content change.

## The pinning tests

| Pin                              | Test                                                                                   |
|----------------------------------|----------------------------------------------------------------------------------------|
| Root ⇄ project envelope parity   | `tests/test_v0285_install_parity.py` (both shapes, ONE `_assert_envelope`)             |
| Drift → adopt classifier parity  | `test_v0285_install_parity.py::test_drift_then_adopt_classifier_parity`                |
| knowledge/ preserve, both shapes | `test_v0285_install_parity.py::test_knowledge_divergence_preserved_both_shapes`        |
| templates/ untouchable           | `test_v0285_install_parity.py::test_templates_tree_byte_identical_after_root_run`      |
| compose noop (docker + podman)   | `test_v0285_install_parity.py::test_infrastructure_compose_noop_on_root`               |
| cross-language argv parity       | `test_v0285_install_parity.py::test_root_bundle_argv_matches_rust_call_site` (WP-1 seam)|
| stdout is a machine contract     | `tests/test_v0284_json_stdout_contract.py` (composed with, not duplicated)             |
| `adopted` tally (D9)             | `projects_v2.rs::tests::pin_p1_adopt_entries_tally_into_summary`                       |
| D12 create argv byte-identical   | `projects_v2.rs::tests::pin_p3_create_argv_default_is_byte_identical_to_pre_refactor`  |
| D12 mode flag XOR + prefixes     | `projects_v2.rs::tests::pin_p2_mode_flag_and_warning_prefix_per_mode`                  |
| D12 warning-string byte-fidelity | `projects_v2.rs::tests::d12_per_mode_warning_strings_are_byte_identical_to_base`       |
