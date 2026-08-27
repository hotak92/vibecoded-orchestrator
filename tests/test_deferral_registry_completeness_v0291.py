# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 WP-B — the deferral-registry COMPLETENESS gate.

Three hard invariants, all source-scanned so they hold for code nobody
remembered to think about:

1. **Every emitted condition_id is registered.** A cid shipping without a
   declared class/owner/clear mechanism is exactly how the ledger silted up:
   nothing could enumerate the conditions, so nothing could notice a new one
   arriving with no lifecycle. This is a HARD failure — no warn-only grace
   period (zero-deferral rule).

2. **No raw `DeferralReport.read/add_entry/write` triplet outside the emitter
   home.** v0.2.83 WP-B1 routed every writer through the locked emitter; ONE
   shipped writer (`sync_knowledge_graph.py`) still bypassed it until v0.2.91,
   and it was the worst possible one — a subprocess writing while install.py's
   own finalize was live. This scan is what stops it coming back.

3. **Every declared `probe:py:<name>` resolves to a real callable.** A registered
   cid whose documented clear protocol does not exist is the
   "documented-protocol-never-implemented" failure class (v0.2.54 Track D found
   two; v0.2.90 still shipped two more). Here it is a test failure.

Plus a MIGRATION PIN: the registry-derived install-ownership set must be a
strict superset of the hand-written v0.2.90 one, with only the deliberate
v0.2.91 additions. That is what proves the constants moved without losing an
id — the failure mode that would silently disable self-clearing for whatever
got dropped.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Directories the scan never walks: tests (fixtures deliberately use fake cids),
# build output, vendored deps.
_SKIP_DIR_PARTS = {
    ".git", ".venv", "venv", "node_modules", "target", "__pycache__",
    "tests", "dist", "build", ".mypy_cache", ".ruff_cache", ".claude",
}

# The emitter home modules. Their docstrings carry worked EXAMPLES using real
# cids, and (for deferral_report) the triplet is the implementation itself.
_EMITTER_HOME_FILES = {
    Path("vco_lib/deferral_report.py"),
    Path("vco_lib/deferral_emit.py"),
}

# Python emit shapes.
_PY_LITERAL = re.compile(
    r"""condition_id\s*=\s*(?P<f>f?)(?P<q>['"])(?P<val>[^'"]*)(?P=q)"""
)
_PY_NAME = re.compile(
    r"""condition_id\s*=\s*(?P<name>[A-Za-z_][A-Za-z0-9_.]*)\s*[,)]"""
)
_PY_CONST = re.compile(
    r"""^\s*(?P<name>[A-Z_][A-Z0-9_]*)\s*(?::\s*[^=\n]+)?=\s*(?P<q>['"])(?P<val>[^'"]*)(?P=q)""",
    re.M,
)

# Rust emit shapes.
_RS_LITERAL = re.compile(r"""condition_id\s*:\s*"(?P<val>[^"]*)"\s*,""")
_RS_NAME = re.compile(r"""condition_id\s*:\s*(?P<name>[A-Z_][A-Z0-9_]*)\s*,""")
_RS_CONST = re.compile(
    r"""const\s+(?P<name>[A-Z_][A-Z0-9_]*)\s*:\s*&(?:'static\s+)?str\s*=\s*"(?P<val>[^"]*)"\s*;"""
)
# `storage_ux::emit_deferral` takes the cid as its first positional argument.
_RS_EMIT_DEFERRAL = re.compile(r"""emit_deferral\(\s*\n?\s*"(?P<val>[^"]*)"\s*,""")
# `git_user_editable_merge` renders one deferral's Markdown by hand.
_RS_MD_SECTION = re.compile(
    r"""##\s+(?P<val>[a-z0-9_]+)\s+\((?:critical|warning|info)\)"""
)

