//! Bundle-update change detectors (kg/docs re-embed gate).
//!
//! Verbatim extraction (v0.2.77 Part 7d) of the content-change detectors that
//! decide whether an install-bundle update actually mutated a `knowledge/` or
//! `docs/` file (and therefore whether a kg-sync re-embed must run):
//! `BUNDLE_CONTENT_CHANGING_BUCKETS`, `is_kg_or_docs_rel_path`,
//! `envelope_kg_or_docs_content_changed`. These previously lived inline in
//! `projects_v2.rs`; behaviour is unchanged; the facade re-exports every symbol.
//! The const is used only by these detectors, so it travels with them.
//!
//! **Cost of the v0.2.94 drift probe** (see the block below): one
//! `kg-sync --check-drift` per project per update, run SEQUENTIALLY, and only
//! when the bundle touched no `knowledge/**`/`docs/**` content AND the
//! project's last sync succeeded AND no sync is in flight. It is read-only (a
//! `knowledge/` walk plus one hash-diff GraphQL query — seconds), performs no
//! embedding and no Weaviate write, and is skipped entirely on every other
//! path.

/// v0.2.71 Piece 5b — relative-path buckets whose membership means the bundle
/// actually CHANGED a file's on-disk bytes (vs left it untouched). Only these
/// gate the kg-sync re-embed; `noop` / `preserve` / `keep-regenerated` /
/// `skip-*` / `orphan-preserved` all leave the on-disk content as-is, so they
/// must NOT trigger a re-embed.
///
/// v0.2.85 D9 note: `adopt` is DELIBERATELY absent here even though it rewrites
/// on-disk bytes. An `adopt` entry can never be a KG/docs content change, for
/// two DISTINCT reasons: (a) `knowledge/**` diverging is classified `preserve`,
/// never `adopt` (the D3 carve-out — user-owned KG state is never overwritten);
/// (b) `docs/**` is never adopted because it is never ENUMERATED as a bundle op
/// at all (no op targets it, so no action bucket — `adopt` included — can carry
/// a docs path). Either way, omitting `adopt` from this gate is correct: adding
/// it could only produce false-positive re-embeds. The `adopted` tally
/// (UpdateSummary) is honesty-only (D9) and does not — must not — feed this
/// content-change gate.
pub(crate) const BUNDLE_CONTENT_CHANGING_BUCKETS: [&str; 4] =
    ["create", "overwrite", "always-overwrite", "orphan-deleted"];

/// True iff a relative bundle path lives under `knowledge/` or `docs/` — the
/// two trees `sync_knowledge_graph.py --all` walks. Normalises Windows
/// backslashes so the same envelope path matches cross-OS.
pub(crate) fn is_kg_or_docs_rel_path(rel: &str) -> bool {
    let norm = rel.replace('\\', "/");
    let norm = norm.trim_start_matches("./");
    norm.starts_with("knowledge/") || norm.starts_with("docs/")
}

/// v0.2.71 Piece 5b — pure inspector over the install-bundle `--json` envelope.
///
/// Returns `true` iff at least one path in a content-CHANGING bucket
/// (`BUNDLE_CONTENT_CHANGING_BUCKETS`) lives under `knowledge/**` or `docs/**`.
/// That is the ONLY condition under which a fresh `kg-sync --all` re-embed can
/// surface new/changed content into Weaviate on an UPDATE.
///
/// Conservative on ambiguity: if `actions` is missing or not the expected
/// object-of-arrays shape, returns `true` (assume something changed → spawn the
/// sync). Better to pay an unnecessary all-skip re-validation (bounded now by
/// the Piece-5a semaphore) than to silently skip a sync that WAS needed.
pub(crate) fn envelope_kg_or_docs_content_changed(v: &serde_json::Value) -> bool {
    let Some(actions) = v.get("actions").and_then(|a| a.as_object()) else {
        // Unparseable / unexpected shape → assume changed (spawn, safe-but-slow).
        return true;
    };
    for bucket in BUNDLE_CONTENT_CHANGING_BUCKETS {
        let Some(arr) = actions.get(bucket).and_then(|x| x.as_array()) else {
            continue;
        };
        for entry in arr {
            if let Some(rel) = entry.as_str() {
                if is_kg_or_docs_rel_path(rel) {
                    return true;
                }
            }
        }
    }
    false
}

