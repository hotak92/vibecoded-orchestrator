#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

# v0.2.18 (Commit 11): surface embedding-backend failure hints to Claude.
#
# Purpose
#   When EmbeddingService.for_project() fails because no backend is reachable,
#   vco_lib/embedding_service.py writes a Claude-readable hint to
#   .claude/context/EMBEDDING_FAILURES.md (and a JSONL diagnostic to
#   <metrics dir>/embedding_failures.jsonl). The MD file is auto-cleared
#   the next time construction succeeds. This SessionStart hook surfaces the
#   hint (when it exists) into the current Claude Code session so the LLM
#   has immediate context about the broken state.
#
# v0.2.92 (W4) second surface: the OUTAGE banner is blind to everything
#   that is not a construction-time NoEmbeddingBackendError. The
#   shrink-on-refusal path (a real fidelity loss on the ACTIVE slot,
#   previously disclosed only by a WARNING to stderr nobody reads) and the
#   floor refusal (slot got NO vector) are meant to reach the SAME jsonl as
#   "kind": "shrink_summary" rows — ONE row per run, never per chunk. The
#   fidelity leg below surfaces those rows as a SEPARATE, lower-severity
#   notice ("NOT an outage").
#
#   BEFORE CREDITING THE LEG, CHECK BOTH HALVES. Reader: this hook, the
#   .ps1 sibling and `vco_lib.embedding_fidelity`. Writers: kg-sync for
#   outage rows, and `vco_lib/embedding_service.py::_embed_shrinking_on_
#   overflow` (via its `_note_fidelity` guard) for shrink/floor rows — the
#   ONE loop all four embed legs route through. Both halves were live as of
#   v0.2.92; the shrink writer spent part of that cycle UNWIRED while this
#   comment credited it, so re-run the grep in that module's "Wiring status"
#   docstring rather than trusting this paragraph. Silence here means
#   "no NEW summary row", never "nothing shrank" — and, since v0.2.92
#   MAJOR-3, never "the reader is broken" either: a failed spawn prints a
#   named diagnostic on stdout instead of disappearing into /dev/null.
#   The logic lives in the Python module (the A-leg of the cross-language
#   rule: one implementation, thin .sh/.ps1 wrappers both spawning
#   `python -m vco_lib.embedding_fidelity notice`). A per-project marker
#   (.claude/state/embedding-fidelity.seen) deduplicates: the notice fires
#   once per NEW run summary, not on every session. The size gate keeps the
#   common nothing-new session at one `wc -c` — no python spawn.
#
# Second job (v0.2.92 W7): this is the SessionStart hook that owns the metrics
#   directory, so it is also where the once-per-machine COPY of the legacy
#   ~/.claude/metrics archive into ~/.vct/metrics is triggered
#   (`vco_metrics_migrate_once`). It costs one directory test on a machine with
#   nothing to copy and one file test after the first success; the Python is
#   spawned only while a copy is genuinely owed. Deliberately BEFORE the
#   early exits below, because the copy is owed whether or not an embedding
#   failure happened to be recorded. `install.py` also runs it, but an
#   installed project must not have to wait for the next orchestrator update
#   to get its history moved.
#
# Idempotent — safe to run on every SessionStart even when there's nothing
# to surface. Soft-fails throughout; never blocks SessionStart. The zero-output
# property is preserved: the migration prints nothing on success and its
# failures are swallowed.
#
# The user MAY ask Claude to investigate the detailed JSONL log; the hook
# points at the absolute path so it's a one-tool-call away.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Metrics home + the once-per-machine archive COPY. `_lib/metrics-dir.sh` is
# the ONE shell-side resolver (sibling: `_lib/metrics-dir.ps1`). A missing
# helper leaves both empty and the hook still surfaces the hint — it just has
# no absolute path to name.
JSONL_PATH=""
if [ -f "$SCRIPT_DIR/_lib/metrics-dir.sh" ]; then
    # shellcheck source=_lib/metrics-dir.sh disable=SC1091
    . "$SCRIPT_DIR/_lib/metrics-dir.sh"
    vco_metrics_migrate_once "$SCRIPT_DIR" 2>/dev/null || true