# The v0.2.90 hand-written ownership set, verbatim from `install.py` at
# f97659a4. The migration must not lose any of these.
_V0290_OWNED_IDS = frozenset({
    "autostash_pop_conflict", "boot_service_path_repaired",
    "claude_settings_unparseable", "claude_settings_user_modified_preserved",
    "compose_overlay_ambiguous", "deprecated_mcp_removal_declined",
    "deprecated_mcp_removal_lock_failed", "deprecated_mcp_removal_quiet_skipped",
    "deprecated_mcp_removal_summary", "deprecated_mcp_removal_write_failed",
    "dual_ollama_detected", "env_secrets_hub_migration_failed",
    "env_secrets_hub_migration_partial", "env_secrets_retained_in_plaintext",
    "global_lean_ctx_hooks_detected", "kg_binding_self_heal_db_error",
    "kg_binding_self_healed", "launcher_binary_swap_failed_locked",
    "launcher_install_path_seed_unavailable", "launcher_restart_required",
    "legacy_shared_kg_class_present", "links_to_property_schema_drift",
    "mcp_registration_failed", "mcp_registration_no_venv",
    "mcp_registration_python_fallback", "multi_candidate_prefix_adopt",
    "ollama_mcp_deprecated", "orchestrator_root_kg_collection_locked",
    "orchestrator_self_user_modified_preserved",
    "orphan_orchestrator_development_collection", "podman_daemon_start_failed",
    "rebuild_pending_seed", "schema_drift_rebuild_required",
    "search_mcp_simplified", "searxng_removed_from_default_install",
    "stale_mcp_entry", "stale_mcp_json_restore_detected",
    "stale_mcp_json_shadow_quarantined", "stale_mcp_rewrite_declined",
    "stale_mcp_rewrite_quiet_skipped", "stale_mcp_rewrite_summary",
    "untracked_collision_divergent", "update_resume_required",
    "vct_hub_binary_unavailable", "weaviate_unreachable_at_update",
})

# Files that STILL carry the raw `DeferralReport.read/add_entry/write` triplet.
#
# EMPTY as of v0.2.91 wave-2 — every shipped writer now routes through
# `vco_lib.deferral_emit`.
#
# History (why the set exists rather than a flat "no offenders" assertion):
# the v0.2.83 census claimed exactly one bypass survived; the v0.2.91 scan
# found THREE — which is the point of having a scan instead of a census.
# WP-B closed `templates/scripts/sync_knowledge_graph.py` (the dangerous one:
# it writes as a subprocess while install.py's own finalize is live), and the
# wave-2 completion pass closed the last two:
#
#   * templates/scripts/analyze_code_graph.py
#     (`code_graph_no_embedding_backend` / `code_graph_code_backend_unreachable`)
#   * claude_mcp_servers/weaviate_mcp/server.py
#     (`gate_skipped_no_project_id`)
#
# The set may only SHRINK — see the test for the ratchet's two directions.
# With it empty, ANY file carrying the triplet is now a hard failure.
_KNOWN_UNLOCKED_WRITERS: frozenset = frozenset()

_V0290_OWNED_PREFIXES = (
    "bundle_pin_drift_", "deprecated_mcp_", "kg_named_vector_slot_error_",
    "lowercase_codegraph_residual_", "schema_migration_failed_",
    "schema_migration_required_", "stale_unit_retired_",
)

# The record-class cids v0.2.91 DELIBERATELY promoted to install ownership so
# their one-shot records auto-expire on the next update instead of requiring a
# manual dismissal. Any OTHER addition must be justified in the same commit.
_V0291_OWNED_ADDITIONS = frozenset({
    "codegraph_binding_repaired",
    "hard_cut_performed",
    "kg_access_phantom_repaired",
    "launcher_binary_clobber_averted",
    # WP-D: the doctor phase runs at the END of every install/update run, so
    # its cid is re-detected exactly like any other install.py-emitted
    # condition — installing Node makes the entry disappear on the next run
    # with no dismissal. That is family A by construction, not a record-class
    # exception: the emitter runs INSIDE the install.py run whose finalize
    # rebuilds the ledger, and it emits into that run's own report (sink=),
    # never behind finalize's back.
    "npx_missing_mcp_unspawnable",
})


def _iter_source_files(suffixes):
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        rel = path.relative_to(REPO_ROOT)
        if any(part in _SKIP_DIR_PARTS for part in rel.parts[:-1]):
            continue
        yield rel, path


def scan_emitted_condition_ids() -> dict:
    """Map every emitted condition id (or glob family) → ``["file:line", …]``.

    f-string / const interpolations collapse to a ``*`` wildcard so a
    dynamically-suffixed family matches its registry glob row.
    """
    found: dict = {}

    def add(cid: str, where: str) -> None:
        found.setdefault(cid, []).append(where)

    for rel, path in _iter_source_files({".py"}):
        if rel in _EMITTER_HOME_FILES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        consts = {m.group("name"): m.group("val") for m in _PY_CONST.finditer(text)}
        for m in _PY_LITERAL.finditer(text):
            line = text[: m.start()].count("\n") + 1
            val = m.group("val")
            if m.group("f"):
                def _sub(mm, _consts=consts):
                    inner = mm.group(1).split("(")[0].split(".")[0].strip()
                    return _consts.get(inner, "*")
                val = re.sub(r"\{([^}]*)\}", _sub, val)
            add(val, f"{rel}:{line}")
        for m in _PY_NAME.finditer(text):
            base = m.group("name").split(".")[-1]
            if base in consts:
                line = text[: m.start()].count("\n") + 1
                add(consts[base], f"{rel}:{line}")

    for rel, path in _iter_source_files({".rs"}):
        text = path.read_text(encoding="utf-8", errors="replace")
        # Everything from the first `#[cfg(test)]` is test scaffolding whose
        # fixture cids are intentionally fake.
        cut = text.find("#[cfg(test)]")
        if cut != -1:
            text = text[:cut]
        consts = {m.group("name"): m.group("val") for m in _RS_CONST.finditer(text)}
        for pattern in (_RS_LITERAL, _RS_EMIT_DEFERRAL, _RS_MD_SECTION):
            for m in pattern.finditer(text):
                line = text[: m.start()].count("\n") + 1
                add(m.group("val"), f"{rel}:{line}")
        for m in _RS_NAME.finditer(text):
            name = m.group("name")
            if name in consts:
                line = text[: m.start()].count("\n") + 1
                add(consts[name], f"{rel}:{line}")

    return found