/// v0.2.71 Piece 5b — pure decision predicate for the kg-sync spawn gate.
/// Unit-testable without spawning anything.
///
/// On CREATE (`is_initial_create=true`) ALWAYS spawn: a fresh project's
/// pre-existing `knowledge/**`/`docs/**` must be indexed for the first time
/// (the original 2026-05-12 KG-auto-sync purpose). On UPDATE, spawn ONLY when
/// the bundle actually changed KG/docs content — otherwise the `--all` would
/// re-walk byte-identical content and (per the audit) merely multiply
/// Weaviate fetch round-trips under contention, plus risk a full arctic-CPU
/// re-seed of the curated/shared nodes on any slot/hash miss.
///
/// NOTE the deliberate scope: this gates ONLY the content-change axis. A
/// genuine embedding-MODEL or COLLECTION switch is handled by the dedicated
/// re-embed / migration flow (the regenerate-embeddings modal + migration
/// runner), NOT by a bundle update — a bundle update never changes the active
/// embedding model. So content-change is the correct and sufficient gate here.
pub(crate) fn should_spawn_kg_sync_on_bundle(
    is_initial_create: bool,
    kg_or_docs_content_changed: bool,
) -> bool {
    is_initial_create || kg_or_docs_content_changed
}

// ═══════════════════════════════════════════════════════════════════════════
// v0.2.94 — the gate above was NECESSARY but not SUFFICIENT
// ═══════════════════════════════════════════════════════════════════════════
//
// Field evidence, 2026-09-09. After "Update all", the launcher logged for every
// one of 8 projects:
//
//     kg-sync skipped for <id> (bundle update touched no knowledge/** or
//     docs/** content; on-disk KG/docs unchanged — nothing to re-embed)
//
// while `kg-sync --check-drift` reported, read-only, that Weaviate was missing
// most of those nodes: one project 67 missing + 5 stale of 78; another
// 310 missing of 327 — with ZERO objects in its collection, because its INITIAL
// sync had failed on 2026-09-05 (`ModuleNotFoundError: No module named
// 'weaviate'`, from a kg-sync wrapper rendered against a PREVIOUS orchestrator
// location) and its `kg_syncs` row had read `failed` ever since.
//
// The bug is a category error. `kg_or_docs_content_changed` answers "did the
// bundle write a knowledge/docs file" — a statement about THIS UPDATE's disk
// writes. "Does Weaviate hold every node" is a statement about a DIFFERENT
// store, which this gate cannot see. Deriving the second from the first means a
// project whose sync never landed is skipped forever on the strength of "the
// files didn't change" — and each skip re-affirms the stale `failed` pill in
// the GUI while doing nothing about it.
//
// Three more legs, all EVIDENCE rather than inference:
//   * the drift probe (`kg-sync --check-drift`; read-only, seconds, exit 0)
//     asks the store itself;
//   * `kg_syncs.status` — a project that has NEVER succeeded is owed a sync
//     regardless of what the bundle touched;
//   * "could not check" is its own answer and must NEVER be rendered as "every
//     node is present".
//
// COST DISCIPLINE (standing rule — "never re-embed hash-unchanged rows"): every
// spawn here is the ordinary `kg-sync --all`, whose per-node content-hash gate
// skips anything already current, so a drift of N nodes embeds exactly N. No
// `--rechunk`, no force flag, no drop/recreate — pinned by
// `kg_sync::DRIFT_SPAWN_FORBIDDEN_FLAGS` and its test.