fi

# Discover install root — same anchor convention as ensure-containers.sh.
# $CLAUDE_PROJECT_DIR is set by Claude Code; git toplevel is the fallback.
INSTALL_ROOT="${CLAUDE_PROJECT_DIR:-}"
if [ -z "$INSTALL_ROOT" ]; then
    INSTALL_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
fi
if [ -z "$INSTALL_ROOT" ]; then
    # No project context — nothing to do (running outside any VCO project).
    exit 0
fi

HINT_FILE="$INSTALL_ROOT/.claude/context/EMBEDDING_FAILURES.md"

# ── v0.2.92 W4: fidelity leg (shrink summaries / floor refusals) ────────────
# Runs for every project with a metrics jsonl, INDEPENDENT of the outage MD:
# the two are different severities with different writers. Cheap size gate —
# only when the jsonl has grown past this project's marker do we spawn the
# python that renders (and re-writes) the notice.
FIDELITY_JSONL=""
if command -v vco_metrics_read_file >/dev/null 2>&1; then
    FIDELITY_JSONL="$(vco_metrics_read_file "embedding_failures.jsonl" 2>/dev/null || printf '')"
fi
if [ -n "$FIDELITY_JSONL" ] && [ -f "$FIDELITY_JSONL" ]; then
    MARKER="$INSTALL_ROOT/.claude/state/embedding-fidelity.seen"
    SEEN=0
    if [ -f "$MARKER" ]; then
        SEEN="$(sed -n 's/.*"offset":[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$MARKER" 2>/dev/null || echo 0)"
    fi
    SIZE="$(wc -c < "$FIDELITY_JSONL" 2>/dev/null || echo 0)"
    case "$SIZE" in ''|*[!0-9]*) SIZE=0 ;; esac
    case "$SEEN" in ''|*[!0-9]*) SEEN=0 ;; esac
    if [ "$SIZE" -gt "$SEEN" ]; then
        # Resolve the VCO venv python (same shared helper the other hooks use;
        # NEVER the user's project venv — vco_lib must be importable).
        VCO_VENV_PYTHON=""
        if [ -f "$SCRIPT_DIR/_lib/resolve-vco-venv.sh" ]; then
            # shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
            . "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
            resolve_vco_venv_python "$SCRIPT_DIR" 2>/dev/null || true
        fi
        if [ -n "$VCO_VENV_PYTHON" ]; then
            # v0.2.92 MAJOR-3 — FAILURE MUST BE DISCOVERABLE.
            #
            # This call used to end in `2>/dev/null || true`. "A hook must
            # never break the session" is right; "a hook may fail invisibly
            # forever" is a different thing, and that is what the old form
            # bought: on a machine in the documented shadow-copy state (a
            # stale non-editable `vco_lib` in site-packages, or a venv that
            # predates this module) the spawn dies with `No module named
            # vco_lib.embedding_fidelity`, the marker is never advanced, and
            # the ONE surface built to report embedding-fidelity loss goes
            # quiet permanently with nobody able to tell that it has.
            #
            # So: stderr is captured (fd swap — stdout keeps flowing straight
            # through, because THAT is the notice), and a non-zero exit is
            # reported as a few stdout lines. Stdout, not stderr, because
            # Claude Code injects a SessionStart hook's stdout as a
            # system-reminder and DISCARDS its stderr on exit 0 — a diagnostic
            # on stderr would be exactly as invisible as no diagnostic.
            #
            # It repeats every session while the breakage lasts: the marker
            # only advances on success, so the state is genuinely unresolved
            # and re-reporting it is the point. It self-clears the first time
            # the import works.
            #
            # fd 3 must be duplicated from the hook's REAL stdout BEFORE the
            # command substitution opens its capture pipe, or `1>&3` would
            # redirect the notice into the very variable meant for stderr.
            exec 3>&1
            FIDELITY_ERR="$("$VCO_VENV_PYTHON" -m vco_lib.embedding_fidelity notice \
                --project-root "$INSTALL_ROOT" 2>&1 1>&3)"
            FIDELITY_RC=$?
            exec 3>&-
            if [ "$FIDELITY_RC" -ne 0 ]; then
                FIDELITY_LAST="$(printf '%s\n' "$FIDELITY_ERR" \
                    | grep -v '^[[:space:]]*$' | tail -n 1)"
                echo "[embedding-failures-surface] the embedding-fidelity notice could not run (exit $FIDELITY_RC)."
                echo "  interpreter: $VCO_VENV_PYTHON"
                echo "  error:       ${FIDELITY_LAST:-(no stderr)}"
                echo "  Until this is fixed, shrink/refusal fidelity loss is recorded but never reported."
                echo "  Usually a broken or stale vco_lib install: run \`python install.py --update\`"
                echo "  from the orchestrator root, then check \`python -c 'import vco_lib; print(vco_lib.__file__)'\`"
                echo "  prints a path inside that checkout."
            fi
        else
            # The OTHER way this leg goes permanently quiet: no interpreter to
            # spawn at all. There used to be no `else` here at all, which made
            # it indistinguishable from "nothing to report". Rows are owed and cannot be rendered,
            # so say so. (Rare by construction: the jsonl is written by
            # vco_lib itself, so a machine with rows normally has a venv that
            # can import it — which is exactly why an unreported failure here
            # would be so hard to diagnose.)
            echo "[embedding-failures-surface] the embedding-fidelity notice could not run (no VCO venv resolved)."
            echo "  Probed \$VCT_VENV, \$VCT_INSTALL_ROOT and the clone-relative fallback; none held a usable python."
            echo "  Until this is fixed, shrink/refusal fidelity loss is recorded but never reported."
            echo "  Remedy: start the session from the launcher (it exports VCT_INSTALL_ROOT), or point"
            echo "  \$VCT_VENV at the orchestrator's venv."
        fi
    fi