class TestRegistryCompleteness(unittest.TestCase):
    def setUp(self):
        from vco_lib import deferral_registry as dr  # noqa: PLC0415

        self.dr = dr

    def test_every_emitted_condition_id_is_registered(self):
        """The gate. A new emit site lands with its registry row or CI is red."""
        emitted = scan_emitted_condition_ids()
        self.assertGreater(
            len(emitted), 80,
            "the scanner found suspiciously few emit sites — a regex probably "
            "stopped matching; fix the scan before trusting a green run",
        )
        missing = {
            cid: sites for cid, sites in sorted(emitted.items())
            if not self.dr.matches_registered_pattern(cid)
        }
        self.assertFalse(
            missing,
            "condition_id(s) emitted but absent from "
            "vco_lib/deferral_conditions.toml:\n"
            + "\n".join(
                f"  {cid}  ({', '.join(sorted(set(sites)))})"
                for cid, sites in missing.items()
            )
            + "\n\nAdd a [conditions.<id>] row declaring class / owner / "
              "clear_probe / emit_surfaces in the SAME commit as the emit site.",
        )

    def test_scanner_finds_the_known_emit_sites(self):
        """Self-check on the SCANNER — a silently-broken regex would make the
        gate above pass vacuously. Pin a sample covering every shape it must
        recognise: a plain Python literal, a Python module constant, an
        f-string family, a Rust literal, a Rust const, and the Rust
        hand-rendered Markdown writer."""
        emitted = scan_emitted_condition_ids()
        for cid in (
            "dual_ollama_detected",             # py literal (install.py)
            "kg_summary_no_backend",            # py module constant
            "stale_unit_retired_*",             # py f-string family
            "kg_access_phantom_repaired",       # rust literal
            "launcher_binary_stale",            # rust const
            "launcher_update_diverged",         # rust hand-rendered markdown
            "kg_sync_no_embedding_backend",     # the template script
        ):
            self.assertIn(
                cid, emitted,
                f"scanner missed {cid!r} — the completeness gate is only as "
                f"good as this scan",
            )

    def test_no_raw_deferral_triplet_outside_the_emitter_home(self):
        """`DeferralReport.read → add_entry → write` may only live in the
        emitter home. Anywhere else it is an UNLOCKED read-modify-write that
        can interleave with a concurrent writer and drop entries.

        RATCHET, not an allowlist: :data:`_KNOWN_UNLOCKED_WRITERS` names the
        files that still carry the triplet at the moment this scan was written.
        The set may only ever SHRINK — a new offender fails, and closing a
        known one without updating the set also fails (so the ratchet cannot
        silently rot into permission). The v0.2.91 WP-B pass closed
        `templates/scripts/sync_knowledge_graph.py`, which was the dangerous
        one: it runs as a subprocess DURING an install run, so its unlocked
        write could interleave with `InstallDeferralFlow.finalize()`'s.
        """
        offenders = []
        for rel, path in _iter_source_files({".py"}):
            if rel in _EMITTER_HOME_FILES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            # Only count real code: `.write(` on a report object combined with
            # a DeferralReport.read in the same file.
            if "DeferralReport.read(" not in text:
                continue
            code = "\n".join(
                ln for ln in text.splitlines()
                if not ln.lstrip().startswith("#")
            )
            if "DeferralReport.read(" not in code:
                continue
            if re.search(r"\breport\.write\(", code) or re.search(
                r"\breport\.add_entry\(", code
            ):
                offenders.append(str(rel))
        new_offenders = sorted(set(offenders) - _KNOWN_UNLOCKED_WRITERS)
        self.assertFalse(
            new_offenders,
            "NEW raw DeferralReport read/add_entry/write triplet outside "
            "vco_lib/deferral_report.py + vco_lib/deferral_emit.py: "
            f"{new_offenders}. Route the write through vco_lib.deferral_emit "
            "(emit / emit_entries / resolve_conditions / locked_report) so it "
            "serializes on the shared file lock — see "
            "templates/scripts/sync_knowledge_graph.py for the migration shape.",
        )
        closed = sorted(_KNOWN_UNLOCKED_WRITERS - set(offenders))
        self.assertFalse(
            closed,
            f"{closed} no longer carries the raw triplet — remove it from "
            "_KNOWN_UNLOCKED_WRITERS so the ratchet keeps tightening.",
        )

    def test_every_declared_python_probe_exists(self):
        """A registered `probe:py:<name>` must resolve to a real callable."""
        from vco_lib.deferral_probes import PROBES  # noqa: PLC0415

        missing = []
        for spec in self.dr.all_specs():
            name = spec.probe_name
            if name is None:
                continue
            if name not in PROBES or not callable(PROBES[name]):
                missing.append(f"{spec.pattern} → probe:py:{name}")
        self.assertFalse(
            missing,
            "registry declares Python probes that do not exist in "
            f"vco_lib.deferral_probes.PROBES: {missing}",
        )

    def test_every_registered_probe_is_actually_dispatched(self):
        """install.py's re-probe pass must route registry probes, and the
        bundle-update reconcile must too. Without both call sites a declared
        probe is a docstring lie again."""
        install_src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
        self.assertIn("_deferral_probes.registry_probe_name(cid)", install_src)
        self.assertIn("_deferral_probes.evaluate(", install_src)
        project_init_src = (
            REPO_ROOT / "vco_lib" / "project_init.py"
        ).read_text(encoding="utf-8")
        self.assertIn("_probe_resolvable_deferrals(folder, report)", project_init_src)
        # Both surfaces route through the SAME dispatcher in vco_lib, so a
        # verdict can never mean different things on the two update paths.
        self.assertIn("resolvable_condition_ids", project_init_src)

    def test_registry_schema_is_valid_and_non_trivial(self):
        specs = self.dr.all_specs()
        self.assertGreater(len(specs), 90, "the registry lost rows")
        for spec in specs:
            self.assertIn(spec.condition_class, self.dr.CLASSES)
            self.assertTrue(spec.owner)
            self.assertTrue(spec.emit_surfaces)
            self.assertIn(spec.status, self.dr.STATUSES)
            if spec.dismiss_key:
                self.assertTrue(all(spec.dismiss_key))

    def test_dismiss_keys_are_declared_only_where_supported(self):
        """Every declared dismiss_key must be resolvable to field VALUES —
        either the emitter attaches them or a fallback provider computes them.
        A key nobody can populate would hash to a constant and make one
        dismissal suppress the condition forever."""
        from vco_lib.deferral_dismissal import _FALLBACK_PROVIDERS  # noqa: PLC0415

        emitter_supplied = {"dual_ollama_detected"}
        for spec in self.dr.all_specs():
            if not spec.dismiss_key:
                continue
            self.assertTrue(
                spec.pattern in _FALLBACK_PROVIDERS
                or spec.pattern in emitter_supplied,
                f"{spec.pattern} declares dismiss_key {spec.dismiss_key} but "
                f"nothing can produce its values",
            )