/// How the project's `kg_syncs` row reads FOR GATE PURPOSES.
///
/// The raw `status` string is not enough, twice over:
///
/// * `running` / `pending` mean a sync is ALREADY driving this project. Letting
///   those fall through to the drift leg is actively wrong: a probe taken
///   mid-sync sees the not-yet-written nodes as missing, returns `Drift`, and
///   the gate queues a SECOND `kg-sync --all` behind the first — which then
///   re-marks the row RUNNING unconditionally (`kg_sync::run_sync_task`), so
///   the GUI's own progress restarts for no reason.
/// * a `running` row is only evidence of a LIVE task while its liveness stamp
///   is fresh. `get_kg_sync_status` already applies that read-time guard
///   (v0.2.89 BUG 2); reading the row raw here would let a row abandoned by a
///   dead task block the repair for up to one sweeper interval (300 s) — the
///   exact "never succeeded, never retried" state this gate exists to end.
///
/// `heartbeat_is_stale` is the ONE home for that judgement and is called, not
/// re-derived.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum LastKgSync {
    /// No row at all — the project has never been synced.
    Absent,
    /// `success` or `skipped`: a run completed.
    Succeeded,
    /// `failed`, OR a `running` row whose liveness stamp expired (its task is
    /// gone; the row is a corpse, not a claim).
    Failed { status: String },
    /// A live `pending` / `running` row: something else is driving this now.
    InFlight { status: String },
}

/// Classify a `kg_syncs` row for the gate. Pure — `now_ms` and `stale_secs` are
/// injected, so both the live and the abandoned case are unit-testable.
pub(crate) fn classify_last_kg_sync(
    row: Option<&crate::db::kg_syncs::KgSyncRow>,
    now_ms: i64,
    stale_secs: u64,
) -> LastKgSync {
    use crate::db::kg_syncs::{heartbeat_is_stale, status};
    let Some(row) = row else {
        return LastKgSync::Absent;
    };
    match row.status.as_str() {
        status::SUCCESS | status::SKIPPED => LastKgSync::Succeeded,
        status::FAILED => LastKgSync::Failed { status: row.status.clone() },
        status::RUNNING | status::PENDING => {
            if heartbeat_is_stale(row.heartbeat_at, row.started_at, now_ms, stale_secs) {
                LastKgSync::Failed {
                    status: format!("{} (abandoned — no liveness stamp)", row.status),
                }
            } else {
                LastKgSync::InFlight { status: row.status.clone() }
            }
        }
        // An unknown status is not evidence of success. Treat it the way the
        // rest of this file treats "could not determine": as owed work.
        other => LastKgSync::Failed { status: other.to_string() },
    }
}

/// What a drift probe concluded. `Unavailable` is a first-class answer, not a
/// flavour of `Ok`: the defect class here is "absence of evidence read as
/// evidence of absence".
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum DriftVerdict {
    /// Weaviate is missing, or holds stale copies of, at least one node.
    Drift { missing: usize, stale: usize, scanned: usize },
    /// Every on-disk node is present in Weaviate at the current content hash.
    Ok { scanned: usize },
    /// No verdict (no binding, Weaviate down, wrapper failed, unparseable
    /// output). Carries WHY.
    Unavailable { detail: String },
}

/// Why a kg-sync is being spawned — carried so the log line can be specific.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum KgSyncSpawnReason {
    InitialCreate,
    ContentChanged,
    /// No `kg_syncs` row, or its last status is `failed`.
    NeverSucceeded { last_status: String },
    DriftDetected { missing: usize, stale: usize, scanned: usize },
}

/// The gate's verdict. `Skip` is split so the caller can be honest about which
/// kind of "not spawning" this is.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum KgSyncDecision {
    Spawn(KgSyncSpawnReason),
    /// Positively confirmed: nothing on disk changed AND Weaviate has it all.
    SkipConfirmed { scanned: usize },
    /// Nothing on disk changed, but the store's contents could NOT be
    /// confirmed. Logged at WARN — never phrased as "every node present".
    SkipUnverified { detail: String },
    /// A live sync is already running/queued for this project. Not a verdict
    /// about the store at all — and deliberately reached WITHOUT probing, so a
    /// mid-sync probe cannot report the not-yet-written nodes as drift.
    SkipInFlight { status: String },
}