fi

if [ ! -f "$HINT_FILE" ]; then
    # No outage recorded — the fidelity leg above already ran; silent exit
    # (idempotent zero-output path when nothing new was surfaced either).
    exit 0
fi

# Name the log Claude should read. Prefer an EXISTING file, new home before the
# frozen archive, so a user whose modified hooks still append to ~/.claude —
# or one whose copy has not run yet — is pointed at the file that actually has
# their rows, not at a path that does not exist. Falls back to the write target
# when neither exists yet (the path is informational; existence is not
# required here).
if command -v vco_metrics_read_file >/dev/null 2>&1; then
    JSONL_PATH="$(vco_metrics_read_file "embedding_failures.jsonl" 2>/dev/null || printf '')"
    if [ -z "$JSONL_PATH" ] && [ -n "${VCO_METRICS_DIR:-}" ]; then
        JSONL_PATH="$VCO_METRICS_DIR/embedding_failures.jsonl"
    fi
fi
[ -n "$JSONL_PATH" ] || JSONL_PATH="(metrics directory not resolvable)"

# Surface to Claude via stdout — SessionStart hook stdout is injected as a
# system-reminder. Print path + full hint body so the LLM sees both the
# pointer and the diagnostic in-context.
echo ""
echo "==================================================================="
echo "Embedding-backend failure recorded since last successful run."
echo "Claude: read this hint and (if asked) investigate the JSONL log."
echo ""
echo "Hint file:    $HINT_FILE"
echo "Detail log:   $JSONL_PATH"
echo "==================================================================="
echo ""
cat "$HINT_FILE" 2>/dev/null || echo "(hint file became unreadable between check and cat)"
echo ""
echo "==================================================================="
echo ""

exit 0