class TestOwnershipMigrationPin(unittest.TestCase):
    """The registry-derived ownership sets vs the v0.2.90 hand-written ones."""

    def setUp(self):
        from vco_lib import deferral_registry as dr  # noqa: PLC0415

        self.owned = set(dr.install_owned_ids())
        self.prefixes = tuple(dr.install_owned_prefixes())

    def test_no_v0290_owned_id_was_lost(self):
        lost = _V0290_OWNED_IDS - self.owned
        self.assertFalse(
            lost,
            "moving the ownership set into the registry DROPPED ids "
            f"{sorted(lost)} — those conditions would stop self-clearing",
        )

    def test_additions_are_exactly_the_deliberate_ones(self):
        added = self.owned - _V0290_OWNED_IDS
        self.assertEqual(
            added, _V0291_OWNED_ADDITIONS,
            "ownership grants changed. Ownership of a FOREIGN cid means it is "
            "dropped whenever install.py does not re-detect it — intended for "
            "one-shot records, catastrophic for anything whose emitter runs "
            "INSIDE an install.py run. Justify the change and update this pin.",
        )

    def test_prefix_families_unchanged(self):
        self.assertEqual(self.prefixes, _V0290_OWNED_PREFIXES)


if __name__ == "__main__":
    unittest.main()