/// THE kg-sync-on-bundle decision (v0.2.94). Pure; every input is a value the
/// caller resolved.
///
/// `drift` is `None` when the probe was not run — correct and cheap whenever an
/// earlier leg already decided to spawn (see [`kg_sync_needs_drift_probe`]).
/// `None` reaching the drift leg means "no verdict", and lands in
/// `SkipUnverified`.
pub(crate) fn decide_kg_sync_on_bundle(
    is_initial_create: bool,
    kg_or_docs_content_changed: bool,
    last_kg_sync: &LastKgSync,
    drift: Option<DriftVerdict>,
) -> KgSyncDecision {
    // Legs 1-2 are the v0.2.71 gate, unchanged and still the whole answer when
    // it says "spawn". It is CALLED rather than re-implemented so the two can
    // never disagree — this function extends it, it does not replace it.
    //
    // They sit AHEAD of the in-flight check on purpose: a bundle that just
    // wrote knowledge/docs bytes is NEW work an already-running sync may have
    // walked past, and that spawn is v0.2.71 behaviour this change must not
    // regress. The single-flight permit (`acquire_kg_sync_permit`) makes it a
    // queue, not a race.
    if should_spawn_kg_sync_on_bundle(is_initial_create, kg_or_docs_content_changed) {
        return KgSyncDecision::Spawn(if is_initial_create {
            KgSyncSpawnReason::InitialCreate
        } else {
            KgSyncSpawnReason::ContentChanged
        });
    }
    // A LIVE sync is already driving this project. Stop here — before the
    // probe, not just before the spawn: a probe taken mid-sync sees the
    // not-yet-written nodes as missing and would manufacture the very "drift"
    // that queues a redundant second run.
    if let LastKgSync::InFlight { status } = last_kg_sync {
        return KgSyncDecision::SkipInFlight { status: status.clone() };
    }
    // A project that has never succeeded is owed a sync no matter what the
    // bundle touched. A `running` row whose task died reaches here too — see
    // `classify_last_kg_sync`.
    match last_kg_sync {
        LastKgSync::Absent => {
            return KgSyncDecision::Spawn(KgSyncSpawnReason::NeverSucceeded {
                last_status: "absent".to_string(),
            })
        }
        LastKgSync::Failed { status } => {
            return KgSyncDecision::Spawn(KgSyncSpawnReason::NeverSucceeded {
                last_status: status.clone(),
            })
        }
        LastKgSync::Succeeded | LastKgSync::InFlight { .. } => {}
    }
    match drift {
        Some(DriftVerdict::Drift { missing, stale, scanned }) if missing + stale > 0 => {
            KgSyncDecision::Spawn(KgSyncSpawnReason::DriftDetected { missing, stale, scanned })
        }
        // A `Drift` verdict carrying zero counts is a contradiction; treat it
        // as "no verdict" rather than invent a clean bill of health.
        Some(DriftVerdict::Drift { .. }) => KgSyncDecision::SkipUnverified {
            detail: "drift check reported drift with no drifted nodes".to_string(),
        },
        Some(DriftVerdict::Ok { scanned }) => KgSyncDecision::SkipConfirmed { scanned },
        Some(DriftVerdict::Unavailable { detail }) => KgSyncDecision::SkipUnverified { detail },
        None => KgSyncDecision::SkipUnverified {
            detail: "drift check was not run".to_string(),
        },
    }
}

/// The USER-FACING warning a decision owes, if any. `None` for every decision
/// that is either an action (a spawn) or a positively-confirmed skip.
///
/// Only `SkipUnverified` produces one, and it must: the update otherwise
/// reports "complete" while the single check that could have contradicted it
/// never answered. A `tracing::warn!` alone is not a user surface — the launcher
/// toast reads `warnings`.
pub(crate) fn kg_sync_decision_warning(decision: &KgSyncDecision) -> Option<String> {
    match decision {
        KgSyncDecision::SkipUnverified { detail } => Some(format!(
            "kg-sync skipped: on-disk knowledge/docs unchanged, but the drift \
             check could NOT confirm Weaviate holds every node ({}). Run \
             `.claude/scripts/kg-sync --check-drift` in the project to see what \
             is actually stored.",
            detail
        )),
        KgSyncDecision::Spawn(_)
        | KgSyncDecision::SkipConfirmed { .. }
        | KgSyncDecision::SkipInFlight { .. } => None,
    }
}

/// Would the decision still be open without a drift verdict? Only then is the
/// (cheap, but non-zero) probe worth running.
pub(crate) fn kg_sync_needs_drift_probe(
    is_initial_create: bool,
    kg_or_docs_content_changed: bool,
    last_kg_sync: &LastKgSync,
) -> bool {
    matches!(
        decide_kg_sync_on_bundle(
            is_initial_create,
            kg_or_docs_content_changed,
            last_kg_sync,
            None,
        ),
        KgSyncDecision::SkipUnverified { .. }
    )
}

#[cfg(test)]
mod v0294_gate_tests {
    use super::*;
    use crate::db::kg_syncs::{status, KgSyncRow};

    const STALE_SECS: u64 = 600;
    const NOW: i64 = 1_700_000_000_000;

    fn row(status: &str, heartbeat_at: Option<i64>, started_at: Option<i64>) -> KgSyncRow {
        KgSyncRow {
            project_id: "p".to_string(),
            status: status.to_string(),
            started_at,
            finished_at: None,
            duration_ms: None,
            kg_total: 0,
            kg_succeeded: 0,
            kg_failed: 0,
            docs_total: 0,
            docs_succeeded: 0,
            docs_failed: 0,
            error_message: None,
            log_tail: None,
            heartbeat_at,
        }
    }

    /// Classify a row of `status` whose liveness stamp is `age_secs` old.
    fn classify(status: &str, age_secs: i64) -> LastKgSync {
        let stamp = NOW - age_secs * 1000;
        classify_last_kg_sync(Some(&row(status, Some(stamp), Some(stamp))), NOW, STALE_SECS)
    }

    fn ok_status() -> LastKgSync {
        LastKgSync::Succeeded
    }

    #[test]
    fn create_always_spawns() {
        assert_eq!(
            decide_kg_sync_on_bundle(
                true, false, &ok_status(), Some(DriftVerdict::Ok { scanned: 9 })
            ),
            KgSyncDecision::Spawn(KgSyncSpawnReason::InitialCreate)
        );
    }

    #[test]
    fn content_change_spawns_without_needing_a_probe() {
        assert_eq!(
            decide_kg_sync_on_bundle(false, true, &ok_status(), None),
            KgSyncDecision::Spawn(KgSyncSpawnReason::ContentChanged)
        );
        assert!(!kg_sync_needs_drift_probe(false, true, &ok_status()));
    }

    /// THE never-succeeded LEG: `failed` since 2026-09-05, two bundle
    /// updates since, each skipped on "files unchanged".
    #[test]
    fn a_never_succeeded_project_spawns_even_with_nothing_touched() {
        assert_eq!(
            decide_kg_sync_on_bundle(false, false, &classify(status::FAILED, 0), None),
            KgSyncDecision::Spawn(KgSyncSpawnReason::NeverSucceeded {
                last_status: status::FAILED.to_string()
            })
        );
        assert_eq!(
            decide_kg_sync_on_bundle(false, false, &LastKgSync::Absent, None),
            KgSyncDecision::Spawn(KgSyncSpawnReason::NeverSucceeded {
                last_status: "absent".to_string()
            })
        );
        // ...and no probe is needed to know it.
        assert!(!kg_sync_needs_drift_probe(false, false, &classify(status::FAILED, 0)));
        assert!(!kg_sync_needs_drift_probe(false, false, &LastKgSync::Absent));
    }

    /// THE drift LEG: last sync succeeded, bundle touched nothing, but the
    /// store is missing 67 of 78 nodes.
    #[test]
    fn drift_spawns() {
        assert_eq!(
            decide_kg_sync_on_bundle(
                false,
                false,
                &ok_status(),
                Some(DriftVerdict::Drift { missing: 67, stale: 5, scanned: 78 }),
            ),
            KgSyncDecision::Spawn(KgSyncSpawnReason::DriftDetected {
                missing: 67,
                stale: 5,
                scanned: 78
            })
        );
    }

    #[test]
    fn a_clean_store_is_the_only_confirmed_skip() {
        assert_eq!(
            decide_kg_sync_on_bundle(
                false, false, &ok_status(), Some(DriftVerdict::Ok { scanned: 78 }),
            ),
            KgSyncDecision::SkipConfirmed { scanned: 78 }
        );
    }

    /// The rule the whole change exists for: an unavailable check must never
    /// be reported as "Weaviate has every node".
    #[test]
    fn an_unavailable_check_is_never_a_confirmed_skip() {
        for verdict in [
            Some(DriftVerdict::Unavailable { detail: "weaviate unreachable".into() }),
            Some(DriftVerdict::Drift { missing: 0, stale: 0, scanned: 12 }),
            None,
        ] {
            let got = decide_kg_sync_on_bundle(false, false, &ok_status(), verdict.clone());
            assert!(
                matches!(got, KgSyncDecision::SkipUnverified { .. }),
                "{:?} must skip UNVERIFIED, got {:?}",
                verdict,
                got
            );
        }
    }

    /// A probe is worth running exactly when nothing else has decided.
    #[test]
    fn probe_is_run_only_when_the_decision_is_still_open() {
        assert!(kg_sync_needs_drift_probe(false, false, &ok_status()));
        assert!(!kg_sync_needs_drift_probe(true, false, &ok_status()));
    }

    // ── R6/1: a LIVE sync is not a store verdict, and not a spawn ──────────

    /// The regression this closes. `pending` / `running` used to fall through
    /// to the DRIFT leg, and a probe taken MID-SYNC sees the not-yet-written
    /// nodes as missing → `Drift` → a second `kg-sync --all` queued, which
    /// `run_sync_task` then re-marks RUNNING unconditionally, restarting the
    /// GUI's progress for no reason.
    ///
    /// Asserts the DECISION, not merely the probe flag: skipping the probe and
    /// still spawning would be just as wrong.
    #[test]
    fn in_flight_statuses_do_not_re_spawn() {
        for s in [status::PENDING, status::RUNNING] {
            let live = classify(s, 5); // stamped 5 s ago — plainly alive
            assert_eq!(
                live,
                LastKgSync::InFlight { status: s.to_string() },
                "a freshly-stamped {} row is a LIVE task",
                s
            );
            assert_eq!(
                decide_kg_sync_on_bundle(false, false, &live, None),
                KgSyncDecision::SkipInFlight { status: s.to_string() },
                "{} must not re-spawn",
                s
            );
            // Even handed a drift verdict it must not spawn — the in-flight
            // check sits AHEAD of the drift leg precisely so that (spurious)
            // verdict is never taken.
            assert_eq!(
                decide_kg_sync_on_bundle(
                    false,
                    false,
                    &live,
                    Some(DriftVerdict::Drift { missing: 40, stale: 0, scanned: 40 }),
                ),
                KgSyncDecision::SkipInFlight { status: s.to_string() }
            );
            assert!(
                !kg_sync_needs_drift_probe(false, false, &live),
                "{} must not even pay for the probe",
                s
            );
        }
    }

    /// ...but a content change STILL spawns during a live sync: that is
    /// v0.2.71 behaviour (new bytes the running walk may already have passed),
    /// and the single-flight permit makes the second run a queue entry, not a
    /// race. The in-flight check must not silently widen into a suppressor.
    #[test]
    fn a_content_change_still_spawns_during_a_live_sync() {
        let live = classify(status::RUNNING, 5);
        assert_eq!(
            decide_kg_sync_on_bundle(false, true, &live, None),
            KgSyncDecision::Spawn(KgSyncSpawnReason::ContentChanged)
        );
    }

    /// A `running` row whose liveness stamp expired belongs to a DEAD task.
    /// Reading it raw would block the repair for up to one sweeper interval
    /// (300 s) — the "never succeeded, never retried" state this gate exists to
    /// end. `heartbeat_is_stale` is the ONE home for that judgement, and this
    /// asserts it is applied HERE and not only in `get_kg_sync_status`.
    #[test]
    fn an_abandoned_running_row_is_treated_as_failed() {
        let dead = classify(status::RUNNING, STALE_SECS as i64 + 60);
        match &dead {
            LastKgSync::Failed { status } => assert!(
                status.contains(status::RUNNING) && status.contains("abandoned"),
                "the classification must SAY it was abandoned, got {:?}",
                status
            ),
            other => panic!("a stale running row must classify Failed, got {:?}", other),
        }
        assert!(matches!(
            decide_kg_sync_on_bundle(false, false, &dead, None),
            KgSyncDecision::Spawn(KgSyncSpawnReason::NeverSucceeded { .. })
        ));
        assert!(!kg_sync_needs_drift_probe(false, false, &dead));
    }

    /// A `running` row with NO stamps at all (pre-migration legacy) reads as
    /// abandoned, not live: "cannot show liveness" is not evidence of liveness.
    #[test]
    fn a_running_row_with_no_stamps_is_not_taken_as_live() {
        let ghost =
            classify_last_kg_sync(Some(&row(status::RUNNING, None, None)), NOW, STALE_SECS);
        assert!(matches!(ghost, LastKgSync::Failed { .. }));
    }

    // ── R6/5: an unconfirmed skip must reach the USER, not just the log ────

    #[test]
    fn only_an_unverified_skip_warns_the_user() {
        let unverified = decide_kg_sync_on_bundle(
            false,
            false,
            &ok_status(),
            Some(DriftVerdict::Unavailable { detail: "weaviate unreachable".into() }),
        );
        let msg = kg_sync_decision_warning(&unverified)
            .expect("an unconfirmed skip owes the user a warning");
        assert!(msg.contains("could NOT confirm"), "{}", msg);
        assert!(msg.contains("weaviate unreachable"), "the WHY must survive: {}", msg);
        assert!(msg.contains("--check-drift"), "and it must say what to run: {}", msg);
        assert!(
            !msg.contains("nothing to re-embed"),
            "an unconfirmed skip must never claim the store is complete: {}",
            msg
        );

        // Everything else is either an action or positively confirmed.
        for quiet in [
            decide_kg_sync_on_bundle(
                false, false, &ok_status(), Some(DriftVerdict::Ok { scanned: 3 }),
            ),
            decide_kg_sync_on_bundle(false, true, &ok_status(), None),
            decide_kg_sync_on_bundle(false, false, &classify(status::RUNNING, 5), None),
        ] {
            assert_eq!(
                kg_sync_decision_warning(&quiet),
                None,
                "{:?} must not warn the user",
                quiet
            );
        }
    }

    #[test]
    fn terminal_statuses_classify_as_expected() {
        assert_eq!(classify(status::SUCCESS, 0), LastKgSync::Succeeded);
        assert_eq!(classify(status::SKIPPED, 0), LastKgSync::Succeeded);
        assert_eq!(classify_last_kg_sync(None, NOW, STALE_SECS), LastKgSync::Absent);
        // An unrecognised status is owed work, never silent success.
        assert!(matches!(classify("weird", 0), LastKgSync::Failed { .. }));
    }
}

