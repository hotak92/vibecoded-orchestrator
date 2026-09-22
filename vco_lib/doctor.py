# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``vco doctor`` — ONE probe engine, three invocation points (v0.2.91 WP-D).

The gap this closes (report 6, §B.1 / §C.2): VCO probes its environment ONCE,
at install time, and then never re-verifies the assumptions against what it
actually registered. ``install.py --bootstrap`` computes ``missing_prereqs``
and nothing downstream consumes it; ``_find_npx`` learns npx is missing and
prints a reassurance that is false; the launcher's registration badge is
structurally blind to the npx-less failure. So an environment that drifts (or
was never complete) stays broken silently — for months, in the field.

The engine
----------
:func:`run_doctor` runs a set of PROBES and returns a :class:`DoctorReport`.
It is deliberately **not a new detection codebase**: every probe composes a
mechanism that already exists — with ONE deliberate exception, ``disk_space``,
which measures a resource nothing in VCO was watching at all (see its own
docstring; one ``shutil.disk_usage`` call per distinct filesystem, no new
subsystem) —

    probe id                    composes
    ─────────────────────────── ────────────────────────────────────────────
    npx_resolvable              vco_lib.npx_resolver (the v0.2.51 ladder)
    mcp_commands_spawnable      ~/.claude.json entries × the same resolver
    npm_pins                    vco_lib.cli.verify (``vco verify-pins``)
    launcher_binary_fresh       vco_lib.deferral_probes (WP-A's freshness leg)
    deferral_ledger             the WP-B registry's own clear probes
    owed_retryable_work         the WP-B registry's ``auto_retryable`` class
    disk_space                  ``shutil.disk_usage`` on the install root +
                                the vct state dir (``vco_lib.paths``)
    prereqs                     install.py's ``--bootstrap`` envelope (INJECTED
                                by install.py; not re-derived here)
    head_attached  ┐            read-only git plumbing against the checkout,
    source_currency┘            asking the SAME remote/branch questions
                                ``git_cmd.rs`` asks on the launcher side
    last_update_run             ``<install_root>/state/logs/install.jsonl``
                                (install.py's own durable session log)
    diagnostic_files            ``vct_launcher_core::logging``'s log dir +
                                the artefacts docs/post-install/UPDATE-
                                RECOVERY.md's table names
    stale_vct_deploy            ``tools/vct-secrets/vct``'s own ``VCT_GUARDS``
                                capability stamp, read off the copy PATH
                                actually resolves
    kg_binding_evidence         ``vco_lib.kg_binding_doctor`` (the D18
                                read-only three-value comparison) over
                                ``project_identity``'s one DB read and
                                ``weaviate_helpers``' listing/count/sample
    summary_pending             ``vco_lib.summary_health``'s sidecar scan
                                (the ``kg_summaries_degraded`` pending set;
                                REPORTED — the module owns the lifecycle)

v0.2.92 WP-14/WP-7 — what the last five close
---------------------------------------------
The tool positioned as the authoritative post-update health check could not
see the failure it exists to catch. A field install sat FIVE WEEKS behind
upstream in a detached HEAD while every surface reported health, and this
module had no probe for detached HEAD, no HEAD-vs-upstream distance, and no
"when did an update last succeed". ``probe_launcher_binary_fresh`` compares
the running binary to *the tree's* binary — on a frozen clone both are the
same frozen version, so it answered ``ok``: the correct answer to the wrong
question. The currency probes ask the question the user actually has.
``diagnostic_files`` closes the other half: the docs told users to copy
``~/.vct/update.log`` as step 0 of a recovery recipe, but that file is a
post-swap forensic artefact that cannot exist for anyone who never completed
a binary swap — so the ask is now generated from what is on disk rather than
from a document. ``stale_vct_deploy`` is the same theme one tool over: a
``vct`` deployed by ``cp`` months ago keeps running while its user reads
current documentation.

Fix boundary (§F decision #4)
-----------------------------
Each probe declares :attr:`Finding.fix` — ``auto_fix`` or ``defer``:

* **auto_fix** — environment-level owed WORK the user already consented to by
  installing (re-running an owed KG seed). It is dispatched through
  :mod:`vco_lib.deferral_retry`, which gates every action on its own
  precondition and caps attempts.
* **defer** — anything else, including EVERY finding that touches a running
  binary (the standing no-auto-restart / no-auto-heal ruling). The doctor
  emits a registry-classed condition naming the exact command and stops.

The hub's boot auto-restart (``running_hub_is_stale`` →
``hub_launcher.rs``) predates that ruling and stays GRANDFATHERED: it is the
hub's own documented contract, is scoped to a service VCO owns end-to-end, and
is explicitly not a precedent for the launcher binary. The doctor never
restarts anything.

Hermeticity
-----------
Every probe takes its facts from an injectable seam (``resolvers`` /
``injected``), so the whole engine is testable against a mocked environment
with no live services — the v0.2.89 env-probing-hermeticity lesson.

**Network I/O: exactly one call, in one probe, behind one seam.** Until v0.2.92
this section said "nothing in this module opens a socket by itself", which
``source_currency`` made untrue: its default fact-collector runs
``git ls-remote`` against the upstream remote, and that is a network round
trip. The precise contract now:

* it is the ONLY network call any probe makes, and it runs in the ``full``
  scope only (:func:`_ask_remote_for`);
* it is bounded (:data:`GIT_TIMEOUT_SECONDS`) and non-interactive
  (``GIT_TERMINAL_PROMPT=0``), so it cannot hang the caller waiting for a
  password nobody is there to type;
* it is READ-ONLY — ``ls-remote`` fetches nothing, writes no ref, and adds no
  object, so ``vco doctor`` never changes the repository it reports on;
* and it arrives through ``DoctorResolvers.source_facts``, so a test replaces
  the whole repo state with a dataclass and touches no network at all.

One thing outside the probe set can still reach the wire, deliberately: the
v0.2.95 standalone preflight (see :func:`reconcile_probe_cleared`) re-runs the
REGISTRY clear probes over the ledger — the same bounded, read-only probes
install.py's re-probe pass runs — and one of those may poll the local hub's
``/api/v1/health`` on the resolved port. Loopback-only, sub-second, and the
same verdict install.py would have reached; noted here so the "exactly one"
sentence above stays about the DOCTOR's own probes, which it still is.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from vco_lib import remedy_shell

#: Report contract version (the GUI + tests pin it).
SCHEMA_VERSION = 1

#: Probe outcome states. ``unknown`` is load-bearing: a probe that could not
#: run must never be rendered as "all good" (positive evidence only).
STATUS_OK = "ok"
STATUS_PROBLEM = "problem"
STATUS_UNKNOWN = "unknown"

#: The two fix dispositions of decision #4.
FIX_AUTO = "auto_fix"
FIX_DEFER = "defer"

#: Probe sets. ``boot`` is the cheap subset the launcher may run at startup
#: (in-process resolution + file reads); ``full`` is what install/update and
#: the CLI run. See :data:`PROBES` for what each excludes and why.
SCOPE_FULL = "full"
SCOPE_BOOT = "boot"

#: v0.2.95 F4. The exact command that (re)produces the bootstrap envelope a
#: standalone ``vco doctor`` needs for ``prereqs`` / ``launcher_binary_fresh``.
#: A printed command is shipped code (the promise rule), so the CLI test suite
#: asserts this is a real ``install.py`` surface that answers on stdout — the
#: probes print it whenever they must report ``unknown`` for want of the
#: envelope, so "not evaluated" always names what WOULD evaluate it.
BOOTSTRAP_COMMAND = "python install.py --bootstrap --json"

#: condition_id emitted when a bare-name MCP command cannot be resolved.
#: Registered ``action_required`` + install-owned, so it disappears on the
#: first run after the user installs Node (drop-when-absent).
CID_NPX_MISSING = "npx_missing_mcp_unspawnable"

#: condition_id emitted when free disk space is under the floor. Registered
#: ``environmental`` (a true, live description of the machine — nothing is
#: broken and VCO must not "fix" it by deleting the user's files) with a named
#: clear probe, so it resolves itself once space comes back.
CID_DISK_SPACE_LOW = "disk_space_low"

#: condition_id emitted when ``import vco_lib`` does not reach the checkout.
#: Registered ``action_required`` + install-owned: install step 4 repairs the
#: repairable shapes and the doctor re-probes in the SAME run, so the entry is
#: dropped by the very run that fixes it (drop-when-absent).
CID_VCO_LIB_SHADOWED = "vco_lib_shadowed_by_venv_copy"

#: condition_id emitted when a registered project's KG data demonstrably
#: lives in a class its primary binding does not name (the D18 existing-ghost
#: case the update-time prefix-adopt pass skips by construction). Registered
#: ``action_required``: something IS broken — the project's KG writes land
#: outside its binding — and the remedy is a human action in the launcher's
#: Identity tab. Read-only detection; the doctor never rewrites a binding.
CID_KG_BINDING_EVIDENCE_MISMATCH = "kg_binding_evidence_mismatch"

#: condition_id emitted when a populated ``*_KnowledgeGraph`` class is
#: claimed by NO registered project (no binding row names it, no registered
#: folder anchors its sampled paths) — the removed-project leftover the D18
#: scan's verdicts cannot see because verdicts are keyed by REGISTERED
#: project (v0.2.92 reported-not-fixed item 3; field find:
#: ``AgapeTest_KnowledgeGraph``, 84 objects). Registered ``action_required``:
#: deciding what unclaimed data is (re-add the project / re-bind / expected
#: leftover → dismiss) is a human decision that does not resolve on its own.
#: Read-only detection; the entry NEVER prints a drop command.
CID_KG_UNCLAIMED = "kg_unclaimed_populated_classes"

#: condition_id emitted when the RUNNING code-embedding service is not built
#: from the source in this checkout. ``code_embed`` is the only VCO service
#: that ships as an image BUILT from the tree, and compose builds only when
#: the image is MISSING — so a source fix can be live in git, green in CI, and
#: absent from every existing install (v0.2.92 BLOCKER-1: image 2026-05-16,
#: container recreated 2026-07-12, service still silently truncating).
#: Registered ``action_required`` + install-owned: install.py's compose-up now
#: passes ``--build`` when the image is not provably current, and the doctor
#: re-probes in the SAME run, so the entry is dropped by the run that fixes it.
CID_CODE_EMBED_IMAGE_STALE = "code_embed_image_stale"

#: v0.2.95 R5c. The model gateway's login-time registration exists and its
#: baked entry point cannot run — the state this machine sat in for eight
#: hours on 2026-09-10 while the launcher toggle read "registered". Shared
#: with ``vco_lib.gateway_ensure`` (the SessionStart emitter); both read the
#: same function, so the two surfaces cannot disagree.
CID_GATEWAY_UNRUNNABLE = "gateway_registered_but_unrunnable"

#: condition_id emitted when ``state/install-manifest.json`` attests a source
#: that this install's ``.claude/.vco-manifest.json`` — written ONLY by the
#: bundle engine — does not corroborate. The v0.2.95 surface map's H1/H2/H3:
#: the launcher's ``apply_launcher_update`` / ``force_resync_launcher`` /
#: ``update_orchestrator_at`` each advance the source and refresh the marker
#: without running install.py, so ``installed: true`` at the NEW version is
#: written over a venv/hooks/templates/MCP/KG/schema set still at the old one.
#: Registered ``action_required`` + install-owned (the ``vco_lib_shadowed``
#: precedent): the exit is an install/update run, which rewrites BOTH records
#: from one tree, and the doctor re-probes at the end of that same run — so
#: the entry is dropped by the very run that fixes it. Read-only detection;
#: this probe never writes a manifest and never touches an update path.
CID_INSTALL_MARKER_UNBACKED = "install_manifest_attests_uninstalled_source"

#: condition_ids the DOCTOR owns END-TO-END: it detects them AND emits them.
#: A cid another component owns (``launcher_binary_stale``) is REPORTED by the
#: doctor but emitted by its owner — re-emitting it here would fork its
#: lifecycle.
DOCTOR_OWNED_CIDS: tuple[str, ...] = (
    CID_NPX_MISSING, CID_DISK_SPACE_LOW, CID_VCO_LIB_SHADOWED,
    CID_KG_BINDING_EVIDENCE_MISMATCH, CID_KG_UNCLAIMED,
    CID_CODE_EMBED_IMAGE_STALE, CID_GATEWAY_UNRUNNABLE,
    CID_INSTALL_MARKER_UNBACKED,
)

#: Doctor-owned cids the doctor also RESOLVES when its own probe reports OK.
#: Only conditions whose probe is a cheap, positive-evidence re-measurement
#: belong here: the reading that emitted the entry is the same reading that
#: clears it, so "auto-clears once the machine recovers" is true at every
#: invocation point, not only at the next ``--update``.
DOCTOR_SELF_RESOLVING_CIDS: tuple[str, ...] = (
    CID_DISK_SPACE_LOW, CID_KG_BINDING_EVIDENCE_MISMATCH,
    CID_CODE_EMBED_IMAGE_STALE, CID_GATEWAY_UNRUNNABLE,
)

#: Env override for the free-space floor, in GiB (float). Default
#: :data:`DISK_MIN_FREE_GB_DEFAULT`.
DISK_MIN_FREE_ENV = "VCT_DISK_SPACE_MIN_FREE_GB"

#: Default free-space floor, GiB. Sized for what VCO itself needs headroom
#: for: a model pull, a Weaviate re-embed, a launcher rebuild + dist swap.
DISK_MIN_FREE_GB_DEFAULT = 2.0

#: Below this many free bytes the finding is ``critical`` rather than
#: ``warning`` — at a quarter-gig, writes are actively failing, not "tight".
DISK_CRITICAL_FREE_BYTES = 256 * 1024 * 1024

_GIB = 1024 ** 3


@dataclass(frozen=True)
class Finding:
    """One probe's verdict."""

    probe: str
    status: str
    #: One-line human summary. Rendered verbatim in the CLI + the report.
    summary: str
    #: ``auto_fix`` or ``defer``. Meaningless when ``status != problem`` —
    #: and the JSON contract says so STRUCTURALLY: :meth:`to_dict` omits the
    #: ``fix`` key from every non-problem finding, so a consumer branching on
    #: the key cannot mistake a healthy probe for deferred work. An OK or
    #: unknown finding carrying ``"fix": "defer"`` was exactly that trap.
    fix: str = FIX_DEFER
    #: Registry condition_id this finding maps to (``""`` when it maps to none).
    condition_id: str = ""
    #: Exact command the user would run. Empty for auto_fix findings.
    command: str = ""
    #: Free-form structured detail for the JSON payload.
    detail: dict = field(default_factory=dict)

    @property
    def is_problem(self) -> bool:
        return self.status == STATUS_PROBLEM


@dataclass
class DoctorReport:
    """The authoritative result of one doctor pass."""

    folder: Path
    scope: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def problems(self) -> list[Finding]:
        return [f for f in self.findings if f.is_problem]

    @property
    def unknowns(self) -> list[Finding]:
        return [f for f in self.findings if f.status == STATUS_UNKNOWN]

    @property
    def ok(self) -> bool:
        """True when no probe reported a problem. Unknowns do NOT fail."""
        return not self.problems

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "folder": str(self.folder),
            "scope": self.scope,
            "ok": self.ok,
            "findings": [
                {
                    "probe": f.probe,
                    "status": f.status,
                    "summary": f.summary,
                    # v0.2.92 (reported-not-fixed): `fix` is an action verb
                    # ("run this remediation"), and an action verb on a
                    # finding that needs no action is a trap for any consumer
                    # that branches on it. The key is therefore OMITTED for
                    # every non-problem finding — absence unambiguously means
                    # "nothing to do". Problems always carry it.
                    **({"fix": f.fix} if f.is_problem else {}),
                    "condition_id": f.condition_id,
                    "command": f.command,
                    "detail": f.detail,
                }
                for f in self.findings
            ],
        }

    def render_lines(self) -> list[str]:
        """Human report — one line per finding, problems first."""
        order = {STATUS_PROBLEM: 0, STATUS_UNKNOWN: 1, STATUS_OK: 2}
        marks = {STATUS_PROBLEM: "!", STATUS_UNKNOWN: "?", STATUS_OK: "ok"}
        out: list[str] = []
        for f in sorted(self.findings, key=lambda x: (order.get(x.status, 3), x.probe)):
            out.append(f"  [{marks.get(f.status, '?'):>2}] {f.probe}: {f.summary}")
            if f.is_problem and f.command:
                for line in f.command.splitlines():
                    out.append(f"        {line}")
        return out


# ---------------------------------------------------------------------------
# Resolver seam — every environment fact a probe needs arrives through here so
# tests can supply a whole fake environment without patching module internals.
# ---------------------------------------------------------------------------


@dataclass
class DoctorResolvers:
    """Injectable environment accessors. Defaults hit the real machine."""

    #: () -> payload of vco_lib.npx_resolver.probe(names)
    npx_probe: Optional[Callable[[Sequence[str]], dict]] = None
    #: () -> parsed ~/.claude.json mcpServers mapping ({} when absent)
    mcp_entries: Optional[Callable[[], dict]] = None
    #: (folder) -> DeferralReport-like object, or None
    deferral_report: Optional[Callable[[Path], Any]] = None
    #: (folder, entry) -> tri-state probe verdict
    probe_entry: Optional[Callable[[Path, Any], Optional[bool]]] = None
    #: () -> list of pin rows (``.key/.pinned/.installed/.status``), or None
    #: when npm is unavailable. Injected so tests never shell out to npm.
    pin_rows: Optional[Callable[[], Optional[list]]] = None
    #: (path) -> object with ``.total/.used/.free``, or None when the path
    #: cannot be measured. Injected so a test never reads the real disk.
    disk_usage: Optional[Callable[[Path], Any]] = None
    #: (install_root) -> ``{"origin", "purelib", "error"}`` payload, or None
    #: when the measurement could not be taken. Injected so a test never
    #: spawns an interpreter.
    vco_lib_origin: Optional[Callable[[Path], Optional[dict]]] = None
    #: (folder, ask_remote) -> :class:`SourceFacts`. Injected so a test can
    #: describe a whole repo state — detached, behind, unreachable remote —
    #: without a git binary. The default shells read-only git plumbing.
    source_facts: Optional[Callable[[Path, bool], "SourceFacts"]] = None
    #: (name) -> absolute path the OS would run for ``name``, or None.
    #: Defaults to :func:`shutil.which`. Injected so the stale-``vct`` probe
    #: can be driven against a synthetic PATH on any OS.
    path_command: Optional[Callable[[str], Optional[str]]] = None
    #: () -> :class:`vco_lib.kg_binding_doctor.BindingEvidenceScan`, or None
    #: when the comparison could not be LOOKED at (launcher.db unreadable or
    #: Weaviate unreachable — probe failure is not evidence). Defaults to the
    #: scan itself; injected so the KG-binding probe's tests describe a whole
    #: machine's bindings and Weaviate state with dataclasses, no services.
    kg_binding_evidence: Optional[Callable[[], Optional[Any]]] = None
    #: (install_root) -> :class:`vco_lib.code_embed_image.ImageState`.
    #: Injected so the code-embed staleness probe is driven from a described
    #: machine — no service, no container runtime, no network.
    code_embed_state: Optional[Callable[[Path], Any]] = None
    #: () -> :class:`vco_lib.code_embed_image.RebuildContext` — the compose
    #: invocation this host has, plus the compose identity that OWNS the
    #: running container. Injected so the rebuild remediation is rendered
    #: from a described machine: no runtime, no container, no inspect.
    code_embed_rebuild_context: Optional[Callable[[], Any]] = None
    #: () -> :class:`vco_lib.gateway_ensure.GatewayEnsureResult`. Injected so
    #: the gateway probe is driven from a described machine: no systemd, no
    #: launchd, no schtasks, and no spawn of a registered entry point.
    gateway_state: Optional[Callable[[], Any]] = None
    #: (project_root) -> :class:`vco_lib.summary_health.PendingSet`, or None
    #: when the sidecars could not be read. Defaults to the scan itself;
    #: injected so the summary-pending probe's tests describe a machine's
    #: sidecars with dataclasses, no filesystem.
    summary_pending: Optional[Callable[[Path], Optional[Any]]] = None

    def resolve_gateway_state(self):
        """The model gateway's registration verdict, from its ONE home.

        Delegates to :mod:`vco_lib.gateway_ensure` rather than re-reading a
        unit file here, so the doctor's finding, the SessionStart ensure and
        the registry's clear probe cannot disagree. Soft-fail: an exception
        reads as "not registered", which produces NO findings — the
        conservative answer, because the alternative is telling a user their
        opt-in daemon is broken on the strength of a probe that crashed.
        """
        if self.gateway_state is not None:
            return self.gateway_state()
        from vco_lib import gateway_ensure

        try:
            return gateway_ensure.gateway_status()
        except Exception:  # noqa: BLE001 — could not look is not a verdict
            return gateway_ensure.GatewayEnsureResult(
                state=gateway_ensure.GatewayState.NOT_REGISTERED,
                reason="the gateway registration probe could not run",
            )

    def resolve_code_embed_state(self, install_root: Path):
        """The code-embed image verdict, composed from its ONE home.

        Delegates to :mod:`vco_lib.code_embed_image` rather than re-deriving
        the comparison, so the doctor's finding, the registry's clear probe
        and the compose-up rebuild decision can never disagree about what
        "the running service is current" means. Soft-fail: any exception is
        an ``unknown`` verdict, never a claim of currency.
        """
        if self.code_embed_state is not None:
            return self.code_embed_state(Path(install_root))
        from vco_lib import code_embed_image

        try:
            return code_embed_image.image_state(Path(install_root))
        except Exception:  # noqa: BLE001 — could not look is not a verdict
            return code_embed_image.ImageState(
                code_embed_image.UNKNOWN,
                "code_embed: the image-state probe could not run.",
            )

    def resolve_code_embed_rebuild_context(self):
        """What the printed rebuild command needs to know about the machine.

        v0.2.95 F2. Composed from :mod:`vco_lib.code_embed_image` for the same
        reason as the verdict above: the command the entry prints and the
        ownership rule the installer's step-5 guard applies are the same fact,
        and a second reading here could print a rebuild in a project that does
        not own the container. Injected so the remediation's tests describe a
        machine (foreign project, own project, nothing running) without a
        container runtime.

        Soft-fail: any exception yields the default context, i.e. the
        installer's own compose project — the shape that is right for every
        machine that has no foreign owner, and never a claim about one.
        """
        from vco_lib import code_embed_image

        if self.code_embed_rebuild_context is not None:
            return self.code_embed_rebuild_context()
        try:
            return code_embed_image.rebuild_context()
        except Exception:  # noqa: BLE001 — a probe never fails the doctor
            return code_embed_image.RebuildContext()

    def resolve_source_facts(self, folder: Path, *, ask_remote: bool) -> "SourceFacts":
        if self.source_facts is not None:
            return self.source_facts(Path(folder), ask_remote)
        return collect_source_facts(Path(folder), ask_remote=ask_remote)

    def resolve_path_command(self, name: str) -> Optional[str]:
        if self.path_command is not None:
            return self.path_command(name)
        return shutil.which(name)

    def resolve_kg_binding_evidence(self) -> Optional[Any]:
        """The KG-binding evidence scan, or ``None`` when it could not look.

        Composes :mod:`vco_lib.kg_binding_doctor` — the ONE home for the
        three-value comparison — so the doctor's finding, the registry's
        clear probe and any future caller can never disagree about what
        "the data lives where the binding says" means. The scan itself is
        soft-fail throughout; the ``except`` here is belt-and-braces so a
        probe defect can never surface as a false verdict either way.
        """
        if self.kg_binding_evidence is not None:
            return self.kg_binding_evidence()
        from vco_lib.kg_binding_doctor import scan_kg_binding_evidence

        try:
            return scan_kg_binding_evidence()
        except Exception:  # noqa: BLE001 — could not look is not a verdict
            return None

    def resolve_disk_usage(self, path: Path):
        """Free-space triple for ``path``, or ``None`` when unmeasurable.

        Defaults to :func:`shutil.disk_usage`. Every failure arm returns
        ``None`` (which the probe renders as ``unknown``) — a path that cannot
        be stat'ed is not evidence that the disk is fine.
        """
        if self.disk_usage is not None:
            return self.disk_usage(Path(path))
        try:
            return shutil.disk_usage(str(path))
        except OSError:
            return None

    def resolve_npx(self, names: Sequence[str]) -> dict:
        if self.npx_probe is not None:
            return self.npx_probe(names)
        from vco_lib import npx_resolver

        return npx_resolver.probe(names)

    def resolve_mcp_entries(self) -> dict:
        if self.mcp_entries is not None:
            return self.mcp_entries()
        return _read_claude_json_mcp_servers()

    def resolve_pin_rows(self) -> Optional[list]:
        """Bundled-npm pin rows, or ``None`` when npm cannot be asked.

        Composes ``vco_lib.cli.verify``'s OWN row collector rather than
        re-deriving the comparison, so the doctor can never disagree with
        ``vco verify-pins`` about what drift means. Reaching into that
        module's private helpers is deliberate: the alternative is a second
        implementation of the same rule, which the modularity rule forbids
        outright (its public entry prints and returns an exit code — not
        composable).
        """
        if self.pin_rows is not None:
            return self.pin_rows()
        from vco_lib.cli import verify

        npm_path = verify._which("npm")
        if npm_path is None:
            return None
        return verify._collect_pin_rows(
            verify._npm_pin_section(verify._load_bundled_versions()),
            npm_path=npm_path,
        )

    def resolve_vco_lib_origin(self, install_root: Path) -> Optional[dict]:
        """Where does ``import vco_lib`` land, measured from a NEUTRAL cwd?

        Composes ``vco_lib.install_companions`` — the same module install.py's
        step-4 repair uses, so the doctor and the repair can never disagree
        about what "shadowed" means. ``None`` when the venv interpreter cannot
        be found or the probe subprocess failed; the caller renders that as
        ``unknown``, never as healthy.
        """
        if self.vco_lib_origin is not None:
            return self.vco_lib_origin(Path(install_root))
        from vco_lib.install_companions import (
            measure_vco_lib_origin,
            resolve_install_venv_python,
        )

        venv_python = resolve_install_venv_python(Path(install_root))
        if venv_python is None:
            return None
        return measure_vco_lib_origin(venv_python)

    def resolve_deferral_report(self, folder: Path):
        if self.deferral_report is not None:
            return self.deferral_report(folder)
        try:
            from vco_lib.deferral_report import DeferralReport

            return DeferralReport.read(folder)
        except Exception:  # noqa: BLE001 — an unreadable ledger is "unknown"
            return None

    def resolve_summary_pending(self, project_root: Path) -> Optional[Any]:
        """The summary-degradation pending set, or ``None`` when unscannable.

        Composes :func:`vco_lib.summary_health.scan_pending` — the ONE home
        for "which summary rows are degraded", already derived (not a second
        ledger) from the sidecars' recorded backends — so the doctor's count,
        the ``kg_summaries_degraded`` entry's own ``detected`` text and the
        recheck's backfill list can never disagree about what is pending.
        Soft-fail: an unreadable sidecar tree is ``None`` (the probe renders
        ``unknown``), never "nothing pending".
        """
        if self.summary_pending is not None:
            return self.summary_pending(Path(project_root))
        from vco_lib import summary_health  # noqa: PLC0415

        try:
            return summary_health.scan_pending(Path(project_root))
        except Exception:  # noqa: BLE001 — could not look is not a verdict
            return None


def _read_claude_json_mcp_servers() -> dict:
    """``~/.claude.json`` → ``mcpServers`` mapping. ``{}`` on any failure.

    Same file the launcher's badge reads. Absent/unparseable degrades to an
    empty mapping, which yields an ``unknown`` finding rather than a false OK.

    Resolved through ``vco_lib.paths.user_home`` — the ONE home for
    ``$VCT_USER_HOME_OVERRIDE``. ``run_doctor`` reaches this, so an inline
    ``Path.home()`` made the test suite read the developer's REAL global Claude
    config (branching on whatever MCPs they happened to have registered) while
    CI branched on the empty mapping. No-op in production.
    """
    from vco_lib.paths import user_home

    try:
        path = user_home() / ".claude.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    servers = payload.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def bare_command_names(servers: dict) -> list[str]:
    """Bare-name (non-path) ``command`` values across MCP entries, sorted.

    A path-shaped command (``/opt/vco/.venv/bin/python``) either exists or
    does not, and the launcher's existing ``path_matches_install`` check
    already covers it. A BARE name is the interesting case: Claude Code
    resolves it from the spawn PATH at MCP-launch time, so an unresolvable one
    is an MCP that silently never starts — exactly the npx failure.
    """
    out: set[str] = set()
    for entry in servers.values():
        if not isinstance(entry, dict):
            continue
        cmd = entry.get("command")
        if not isinstance(cmd, str) or not cmd:
            continue
        if command_is_path(cmd):
            continue
        out.add(cmd)
    return sorted(out)


def command_is_path(cmd: str) -> bool:
    """True when ``cmd`` names a filesystem path rather than a PATH lookup.

    Mirrors ``maintenance.rs::entry_resource_path``'s heuristic (POSIX
    absolute, drive-letter, UNC) plus any embedded separator — a relative
    ``./foo`` or ``dir/foo`` is also resolved by the OS, not by PATH.
    """
    if not cmd:
        return False
    if cmd.startswith("/") or cmd.startswith("\\\\"):
        return True
    if len(cmd) >= 2 and cmd[1] == ":":
        return True
    return "/" in cmd or "\\" in cmd


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def probe_mcp_commands_spawnable(folder: Path, res: DoctorResolvers, ctx: dict) -> list[Finding]:
    """npx/node resolvability for every bare-name MCP command.

    Two findings can come out of this probe:

    * ``npx_resolvable`` — the ladder's verdict on npx itself, reported even
      when no entry needs it (it is the prerequisite VCO's own bundled
      playwright/mermaid entries assume).
    * ``mcp_commands_spawnable`` — the per-entry verdict.

    The problem finding is ``defer``: installing Node.js is not something VCO
    may do to a user's machine unattended.
    """
    servers = res.resolve_mcp_entries()
    names = bare_command_names(servers)
    payload = res.resolve_npx(names)
    resolved = payload.get("commands") or {}

    findings: list[Finding] = []
    npx_present = bool(payload.get("npx_present"))
    npm_present = bool(payload.get("npm_present"))
    if npx_present:
        npx_summary = f"npx resolves at {payload.get('npx_path')}"
        npx_cid = ""
        npx_command = ""
    else:
        npx_summary = "npx is NOT resolvable" + (
            " (npm IS present — wrapper MCPs fall back to `npm exec`, but "
            "entries registered as `npx` cannot spawn)"
            if npm_present
            else " and neither is npm — Node.js is not installed"
        )
        npx_cid = CID_NPX_MISSING
        npx_command = _npx_remediation(npm_present)
    findings.append(
        Finding(
            probe="npx_resolvable",
            status=STATUS_OK if npx_present else STATUS_PROBLEM,
            summary=npx_summary,
            fix=FIX_DEFER,
            condition_id=npx_cid,
            command=npx_command,
            detail={"npx_path": payload.get("npx_path", ""), "npm_present": npm_present},
        )
    )

    unresolvable = sorted(n for n in names if not resolved.get(n))
    if not names:
        findings.append(
            Finding(
                probe="mcp_commands_spawnable",
                status=STATUS_UNKNOWN,
                summary=(
                    "no MCP entries with a bare-name command found in "
                    "~/.claude.json (file absent, empty, or all entries use "
                    "absolute paths)"
                ),
            )
        )
    elif unresolvable:
        affected = sorted(
            name for name, entry in servers.items()
            if isinstance(entry, dict) and entry.get("command") in unresolvable
        )
        # Only npx has a registered condition today; another unresolvable bare
        # command still reports as a problem, it just has no cid of its own.
        entry_cid = CID_NPX_MISSING if "npx" in unresolvable else ""
        entry_command = _npx_remediation(npm_present) if "npx" in unresolvable else ""
        findings.append(
            Finding(
                probe="mcp_commands_spawnable",
                status=STATUS_PROBLEM,
                summary=(
                    f"{len(affected)} MCP entr"
                    f"{'y' if len(affected) == 1 else 'ies'} cannot spawn: "
                    f"{', '.join(affected)} — command(s) "
                    f"{', '.join(unresolvable)} not resolvable on PATH"
                ),
                fix=FIX_DEFER,
                condition_id=entry_cid,
                command=entry_command,
                detail={"entries": affected, "commands": unresolvable},
            )
        )
    else:
        findings.append(
            Finding(
                probe="mcp_commands_spawnable",
                status=STATUS_OK,
                summary=(
                    f"all {len(names)} bare MCP command(s) resolve: "
                    f"{', '.join(names)}"
                ),
            )
        )
    ctx["npx_present"] = npx_present
    return findings


def _npx_remediation(npm_present: bool) -> str:
    """The exact command block the deferral + CLI both print.

    v0.2.92 (R42 follow-up): the npm-present branch printed a single POSIX
    recipe (``ln -s`` + ``readlink -f`` + ``command -v``), which is the same
    Windows-narrowing R42 bans one layer up in the file — a Windows user who
    reports this class cannot paste any of it. It now branches per OS; the
    verify pair is split onto two lines because Windows PowerShell 5.1 (the
    default ``powershell.exe`` on every Windows 10/11 box) rejects ``&&``.
    On Windows the npm-present case is rarer (the Node installer ships
    ``npx.cmd`` next to ``npm.cmd``), so the honest advice is to look beside
    npm and fix PATH — the exact sibling-of-npm layout ``npx_resolver``
    probes.
    """
    if sys.platform == "win32":
        if npm_present:
            return (
                "# npm is present but npx is not (probed as npx / npx.cmd / "
                "npx.ps1). On Windows the Node installer ships npx.cmd NEXT "
                "TO npm.cmd, so look there first:\n"
                "#   where.exe npm\n"
                "# If npx.cmd sits in the same directory, that directory is "
                "not on PATH — add it (Settings -> System -> About -> "
                "Advanced system settings -> Environment Variables -> Path). "
                "If it does not, reinstall Node.js 18+ from "
                "https://nodejs.org.\n"
                "# then reopen Claude Code so the MCPs re-spawn."
            )
        return (
            "# Install Node.js 18+ (https://nodejs.org), then reopen Claude Code:\n"
            "#   node --version\n"
            "#   npx --version\n"
            "# Nothing else is needed — VCO re-detects npx on the next run."
        )
    if npm_present:
        return (
            "# npm is present but npx is not — symlink it onto PATH:\n"
            "#   ln -s \"$(dirname \"$(readlink -f \"$(command -v npm)\")\")/npx\" "
            "~/.local/bin/npx\n"
            "# then reopen Claude Code so the MCPs re-spawn."
        )
    return (
        "# Install Node.js 18+ (https://nodejs.org), then reopen Claude Code:\n"
        "#   node --version\n"
        "#   npx --version\n"
        "# Nothing else is needed — VCO re-detects npx on the next run."
    )


def probe_launcher_binary_fresh(folder: Path, res: DoctorResolvers, ctx: dict) -> list[Finding]:
    """Is the delivered launcher binary the one the tree says it should be?

    Composes WP-A's freshness probe through its Python leg
    (``deferral_probes.launcher_binary_stale_still_applies``) rather than
    re-deriving the comparison. Surface-only by construction: a stale RUNNING
    binary is repaired by the user quitting and reopening the launcher, and
    decision #4 forbids the doctor from touching running binaries.

    **Read its ``ok`` narrowly.** This probe compares the delivered binary with
    the one THIS TREE builds. On a checkout that has not updated in weeks both
    are the same frozen version, so it answers ``ok`` — truthfully, and about
    something the user did not ask. That reading is what let a five-week outage
    look healthy. :func:`probe_source_currency` asks whether the tree itself is
    current, and the two are only meaningful together; the ``ok`` summary says
    so rather than leaving the inference to the reader.

    v0.2.95 F4: when the caller supplies no extras the standalone CLI has
    already TRIED to obtain them from the install root
    (:func:`supply_missing_install_context`). Reaching this branch without
    extras therefore means the facts could not be produced, and the finding
    says so tri-state — ``unknown``, never ``ok`` — naming the exact command
    that would evaluate it. "Not evaluated" must not read as absence of a
    problem; it must read as an unanswered question with the question attached.
    """
    extras = ctx.get("launcher_probe_extras") or {}
    if not extras:
        return [
            Finding(
                probe="launcher_binary_fresh",
                status=STATUS_UNKNOWN,
                summary=(
                    "launcher freshness not evaluated — no dist/binary facts "
                    "could be obtained from the install root (the OS→dist-"
                    "subdir mapping has one home in install.py). To produce "
                    f"them yourself, run: {BOOTSTRAP_COMMAND}"
                ),
                command=BOOTSTRAP_COMMAND,
            )
        ]
    from vco_lib import deferral_probes

    verdict = deferral_probes.run_probe(
        "launcher_binary_stale_still_applies",
        deferral_probes.ProbeContext(folder=folder, entry=None, extras=extras),
    )
    if verdict is None:
        return [
            Finding(
                probe="launcher_binary_fresh",
                status=STATUS_UNKNOWN,
                summary="launcher binary freshness could not be determined",
            )
        ]
    if verdict:
        return [
            Finding(
                probe="launcher_binary_fresh",
                status=STATUS_PROBLEM,
                summary=(
                    "the launcher binary on disk is not the one this tree "
                    "builds (or a staged swap has not been applied)"
                ),
                fix=FIX_DEFER,
                condition_id="launcher_binary_stale",
                command=(
                    "# Quit the launcher fully (tray -> Quit), then reopen it.\n"
                    "# If it still lags: python install.py --update"
                ),
            )
        ]
    return [
        Finding(
            probe="launcher_binary_fresh",
            status=STATUS_OK,
            summary=(
                "launcher dist binary matches the tree (this says nothing about "
                "whether the TREE is current — see source_currency)"
            ),
        )
    ]


def probe_deferral_ledger(folder: Path, res: DoctorResolvers, ctx: dict) -> list[Finding]:
    """Summarise the ledger by disposition and name the retryable work owed.

    This is the probe that makes the doctor the AUTHORITATIVE end-of-update
    report: it reads the same ledger the CLAUDE.md reminder points at and
    splits it the way WP-B's registry declares, so "3 pending actions" never
    again means "3 records of things already done".
    """
    # v0.2.92 (R23, pre-existing): `deferral_registry` was imported here and
    # never used — a leftover from the wave-2 fix that routed the partition
    # through `deferral_report.partition_entries` (below) instead. ruff has
    # flagged it since; removed rather than reported, it is this file.
    from vco_lib import deferral_retry

    report = res.resolve_deferral_report(folder)
    if report is None:
        return [
            Finding(
                probe="deferral_ledger",
                status=STATUS_UNKNOWN,
                summary="deferral ledger unreadable",
            )
        ]
    cids = [getattr(e, "condition_id", "") for e in getattr(report, "entries", [])]
    cids = [c for c in cids if c]
    if not cids:
        return [
            Finding(
                probe="deferral_ledger",
                status=STATUS_OK,
                summary="no pending deferral entries",
            )
        ]
    # v0.2.91 dogfood fix: THE partition, not the registry-only one. The
    # cid-only helper cannot see an entry's explicit `disposition`, so this
    # finding could report a tier the ledger it just read disagreed with —
    # the divergence wave-2 MINOR-3 closed for the CLAUDE.md reminder and left
    # armed here. `partition_entries` returns entries; this finding reports ids.
    from vco_lib.deferral_report import partition_entries

    actionable_entries, informational_entries = partition_entries(report)
    actionable = [getattr(e, "condition_id", "") for e in actionable_entries]
    informational = [getattr(e, "condition_id", "") for e in informational_entries]
    # v0.2.91 wave-3 (NIT): consult the attempt cap. A cid whose cap is spent
    # would be SKIPPED by the dispatcher, so promising "VCO can retry this
    # itself" for it is a promise the next dispatch will not keep — the entry
    # is now ordinary manual work, and the ledger finding above already says
    # so. Only conditions a dispatch would actually RUN are listed here.
    retryable = [
        cid
        for cid in deferral_retry.retryable_condition_ids(cids)
        if deferral_retry.attempt_count(folder, cid) < deferral_retry.MAX_ATTEMPTS
    ]
    findings = [
        Finding(
            probe="deferral_ledger",
            status=STATUS_PROBLEM if actionable else STATUS_OK,
            summary=(
                f"{len(actionable)} actionable, {len(informational)} "
                f"informational/record entr"
                f"{'y' if len(cids) == 1 else 'ies'}"
                + (f" — actionable: {', '.join(actionable)}" if actionable else "")
            ),
            fix=FIX_DEFER,
            command=(
                "# Read the entry bodies and run the command each one names:\n"
                "#   .claude/context/UPDATE_DEFERRED.md"
                if actionable
                else ""
            ),
            detail={"actionable": actionable, "informational": informational},
        )
    ]
    if retryable:
        # v0.2.92 (WFT C7 / register item 7): "VCO can retry itself" is true of
        # the CLASSIFICATION and can be false of the last three weeks. The
        # dispatcher now records a durable row every time a pass found the
        # backend down, so this sentence is answerable from evidence instead of
        # from the tier — and this finding is where it gets said, because the
        # entry itself belongs to another component that would overwrite an
        # explicit disposition on its next re-emit.
        notes = {
            cid: deferral_retry.retry_disposition_note(folder, cid)
            for cid in retryable
        }
        blocked = [cid for cid, note in notes.items() if note]
        findings.append(
            Finding(
                probe="owed_retryable_work",
                status=STATUS_PROBLEM,
                summary=(
                    f"{len(retryable)} condition(s) VCO can retry itself: "
                    f"{', '.join(retryable)}"
                    + (
                        " — "
                        + " ".join(f"{cid}: {notes[cid]}" for cid in blocked)
                        if blocked
                        else ""
                    )
                ),
                fix=FIX_AUTO,
                detail={
                    "condition_ids": retryable,
                    "retry_notes": {cid: notes[cid] for cid in blocked},
                },
            )
        )
    return findings


def probe_summary_pending(folder: Path, res: DoctorResolvers, ctx: dict) -> list[Finding]:
    """How many KG/code summary rows are degraded, and the way out.

    v0.2.96 WP-7b (WP-7a review MINOR-3): the ``kg_summaries_degraded``
    entry carries the pending counts in its ``detected`` text, but the
    DOCTOR — the authoritative end-of-update report — never read them, so
    ``vco doctor`` could say "1 actionable entry" without saying WHAT was
    owed. This probe surfaces the count from the same scan the entry and
    the recheck use (:func:`vco_lib.summary_health.scan_pending`), plus the
    one-line recovery.

    REPORTED, not owned: the condition is emitted and paired-resolved by
    :mod:`vco_lib.summary_health` (only ``summary_recheck``'s post-scan
    settles it), so the finding carries the cid for routing but is
    deliberately outside :data:`DOCTOR_OWNED_CIDS` — the doctor never
    re-emits or resolves it. An OK reading therefore does NOT clear a live
    entry either (that is the paired-resolution contract, not an omission):
    a live entry over an empty pending set stays a PROBLEM here naming the
    recheck, because that entry needs its resolver's audit row to go away.

    Trigger: the condition is live in this folder's ledger OR the pending
    set is non-empty — either alone is real degradation (a pending set with
    no entry is the freshly-upgraded machine whose legacy sidecar rows
    predate the ``backend`` field; they regenerate only via the recheck).

    ``full`` scope only: the scan is local file reads plus one sha256 per
    knowledge node — cheap per node, but the orchestrator root's tree is
    walked in full, and the answer changes when summaries generate, not at
    boot. The probe's readers are install/update's end-of-run report and
    ``vco doctor`` (the ``install_completeness`` cost precedent).
    """
    from vco_lib import summary_health  # noqa: PLC0415

    pending = res.resolve_summary_pending(folder)
    total = getattr(pending, "total", None)
    if not isinstance(total, int):
        # Positive evidence only: sidecars that cannot be read are not
        # "nothing pending" (the `mcp_commands_spawnable` precedent).
        return [
            Finding(
                probe="summary_pending",
                status=STATUS_UNKNOWN,
                summary="KG/code summary sidecars could not be scanned",
            )
        ]
    report = res.resolve_deferral_report(folder)
    live = report is not None and any(
        getattr(e, "condition_id", "") == summary_health.CONDITION_ID
        for e in getattr(report, "entries", []) or []
    )
    if not live and total == 0:
        return [
            Finding(
                probe="summary_pending",
                status=STATUS_OK,
                summary="no summary rows pending regeneration",
                detail={"pending_total": 0},
            )
        ]
    kg_stale = len(getattr(pending, "kg_stale", []) or [])
    kg_missing = len(getattr(pending, "kg_missing", []) or [])
    code_stale = len(getattr(pending, "code_stale", []) or [])
    root = Path(folder).resolve()
    return [
        Finding(
            probe="summary_pending",
            status=STATUS_PROBLEM,
            summary=(
                f"{total} summary row(s) degraded ({kg_stale} KG on a "
                f"fallback backend, {kg_missing} KG with no summary, "
                f"{code_stale} code on a fallback backend)"
                + (
                    f" — the {summary_health.CONDITION_ID} entry is live"
                    if live else ""
                )
            ),
            fix=FIX_DEFER,
            condition_id=summary_health.CONDITION_ID,
            command=remedy_shell.steps(
                "# New/changed nodes resume on the preferred tier at the "
                "5 h breaker cooldown;",
                "# hash-frozen fallback rows regenerate only via the "
                "recheck:",
                f"cd {remedy_shell.quote(root)}",
                "python -m vco_lib.summary_health summary-recheck "
                f"--project-root {remedy_shell.quote(root)}",
            ),
            detail={
                "pending_total": total,
                "kg_stale": kg_stale,
                "kg_missing": kg_missing,
                "code_stale": code_stale,
                "condition_live": live,
            },
        )
    ]


def probe_prereqs(folder: Path, res: DoctorResolvers, ctx: dict) -> list[Finding]:
    """Consume the ``--bootstrap`` envelope's ``missing_prereqs``.

    install.py INJECTS the envelope it already built rather than the doctor
    shelling back into install.py mid-run (which would be circular and slow).
    A standalone ``vco doctor`` — which has no caller to hand it one — obtains
    the same envelope itself, in-process, through the SAME producer functions
    (:func:`supply_missing_install_context`); only when THAT fails does this
    probe report ``unknown`` — never a false OK — naming the exact command
    that would produce the facts.

    This is the "consumer that acts after install" report 6 §B.1 says is
    missing: the envelope's findings previously died in a stdout block.
    """
    envelope = ctx.get("bootstrap_envelope")
    if not isinstance(envelope, dict):
        return [
            Finding(
                probe="prereqs",
                status=STATUS_UNKNOWN,
                summary=(
                    "prereqs not re-checked — no bootstrap envelope could be "
                    "obtained from the install root. To produce one, run: "
                    f"{BOOTSTRAP_COMMAND}"
                ),
                command=BOOTSTRAP_COMMAND,
            )
        ]
    missing = [m for m in (envelope.get("missing_prereqs") or []) if isinstance(m, dict)]
    blocking = [m for m in missing if m.get("severity") == "blocking"]
    if not blocking:
        return [
            Finding(
                probe="prereqs",
                status=STATUS_OK,
                summary=(
                    "no blocking prerequisites missing"
                    + (f" ({len(missing)} optional/warning noted)" if missing else "")
                ),
            )
        ]
    return [
        Finding(
            probe="prereqs",
            status=STATUS_PROBLEM,
            summary=(
                "missing prerequisite(s): "
                + ", ".join(str(m.get("name", "?")) for m in blocking)
            ),
            fix=FIX_DEFER,
            command="\n".join(
                f"# {m.get('name')}: {m.get('install_hint', '')}".rstrip()
                for m in blocking
            ),
            detail={"blocking": [m.get("name") for m in blocking]},
        )
    ]


def probe_npm_pins(folder: Path, res: DoctorResolvers, ctx: dict) -> list[Finding]:
    """``vco verify-pins`` as a probe rather than a separate entry point.

    Reports drift; never fixes. ``vco verify-pins --fix`` stays the consented
    path: a pin repair runs ``npm install -g``, a side effect on the user's
    global Node install, not an environment READ.
    """
    try:
        rows = res.resolve_pin_rows()
    except Exception as exc:  # noqa: BLE001 — manifest/import problem ⇒ unknown
        return [
            Finding(
                probe="npm_pins",
                status=STATUS_UNKNOWN,
                summary=f"npm pin status unavailable: {exc}",
            )
        ]
    if rows is None:
        return [
            Finding(
                probe="npm_pins",
                status=STATUS_UNKNOWN,
                summary="npm not available — bundled pins not checked",
            )
        ]
    # DRIFT (installed at the wrong version) is a problem. MISSING is not:
    # a bundled npm package can be legitimately absent from the global store
    # (an opt-out env var at install time, a `file:` pin installed elsewhere,
    # a default-disabled diagram MCP the user never enabled). Reporting those
    # as problems would make the doctor cry wolf on a healthy machine — and a
    # report that cries wolf is one nobody reads, which is the failure this
    # whole work package exists to end.
    drifted = [r for r in rows if r.status == "drift"]
    absent = [r for r in rows if r.status == "missing"]
    if not drifted:
        return [
            Finding(
                probe="npm_pins",
                status=STATUS_OK,
                summary=(
                    f"no bundled npm pin drift across {len(rows)} pin(s)"
                    + (
                        f" ({len(absent)} not installed globally: "
                        f"{', '.join(r.key for r in absent)})"
                        if absent
                        else ""
                    )
                ),
                detail={"missing": [r.key for r in absent]},
            )
        ]
    return [
        Finding(
            probe="npm_pins",
            status=STATUS_PROBLEM,
            summary=(
                "bundled npm pin drift: "
                + ", ".join(f"{r.key} @{r.installed} != {r.pinned}" for r in drifted)
            ),
            fix=FIX_DEFER,
            command="vco verify-pins --fix",
            detail={"drifted": [r.key for r in drifted],
                    "missing": [r.key for r in absent]},
        )
    ]


# ---------------------------------------------------------------------------
# Disk space — the one probe that measures a resource rather than a config
# ---------------------------------------------------------------------------


def disk_min_free_bytes() -> tuple[int, float, bool]:
    """``(floor bytes, floor GiB, overridden?)`` from the env or the default.

    A malformed / non-positive ``VCT_DISK_SPACE_MIN_FREE_GB`` falls back to the
    default rather than disabling the probe: a fat-fingered value must not
    silently turn the check off (the same policy as ``VCO_CG_INJECT_CAP``).
    """
    raw = os.environ.get(DISK_MIN_FREE_ENV, "").strip()
    if raw:
        try:
            gib = float(raw)
        except ValueError:
            gib = DISK_MIN_FREE_GB_DEFAULT
        else:
            if gib > 0:
                return int(gib * _GIB), gib, True
    return int(DISK_MIN_FREE_GB_DEFAULT * _GIB), DISK_MIN_FREE_GB_DEFAULT, False


def _nearest_existing(path: Path) -> Optional[Path]:
    """``path`` or its nearest existing ancestor — ``None`` if none exists.

    ``shutil.disk_usage`` needs a path that EXISTS. The vct state dir may not
    have been created yet on a first run, and its parent's filesystem is the
    one that would hold it, so walking up measures the right device instead of
    reporting ``unknown`` for a perfectly measurable mount.
    """
    try:
        current = Path(path).resolve()
    except OSError:
        return None
    for candidate in (current, *current.parents):
        try:
            if candidate.exists():
                return candidate
        except OSError:
            return None
    return None


def _disk_device_key(path: Path):
    """A filesystem identity for ``path`` — ``st_dev``, else the path string.

    Used to DEDUPE: on most installs the orchestrator clone and ``~/.vct`` sit
    on the same filesystem, and reporting one mount twice would make a single
    low-space condition read like two.
    """
    try:
        return ("dev", os.stat(str(path)).st_dev)
    except OSError:
        return ("path", str(path))


def measure_disk_space(
    folder: Path, res: Optional["DoctorResolvers"] = None
) -> tuple[list[dict], list[str], int, float]:
    """``(measured, unmeasurable, floor_bytes, floor_gib)`` for this install.

    ONE home for the measurement, shared by :func:`probe_disk_space` (which
    reports + emits) and
    ``vco_lib.deferral_probes.disk_space_still_low`` (which clears). A second
    copy would let the emit and the clear disagree about the same disk.

    Measures TWO locations, deduped by filesystem:

    * the install/project ``folder`` — clone, venvs, dist binaries, KG files;
    * the vct state dir (``$VCT_STATE_DIR`` else ``~/.vct``, resolved through
      ``vco_lib.paths.vct_root_dir``) — ``launcher.db``, hub lockfiles, the RL
      event archive, logs. Commonly a different filesystem from the clone.
    """
    resolvers = res or DoctorResolvers()
    targets: list[tuple[str, Path]] = [("install root", Path(folder))]
    try:
        from vco_lib.paths import vct_root_dir

        targets.append(("vct state dir", Path(vct_root_dir())))
    except Exception:  # noqa: BLE001 — a broken paths import is not a verdict
        pass

    measured: list[dict] = []
    unmeasurable: list[str] = []
    seen_devices: set = set()
    for label, raw_path in targets:
        existing = _nearest_existing(raw_path)
        if existing is None:
            unmeasurable.append(f"{label} ({raw_path})")
            continue
        key = _disk_device_key(existing)
        if key in seen_devices:
            continue
        usage = resolvers.resolve_disk_usage(existing)
        free = getattr(usage, "free", None)
        total = getattr(usage, "total", None)
        if not isinstance(free, int):
            unmeasurable.append(f"{label} ({existing})")
            continue
        seen_devices.add(key)
        measured.append(
            {
                "label": label,
                "path": str(existing),
                "free_bytes": free,
                "total_bytes": total if isinstance(total, int) else None,
            }
        )
    floor_bytes, floor_gib, _ = disk_min_free_bytes()
    return measured, unmeasurable, floor_bytes, floor_gib


def disk_space_below_floor(folder: Path) -> Optional[bool]:
    """Tri-state: is ANY measured mount still under the free-space floor?

    ``True`` at least one is · ``False`` every measured one is above it ·
    ``None`` nothing could be measured. The registry's clear probe for
    :data:`CID_DISK_SPACE_LOW` is a thin wrapper over this, so the reading that
    emitted the entry is the reading that clears it.
    """
    measured, _unmeasurable, floor_bytes, _gib = measure_disk_space(Path(folder))
    if not measured:
        return None
    return any(m["free_bytes"] < floor_bytes for m in measured)


def disk_dismiss_fields(folder: Path) -> dict:
    """``dismiss_key`` payload for :data:`CID_DISK_SPACE_LOW`.

    The identity is the set of MOUNT PATHS the finding is about, so a dismissal
    holds for THIS machine's layout and stops holding if the install (or the
    state dir) moves to a different filesystem — the same "dismissal keyed on
    what would have cleared it anyway" shape as the sidecar and dual-Ollama
    keys.
    """
    measured, _unmeasurable, _floor, _gib = measure_disk_space(Path(folder))
    return {"mount_paths": sorted(m["path"] for m in measured)}


def _fmt_gib(n_bytes: Optional[int]) -> str:
    if not isinstance(n_bytes, int):
        return "?"
    return f"{n_bytes / _GIB:.2f} GiB"


def probe_disk_space(folder: Path, res: DoctorResolvers, ctx: dict) -> list[Finding]:
    """Free space on the filesystems VCO actually writes to.

    Cheap — ONE ``shutil.disk_usage`` call per DISTINCT filesystem, since
    :func:`measure_disk_space` de-dupes by ``st_dev`` BEFORE measuring: two
    calls on a split install, one when the clone and the state dir share a
    filesystem. So it runs in the BOOT scope too — the moment it matters most
    is the one where the user is about to start work on a machine that can no
    longer write. Everything downstream of a full disk
    fails in a way that does not name the cause: a Weaviate write, a
    ``launcher.db`` commit, a dist-binary swap, a gzip archive of RL rows all
    surface as their own local error.

    ``defer``, never ``auto_fix``: freeing space means deleting the user's
    files, which VCO does not do unattended under any circumstances.
    """
    measured, unmeasurable, floor_bytes, floor_gib = measure_disk_space(folder, res)
    if not measured:
        return [
            Finding(
                probe="disk_space",
                status=STATUS_UNKNOWN,
                summary=(
                    "free disk space could not be measured"
                    + (f" ({'; '.join(unmeasurable)})" if unmeasurable else "")
                ),
                detail={"unmeasurable": unmeasurable},
            )
        ]

    rendered = ", ".join(
        f"{m['label']} {m['path']} {_fmt_gib(m['free_bytes'])} free" for m in measured
    )
    low = [m for m in measured if m["free_bytes"] < floor_bytes]
    if not low:
        return [
            Finding(
                probe="disk_space",
                status=STATUS_OK,
                # The cid rides the OK finding so the self-resolve pass can see
                # WHICH condition this reading clears. `deferral_entries_for`
                # only ever walks `report.problems`, so it can never emit here.
                condition_id=CID_DISK_SPACE_LOW,
                summary=(
                    f"free space above the {floor_gib:g} GiB floor: {rendered}"
                    + (f" (not measured: {'; '.join(unmeasurable)})" if unmeasurable else "")
                ),
                detail={"mounts": measured, "min_free_bytes": floor_bytes},
            )
        ]

    critical = any(m["free_bytes"] < DISK_CRITICAL_FREE_BYTES for m in low)
    return [
        Finding(
            probe="disk_space",
            status=STATUS_PROBLEM,
            summary=(
                ("CRITICALLY low" if critical else "Low")
                + f" free disk space (floor {floor_gib:g} GiB): "
                + ", ".join(
                    f"{m['label']} {m['path']} has only {_fmt_gib(m['free_bytes'])} free"
                    for m in low
                )
            ),
            fix=FIX_DEFER,
            condition_id=CID_DISK_SPACE_LOW,
            command=_disk_remediation(low, floor_gib),
            detail={
                "mounts": measured,
                "low": [m["path"] for m in low],
                "min_free_bytes": floor_bytes,
                "severity": "critical" if critical else "warning",
                "unmeasurable": unmeasurable,
            },
        )
    ]


def _disk_remediation(low: list[dict], floor_gib: float) -> str:
    """The exact block the deferral + the CLI both print.

    LOOK-FIRST, on purpose: every line is a read except the container prune,
    which is labelled. VCO never deletes the user's files, and its own advice
    should not hand them a recursive delete either — the operator decides what
    is expendable on their machine, not this text.

    v0.2.92 (R42 follow-up): the inspection lines were POSIX-only (``df``,
    ``du``), which no Windows shell provides — the same Windows-narrowing
    R42 bans. Windows now gets the PowerShell equivalents; ``podman system
    prune`` stays shared (the podman CLI is identical on Windows).
    """
    paths = " ".join(m["path"] for m in low)
    tail = (
        "#   podman system prune            # DELETES unused images/layers\n"
        f"# The floor is {floor_gib:g} GiB; raise or lower it with "
        f"{DISK_MIN_FREE_ENV}=<GiB>.\n"
        "# This entry CLEARS ITSELF on the next VCO run once space is back — "
        "nothing to dismiss."
    )
    if sys.platform == "win32":
        return (
            f"# Free space on: {paths}\n"
            "#   Get-Volume\n"
            "# Where VCO's own footprint usually sits (inspect, then decide):\n"
            '#   "{0:N1} GB" -f ((Get-ChildItem "$env:USERPROFILE\\.ollama\\models" '
            "-Recurse -File -ErrorAction SilentlyContinue | "
            "Measure-Object Length -Sum).Sum / 1GB)\n"
            '#   $vct = if ($env:VCT_STATE_DIR) { $env:VCT_STATE_DIR } '
            'else { "$env:USERPROFILE\\.vct" }\n'
            '#   "{0:N1} GB" -f ((Get-ChildItem "$vct\\logs","$vct\\rl_archive" '
            "-Recurse -File -ErrorAction SilentlyContinue | "
            "Measure-Object Length -Sum).Sum / 1GB)\n"
            "#     (rl_archive holds the pruned RL training rows — deleting it "
            "loses embeddings for good)\n"
            + tail
        )
    return (
        f"# Free space on: {paths}\n"
        f"#   df -h {paths}\n"
        "# Where VCO's own footprint usually sits (inspect, then decide):\n"
        "#   du -sh ~/.ollama/models/*        # local embedding/LLM models\n"
        "#   du -sh \"$VCT_STATE_DIR\"/logs \"$VCT_STATE_DIR\"/rl_archive\n"
        "#     (VCT_STATE_DIR defaults to ~/.vct; rl_archive holds the pruned\n"
        "#      RL training rows — deleting it loses embeddings for good)\n"
        + tail
    )


def _vco_lib_shadow_remediation(install_root: Path, origin: str) -> str:
    """The exact block the CLI prints for a shadowed ``vco_lib``.

    LOOK-FIRST like every other remediation here: the repair itself is what
    ``install.py --update`` does, and the verify line is a read.

    v0.2.92 (R23, found while auditing this file's OTHER printed commands):
    the verify line hard-coded ``.venv/bin/python``, which does not exist on
    Windows (``.venv\\Scripts\\python.exe``) — so the one command that PROVES
    the repair worked could not be run by the users on the OS that reported
    the class. It now asks
    ``install_companions.resolve_install_venv_python``, the same resolver the
    probe itself uses, and omits the line entirely when no venv is found
    rather than printing an invented path. Paths are absolutised for the same
    reason (a relative root pastes into a command that silently targets the
    wrong tree).

    v0.2.92 (R42 follow-up, the same defect one layer up): the verify line
    ALSO hard-coded ``cd /tmp &&`` — a POSIX-only path in front of a Windows
    interpreter. A Windows user cannot paste ``cd /tmp`` (cmd has no ``/tmp``
    and PowerShell maps it to a PSDrive that may not exist), so the command
    that proves the repair was un-runnable exactly where the R23 fix aimed
    it. The ``cd`` is gone entirely: ``-I`` (isolated mode) drops the cwd
    from ``sys.path`` — which was the entire reason for stepping outside the
    checkout — and also ignores ``PYTHONPATH``, making the verify line
    paste-and-run in cmd, PowerShell (any version) and every POSIX shell,
    from any directory.
    """
    root = display_path(install_root)
    lines = [
        f"# vco_lib is being imported from {origin}",
        f"# instead of {display_path(Path(install_root) / 'vco_lib')}.",
        "# Every hook, MCP and `python -m vco_lib.X` fired outside a repo root",
        "# is therefore running FROZEN install-time code: later updates change",
        "# the checkout and never the copy, so fixes silently do not take.",
        "# Repair (re-installs vco_lib editable, then sweeps a stale copy):",
        f"#   cd {root}",
        "#   python install.py --update",
    ]
    try:
        from vco_lib.install_companions import resolve_install_venv_python

        venv_python = resolve_install_venv_python(Path(install_root))
    except Exception:  # noqa: BLE001 — advice degrades, never raises
        venv_python = None
    if venv_python is not None:
        lines += [
            "# Verify afterwards (runnable from anywhere, including inside the",
            "# checkout — -I keeps the cwd and PYTHONPATH off sys.path).",
            "# It must print a path INSIDE the checkout:",
            f'#   {display_path(venv_python)} '
            '-I -c "import vco_lib; print(vco_lib.__file__)"',
        ]
    else:
        lines += [
            "# Verify afterwards with this install's venv interpreter",
            "# (the -I keeps the cwd and PYTHONPATH off sys.path) — it must",
            "# print a path INSIDE the checkout:",
            '#   python -I -c "import vco_lib; print(vco_lib.__file__)"',
        ]
    return "\n".join(lines)


def probe_vco_lib_editable(folder: Path, res: DoctorResolvers, ctx: dict) -> list[Finding]:
    """Does ``import vco_lib`` reach the CHECKOUT, or a frozen copy in the venv?

    The failure this catches shipped for several releases and was invisible by
    construction: a later install step reinstalled the orchestrator's own
    distribution WITHOUT ``-e``, which replaced the editable install with a real
    copy of ``vco_lib/`` in the venv's ``site-packages``. Nothing errored —
    imports kept working, against code frozen at install time. Every subsequent
    update changed the checkout and not the copy, so users' fixes never took
    effect and no surface said so. ``CLAUDE.md`` has carried the condition as
    FOLKLORE ("if it prints a path inside .venv/lib, re-run install.py") since
    before the code could detect it; this probe is the code catching it.

    Measured, not inferred: the origin comes from an interpreter run in a
    NEUTRAL cwd with ``PYTHONPATH`` scrubbed, because from inside the checkout
    ``sys.path[0]`` makes even a fully shadowed install look healthy.

    ``full`` scope only — one short-lived subprocess is more than the boot
    subset's file-read budget, and a shadowed install is a condition that
    changes only when an install/update runs, which is exactly when the full
    scope runs.

    Returns NO findings when ``folder`` is not an orchestrator install root
    (no ``vco_lib/__init__.py``): the question does not apply to a user project,
    and an ``unknown`` on every project doctor run would be noise, not evidence.

    ``defer``, never ``auto_fix``: the repair is a pip reinstall plus a delete
    inside the user's venv. install.py's step 4 does exactly that, with the
    positive-identification gates, as part of a run the user asked for — the
    doctor's job here is to say so, not to reach into a venv on its own.
    """
    from vco_lib.install_companions import (  # noqa: PLC0415
        ORIGIN_CHECKOUT,
        ORIGIN_FOREIGN,
        ORIGIN_SITE_PACKAGES,
        VCO_PACKAGE_NAME,
        classify_vco_lib_origin,
    )

    root = Path(folder)
    if not (root / VCO_PACKAGE_NAME / "__init__.py").is_file():
        return []

    payload = res.resolve_vco_lib_origin(root)
    if not isinstance(payload, dict):
        return [
            Finding(
                probe="vco_lib_editable",
                status=STATUS_UNKNOWN,
                summary=(
                    "could not measure where vco_lib resolves (no venv "
                    "interpreter, or the probe interpreter did not answer)"
                ),
            )
        ]

    origin = str(payload.get("origin") or "")
    purelib = str(payload.get("purelib") or "")
    state, detail = classify_vco_lib_origin(
        origin=origin, install_root=str(root), site_packages=purelib
    )
    if state == ORIGIN_CHECKOUT:
        return [
            Finding(
                probe="vco_lib_editable",
                status=STATUS_OK,
                summary=f"vco_lib resolves to the checkout ({origin})",
                detail={"origin": origin, "purelib": purelib},
            )
        ]
    if state in (ORIGIN_SITE_PACKAGES, ORIGIN_FOREIGN):
        return [
            Finding(
                probe="vco_lib_editable",
                status=STATUS_PROBLEM,
                summary=f"vco_lib is NOT the checkout — {detail}",
                fix=FIX_DEFER,
                condition_id=CID_VCO_LIB_SHADOWED,
                command=_vco_lib_shadow_remediation(root, origin or "<unknown>"),
                detail={"origin": origin, "purelib": purelib, "state": state},
            )
        ]
    return [
        Finding(
            probe="vco_lib_editable",
            status=STATUS_UNKNOWN,
            summary=f"vco_lib origin undetermined — {detail}",
            detail={
                "origin": origin,
                "purelib": purelib,
                "error": str(payload.get("error") or ""),
            },
        )
    ]


def probe_code_embed_image(folder: Path, res: DoctorResolvers, ctx: dict) -> list[Finding]:
    """Is the RUNNING code-embedding service built from the source in this tree?

    Every other compose service pulls a pinned upstream image. ``code_embed``
    is BUILT from ``claude_mcp_servers/code_embedding_service``, and both
    ``docker compose up`` and ``podman-compose up`` build only when the image
    is MISSING — a changed build context is not a rebuild trigger, and
    ``--force-recreate`` replaces the container from the SAME image. So the
    v0.2.92 fix that makes the service REFUSE over-window input (instead of
    truncating it silently at HTTP 200) was correct in ``server.py``, pinned
    by its own tests, and running nowhere.

    The evidence is positive and self-reported: the service hashes the source
    files it actually loaded and publishes the digest on ``/health``. This
    probe compares it with the checkout's. Nothing is inferred from an image
    tag, a timestamp, or a build stamp that could lie.

    Verdict mapping (the ``unknown`` arms are load-bearing — a service that
    does not answer is NOT evidence that it is current):

    * ``current`` → ok;
    * ``stale``   → problem + :data:`CID_CODE_EMBED_IMAGE_STALE`, with the
      explicit compose command; note the ``/health``-without-``source_sha``
      arm, which positively identifies a pre-v0.2.92 image — the whole
      population that is still losing text silently;
    * ``unknown`` → unknown (no service source in the tree, e.g. a per-project
      install; service down; service could not hash itself).

    ``full`` scope only: it is one localhost HTTP call with a short timeout,
    which is more than the boot subset's file-read budget, and the answer
    changes when an install/update runs — exactly when the full scope runs.

    Returns NO findings when ``folder`` is not an orchestrator install root:
    a user project neither builds nor owns this image, and an ``unknown`` on
    every project run would be noise rather than evidence.
    """
    from vco_lib import code_embed_image  # noqa: PLC0415

    root = Path(folder)
    if not (root / "vco_lib" / "__init__.py").is_file():
        return []

    state = res.resolve_code_embed_state(root)
    detail = state.to_dict() if hasattr(state, "to_dict") else {}
    if getattr(state, "verdict", code_embed_image.UNKNOWN) == code_embed_image.CURRENT:
        return [
            Finding(
                probe="code_embed_image",
                status=STATUS_OK,
                summary=state.summary,
                detail=detail,
            )
        ]
    if getattr(state, "verdict", None) == code_embed_image.STALE:
        return [
            Finding(
                probe="code_embed_image",
                status=STATUS_PROBLEM,
                summary=state.summary,
                fix=FIX_DEFER,
                condition_id=CID_CODE_EMBED_IMAGE_STALE,
                # The reading that produced this finding is the image state;
                # the rebuild context only decides how the remedy is PHRASED.
                # A context that could not be read must therefore cost the
                # phrasing, never the finding — dropping a STALE verdict
                # because a container label was unreadable would lose the
                # data-integrity signal to a cosmetic failure. (The seam's own
                # default is soft-fail; this covers an INJECTED one that is
                # not, which is how the gap was found.)
                command=_code_embed_rebuild_remediation(
                    root, _safe_rebuild_context(res),
                ),
                detail=detail,
            )
        ]
    return [
        Finding(
            probe="code_embed_image",
            status=STATUS_UNKNOWN,
            summary=getattr(state, "summary", "code_embed: image state unknown."),
            detail=detail,
        )
    ]


def probe_model_gateway_runnable(folder: Path, res: DoctorResolvers, ctx: dict) -> list[Finding]:
    """Can the gateway's login-time registration actually run?

    Returns NO findings when the gateway is not registered, which is the
    DEFAULT and the majority case: autostart is opt-in, and an ``unknown`` line
    on every machine that never opted in is noise, not evidence (the
    ``code_embed_image`` precedent).

    The reading is :func:`vco_lib.gateway_ensure.gateway_status` — the same one
    the SessionStart ensure acts on and the same one the clear probe re-runs —
    so the three surfaces cannot disagree about a single machine. It reads the
    argv back out of the INSTALLED artefact rather than re-resolving it: "what
    would we write now" is a different question from "what does the thing on
    this machine run", and only the second one could have caught 2026-09-10.

    ``full`` scope only: it runs the registered entry point with ``--version``,
    which is a process spawn — past the boot subset's file-read budget, and the
    answer changes exactly when an install/update runs.
    """
    from vco_lib import gateway_ensure  # noqa: PLC0415

    found = res.resolve_gateway_state()
    state = getattr(found, "state", None)
    if state is None or state is gateway_ensure.GatewayState.NOT_REGISTERED:
        return []
    detail = found.to_dict() if hasattr(found, "to_dict") else {}
    if state is gateway_ensure.GatewayState.REGISTERED_BUT_UNRUNNABLE:
        return [
            Finding(
                probe="model_gateway_runnable",
                status=STATUS_PROBLEM,
                summary=f"model gateway: {found.reason}",
                fix=FIX_DEFER,
                condition_id=CID_GATEWAY_UNRUNNABLE,
                command=remedy_shell.steps(
                    f"cd {remedy_shell.quote(Path(folder).resolve())}",
                    "python install.py --update",
                ),
                detail=detail,
            )
        ]
    return [
        Finding(
            probe="model_gateway_runnable",
            status=STATUS_OK,
            # The cid rides the OK finding so the self-resolve pass can see
            # WHICH condition this reading clears (the `disk_space` pattern).
            # `deferral_entries_for` only ever walks `report.problems`, so it
            # can never emit from here.
            condition_id=CID_GATEWAY_UNRUNNABLE,
            summary=f"model gateway: {found.reason}",
            detail=detail,
        )
    ]


def _safe_rebuild_context(res: DoctorResolvers):
    """The rebuild context, or the default one — never a raise.

    ``None`` is returned by nothing here: the remediation always gets a
    context, so the second step is always printed. See the call site for why
    the finding must outlive a failure to read this.
    """
    from vco_lib import code_embed_image  # noqa: PLC0415

    try:
        return res.resolve_code_embed_rebuild_context()
    except Exception:  # noqa: BLE001 — phrasing must not cost the finding
        return code_embed_image.RebuildContext()


def _code_embed_rebuild_remediation(root: Path, context=None) -> str:
    """The command that refreshes the image, ORDERED against the code-graph re-sync.

    Order is load-bearing and is the reason this is one string rather than two
    lines the user might reorder: re-walking the code graph FIRST would embed
    every entity through the old, truncating service and then report success.

    v0.2.95 F2 — why there is a SECOND step. ``install.py --update`` refreshes
    the image only where the installer's own compose project owns the
    container. Where it does not, step 5 applies
    :func:`vco_lib.install_services_guard.apply_recreate_guard`, which strips
    ``code_embed`` from ``build_services`` (correctly — a build under a
    different project name produces an image the running container never
    loads) and prints ``[skip-recreate] code_embed``. The entry then told the
    user to run the update that had just refused, on a condition whose live
    cost is code silently truncated into the code graph: a closed loop, and a
    printed command that could not help. The explicit rebuild — built by
    :func:`vco_lib.code_embed_image.rebuild_command` from the OWNING project's
    own labels when there is one — is the leg that can.

    ``context`` is :class:`vco_lib.code_embed_image.RebuildContext`; ``None``
    (a caller that has no machine reading) falls back to the default context,
    so the step is always present and never invented.
    """
    from vco_lib import code_embed_image  # noqa: PLC0415

    ctx = context if context is not None else code_embed_image.RebuildContext()
    owner = getattr(ctx, "identity", None)
    explicit = code_embed_image.rebuild_command(
        Path(root).resolve(),
        compose_cmd=getattr(ctx, "compose_cmd", "docker compose"),
        identity=owner,
    )
    note = (
        "# 2. The running container was created by compose "
        f"{owner.describe()},\n"
        "#    so step 1 will NOT rebuild its image — it prints "
        "`[skip-recreate] code_embed`\n"
        "#    and leaves the service on the old, TRUNCATING image. Rebuild "
        "inside the\n"
        "#    project that owns it:"
        if owner is not None and getattr(owner, "working_dir", "")
        else
        "# 2. If step 1 printed `[skip-recreate] code_embed`, its compose did "
        "NOT own the\n"
        "#    running container and the image was NOT rebuilt. Rebuild it "
        "explicitly:"
    )
    return remedy_shell.steps(
        "# 1. The update rebuilds the image where its own compose owns the "
        "container:",
        f"cd {remedy_shell.quote(Path(root).resolve())}",
        "python install.py --update",
        note,
        explicit,
    )


def probe_kg_binding_evidence(folder: Path, res: DoctorResolvers, ctx: dict) -> list[Finding]:
    """Does every registered project's KG data live where its binding says?

    D18's recorded symptom — a project reading and writing a ghost collection
    while the binding says otherwise — is invisible to every healer by
    construction: the update-time prefix-adopt pass skips rows whose class
    EXISTS in Weaviate, and a ghost that received writes exists. This probe
    is the READ-ONLY diagnostic half of the closure
    (:mod:`vco_lib.kg_binding_doctor`): it compares, per registered project,
    the primary binding against FILE-BACKED evidence of where the data lives
    and against the name-derived expected class, and DEFERS naming all three
    values when they disagree. This probe never writes a binding — R38
    records that the previous "fix" here would have re-stamped the ghost.

    Since v0.2.92 the same measurement also feeds a WRITER
    (``kg_binding_heal.plan_evidence_repoints``), which re-points the
    binding on the next install/update when one class clears the ownership
    bar with no rival to it (alone, or leading the runner-up by the heal's
    decisive margin) and it is not the one already bound. So what survives to
    THIS finding is what the heal deliberately REFUSED: a split with no
    decisive leader (which the heal now also ASKS about, via
    ``kg_binding_ambiguous_evidence``), a ``manual_override`` row, or a target
    another binding row names. Those the human repairs from the launcher's
    Identity tab (``update_project_identity`` re-writes the binding and
    re-projects the project env from it).

    ``None`` from the scan (launcher.db unreadable, Weaviate unreachable)
    emits NOTHING — not even an ``unknown`` finding: a backend that cannot
    be looked at must never read as "the class is missing" nor as "all
    agreed". A completed scan with zero mismatches emits an OK finding that
    CARRIES the cid, so the self-resolving pass clears an entry the reading
    that emitted it would not have re-emitted (the disk_space contract).

    ``full`` scope only, for cost and for the boot-ledger promise: the scan
    is one schema listing plus one count and one bounded sample per
    ``*_KnowledgeGraph`` class — network reads with no boot-scope business
    slowing the launcher's start — and a binding changes only through the
    launcher or an install/update, which is exactly when this scope runs.
    """
    scan = res.resolve_kg_binding_evidence()
    if scan is None:
        return []
    from vco_lib.kg_binding_doctor import jsonable_verdicts, render_three_values

    mismatches = list(scan.mismatches)
    unclaimed = list(scan.unclaimed)
    if not mismatches:
        checked = len(scan.verdicts)
        # The per-project comparison AGREED, so the D18 self-resolve finding
        # is owed on EVERY branch below — including the unclaimed one. The
        # disk_space contract ("the reading that emitted the entry is the
        # reading that clears it") must not become conditional on a SECOND,
        # independent condition happening to be clean: a machine that fixed
        # its mismatch but has leftover classes would otherwise keep a stale
        # kg_binding_evidence_mismatch entry until the install-time re-probe.
        agreement = Finding(
            probe="kg_binding_evidence",
            status=STATUS_OK,
            # The cid rides the OK finding so the self-resolve pass can
            # see WHICH condition this reading clears (disk_space shape).
            condition_id=CID_KG_BINDING_EVIDENCE_MISMATCH,
            summary=(
                f"{checked} registered project(s) with a primary KG "
                "binding: every one matches where its data demonstrably "
                "lives"
            ),
            detail={"unclaimed": [u.name for u in unclaimed]},
        )
        extra_fixture = list(getattr(scan, "fixture_shaped", ()) or ())
        if not unclaimed and not extra_fixture:
            return [agreement]
        # v0.2.92 (reported-not-fixed item 3): every REGISTERED project
        # agrees, but populated classes exist that no registered project
        # accounts for. Problem status (not OK-with-a-footnote) because the
        # entire defect was invisibility: an OK finding is rendered nowhere
        # but the CLI text, while a problem finding reaches the ledger and
        # the launcher's Updates page. The remedy text is LOOK-only — this
        # probe names data; it never proposes deleting it.
        return [
            Finding(
                probe="kg_binding_evidence",
                status=STATUS_PROBLEM,
                summary=_kg_unclaimed_summary(unclaimed, extra_fixture),
                fix=FIX_DEFER,
                condition_id=CID_KG_UNCLAIMED,
                command=_kg_unclaimed_remediation(unclaimed, extra_fixture),
                detail={
                    "unclaimed": [
                        {"class": u.name, "count": u.count} for u in unclaimed
                    ],
                    "fixture_shaped": extra_fixture,
                    "verdicts": jsonable_verdicts(scan),
                },
            ),
            agreement,
        ]
    lines = "; ".join(render_three_values(v) for v in mismatches)
    evidence_classes = sorted(
        {e.name for v in mismatches for e in v.unbound_evidence}
    )
    from vco_lib.kg_binding_doctor import jsonable_verdicts

    return [
        Finding(
            probe="kg_binding_evidence",
            status=STATUS_PROBLEM,
            summary=(
                f"{len(mismatches)} project(s) whose KG data lives in a class "
                f"the primary binding does not name — {lines}"
            ),
            fix=FIX_DEFER,
            condition_id=CID_KG_BINDING_EVIDENCE_MISMATCH,
            command=_kg_binding_evidence_remediation(mismatches),
            detail={
                "projects": [v.project_name for v in mismatches],
                "evidence_classes": evidence_classes,
                # The three values, machine-readable, for `vco doctor --json`.
                "verdicts": jsonable_verdicts(scan),
                # v0.2.92 item 3: unclaimed classes ride the mismatch
                # finding's DETAIL (its summary/dismiss-key are the D18
                # contract) — the dedicated finding + entry come on the
                # first pass clean of mismatches.
                "unclaimed": [u.name for u in scan.unclaimed],
            },
        )
    ]


def _fixture_shaped_names(names) -> list[str]:
    """The subset of *names* whose project stem is one of VCO's test fixtures.

    ONE home for the question, asked by the summary, the remedy text and the
    deferral entry — three surfaces that must agree about which classes are
    fixture residue. The rule itself lives in
    :mod:`vco_lib.fixture_class_guard`, beside the write guard that stops new
    ones being created; this is only the doctor's read of it.
    """
    from vco_lib.fixture_class_guard import fixture_stem_of

    return [n for n in names if fixture_stem_of(n) is not None]


def _kg_unclaimed_summary(unclaimed, extra_fixture=()) -> str:
    """The one-line diagnosis for populated classes no project claims.

    A fixture-shaped class is a DIFFERENT diagnosis from a removed project's
    leftover, and the difference is actionable: there is no owning project to
    re-add, and there never was one. The 2026-09 field case was
    ``Alpha_KnowledgeGraph`` with 70 real knowledge nodes — ``Alpha`` being a
    name that exists only in this repo's tests. Saying "a removed project's
    leftover" there sends the reader looking for a project that never existed.
    """
    fixture = set(_fixture_shaped_names(u.name for u in unclaimed))
    names = ", ".join(
        f"{u.name} ({u.count} objects"
        + (", fixture-shaped)" if u.name in fixture else ")")
        for u in unclaimed
    )
    extra = list(extra_fixture or ())
    # v0.2.94 (review LOW-1): the ownership analysis above is KG-scoped, so
    # `Foo_Diagrams` and `<Fixture>_Code*` never reached it. They need no
    # analysis — the stem is a name no project has — so they are named here.
    tail = (
        f" Plus {len(extra)} fixture-shaped class(es) in other families: "
        f"{', '.join(extra)}."
        if extra else ""
    )
    if not unclaimed:
        return (
            f"{len(extra)} fixture-shaped class(es) — written by a test or "
            f"probe harness, not by any project: {', '.join(extra)}"
        )
    if not fixture:
        return (
            f"{len(unclaimed)} populated KG class(es) no registered "
            f"project claims: {names}" + tail
        )
    return (
        f"{len(unclaimed)} populated KG class(es) no registered project "
        f"claims; {len(fixture)} of them fixture-shaped ghost(s) — written "
        f"by a test or probe harness, not by any project: {names}" + tail
    )


def _kg_unclaimed_remediation(unclaimed, extra_fixture=()) -> str:
    """The exact block the deferral + the CLI both print. LOOK-only.

    The constraint is absolute (v0.2.92 reported-not-fixed item 3): a
    populated Weaviate class is hours of user work, so this text names
    NON-destructive resolutions only — re-add the project, re-bind via the
    Identity tab, or migrate. It deliberately does NOT print a drop command:
    shipping one, even unexecuted, is shipping a deletion path, and the
    machine's own Weaviate tooling is where that decision belongs. Dismissal
    is the recorded "this leftover is expected" answer.

    v0.2.94 adds a leading block for the FIXTURE-SHAPED subset, because the
    three resolutions below all presuppose an owning project and a fixture
    ghost has none. That block stays inside the same constraint: the parity
    check it prints is a READ, and the drop it describes is prose the user
    performs — a destructive step this text still refuses to hand over as a
    runnable command.
    """
    names = ", ".join(sorted(u.name for u in unclaimed)) or "(none)"
    fixture = sorted(
        set(_fixture_shaped_names(u.name for u in unclaimed))
        | set(extra_fixture or ())
    )
    fixture_block = ""
    if fixture:
        # A fixture-shaped ghost needs DIFFERENT instructions: the three
        # resolutions below (re-add the project / re-bind it / migrate) all
        # assume an owning project, and there is none. Still LOOK-only — the
        # parity check is a read, and the destructive step stays prose the
        # user performs with their own tooling, never a command this text
        # prints (v0.2.92 item 3's constraint holds here too: shipping a drop
        # command, even unexecuted, is shipping a deletion path).
        fixture_block = (
            f"# Fixture-shaped: {', '.join(fixture)}\n"
            "#   The name before '_KnowledgeGraph' is one of VCO's own TEST\n"
            "#   FIXTURE project names (vco_lib/fixture_class_guard.py,\n"
            "#   FIXTURE_PROJECT_NAMES). No project owns this data and none\n"
            "#   ever did: a test or an ad-hoc probe harness reached a live\n"
            "#   Weaviate under a fixture's environment. Nothing below\n"
            "#   applies — there is no project to re-add.\n"
            "#   VERIFY PARITY FIRST (both reads, nothing is changed):\n"
            "#     curl -s \"$WEAVIATE_URL/v1/objects?class=<class>&limit=5\"\n"
            "#     compare title/file_path against your real KG collection\n"
            "#   If every object also exists in the project collection it was\n"
            "#   copied from, the ghost is a duplicate and dropping it loses\n"
            "#   nothing. THAT DROP IS YOURS TO MAKE: VCO does not print the\n"
            "#   command and never performs it. If parity does NOT hold, the\n"
            "#   ghost holds the only copy — migrate it before anything else\n"
            "#   (python -m vco_lib.project_init migrate-collections --help).\n"
            "#   New writes of this shape are REFUSED since v0.2.94, so this\n"
            "#   set cannot grow: the guard is vco_lib/fixture_class_guard.py.\n"
        )
    return (
        f"# Classes affected: {names}\n"
        + fixture_block
        + "# These classes hold objects but no registered project reads them.\n"
        "# Nothing was changed by this report and nothing is deleted by it.\n"
        "# If the owning project still exists: re-add it (launcher Projects\n"
        "#   page -> Add existing folder) so its binding row returns.\n"
        "# To have a CURRENT project own this data: launcher -> the project's\n"
        "#   page -> Identity tab -> pick the class as the primary KG\n"
        "#   collection (the picker does not move existing objects).\n"
        "# To move objects between collections:\n"
        "#   python -m vco_lib.project_init migrate-collections --help\n"
        "# If the leftover is expected: dismiss this entry — the dismissal\n"
        "#   records that choice.\n"
        "# Then confirm: vco doctor\n"
    )


def _kg_unclaimed_entry(finding: Finding):
    """The deferral the unclaimed-classes reading owes — names, not actions.

    ``dismiss_fields`` keys the dismissal on the affected class set (the same
    shape ``kg_binding_evidence_mismatch`` uses): cosmetic rewording cannot
    re-fire a dismissal, but a NEW unclaimed class must. Clearing is the
    registry probe's job (``kg_unclaimed_classes_still_present``): the entry
    survives exactly as long as the unclaimed set is nonempty, so re-binding
    or re-adding the project drops it on the next install/update re-probe —
    no manual step owed for the resolutions that fix the state itself.
    """
    from vco_lib.deferral_report import DeferralEntry

    classes = sorted(
        {
            str(c["class"])
            for c in (finding.detail.get("unclaimed") or [])
            if isinstance(c, dict) and isinstance(c.get("class"), str)
        }
        # v0.2.94 LOW-1: non-KG fixture-shaped classes key the dismissal too,
        # so a NEW one re-fires an entry the user had dismissed.
        | {
            str(n) for n in (finding.detail.get("fixture_shaped") or [])
            if isinstance(n, str)
        }
    )
    fixture = sorted(_fixture_shaped_names(classes))
    fixture_note = (
        (
            f"{len(fixture)} of these are FIXTURE-SHAPED "
            f"({', '.join(fixture)}): the stem is one of VCO's own test "
            "fixture project names, so no project ever owned the data — a "
            "test or an ad-hoc probe harness reached a live Weaviate under a "
            "fixture's environment. There is nothing to re-add or re-bind; "
            "the printed remedy explains the parity check and leaves the "
            "drop to you. New writes of this shape are refused since "
            "v0.2.94, so the set cannot grow. "
        )
        if fixture
        else ""
    )
    return DeferralEntry(
        condition_id=CID_KG_UNCLAIMED,
        title="Populated KG class(es) no registered project claims",
        detected=finding.summary,
        why_deferred=(
            fixture_note
            + "This is a diagnosis, not a defect report: the class(es) hold "
            "real objects while no registered project's binding names them "
            "and no registered project's folder anchors their sampled "
            "paths — data with no reader, typically a removed project's "
            "leftover. Choosing what unclaimed data is (re-add the project, "
            "re-bind it to a current project, migrate the objects, or "
            "accept the leftover) is the user's call, and VCO never "
            "proposes deleting it — a populated collection is hours of "
            "work and the printed remedy intentionally offers no drop "
            "command. The entry clears itself via the registered clear "
            "probe once the class is claimed again or emptied."
        ),
        command_to_apply=finding.command,
        severity="info",
        dismiss_fields={"classes": classes},
        kg_node_refs=["docs/TROUBLESHOOTING.md"],
    )


def _kg_binding_evidence_remediation(mismatches) -> str:
    """The exact block the deferral + the CLI both print. LOOK-only.

    The repair is a GUI action (pick the right class in the Identity tab),
    not a command this text can run for the user — and deliberately so: a
    command that re-wrote the binding would re-introduce the blind healer
    this condition exists to replace. What reaches this text is what the
    automated evidence heal REFUSED (ambiguous evidence, a ``manual_override``
    row, a target another binding row already names); the unambiguous cases
    were re-pointed already and are reported as
    ``kg_binding_evidence_repointed``. The re-check line is a read.
    """
    names = ", ".join(sorted(v.project_name for v in mismatches))
    return (
        f"# Projects affected: {names}\n"
        "# The automatic evidence-backed repoint did NOT act on these: either\n"
        "#   more than one class holds the project's files, or the binding is\n"
        "#   a manual override, or the class is already bound elsewhere.\n"
        "# Open the launcher -> the project's page -> Identity tab and PICK,\n"
        "# as the primary KG collection, the class the report names as holding\n"
        "# the project's data (the evidence line's UNBOUND class). Saving the\n"
        "# identity re-writes the binding AND re-projects the project env from\n"
        "# it, so both the DB and .claude/settings.json name the same class.\n"
        "# Objects already written into the ghost are NOT moved by the picker:\n"
        "# decide separately whether to keep them there or migrate\n"
        "# (python -m vco_lib.project_init migrate-collections --help).\n"
        "# Then confirm: vco doctor\n"
        "# This entry CLEARS ITSELF when the comparison agrees again."
    )


# ---------------------------------------------------------------------------
# Source currency — "is this checkout the code the project ships today?"
#
# THE probe this whole work package exists for. Everything above answers a
# question about the environment AROUND the install; this one asks whether the
# install itself is the current one, which is what a five-week silent outage
# looked like from the inside: internally consistent, and stale.
# ---------------------------------------------------------------------------

#: The remote every VCO update surface compares against.
#:
#: NOT ``origin``: the orchestrator ships into private forks and customer
#: mirrors, where ``origin`` is the fork and answering the currency question
#: against it would answer a DIFFERENT question with a plausible number. The
#: launcher pins the same name (``self_update.rs::VCO_UPSTREAM_REMOTE``).
UPSTREAM_REMOTE = "vco_upstream"

#: Branch to compare against when HEAD names none.
#:
#: MUST MATCH ``launcher/src-tauri/src/commands/git_cmd.rs::FALLBACK_BRANCH``.
#: Pinned by ``tests/test_v0292_n2d_source_currency.py`` — a cross-language
#: mirror of a two-token rule, kept because the Python doctor cannot shell the
#: launcher binary (and must not learn to).
SOURCE_FALLBACK_BRANCH = "main"

#: Wall-clock ceiling for ONE git invocation, seconds. The network leg
#: (``ls-remote``) is the long pole; every other call is local plumbing.
GIT_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class SourceFacts:
    """Everything the currency probe measured about a checkout, tri-state.

    Every ``Optional[bool]`` / ``Optional[int]`` here means the same thing when
    it is ``None``: **the measurement did not happen or did not succeed.** No
    field collapses a failed git call into a value that reads as healthy —
    which is the specific defect that produced the incident (``.unwrap_or(0)``
    on a ``rev-list`` that could not run, then ``count > 0`` as the verdict).
    """

    #: ``True`` only when ``folder`` IS the top level of a git work tree.
    #: A folder that merely SITS INSIDE one is ``False``: answering the
    #: currency question about an enclosing repo is the wrong-question trap.
    is_git_toplevel: bool = False
    #: ``True`` detached, ``False`` on a branch, ``None`` undetermined.
    detached: Optional[bool] = None
    #: Branch used for the comparison — the attached one, else the fallback.
    branch: str = SOURCE_FALLBACK_BRANCH
    head_sha: Optional[str] = None
    remote: str = UPSTREAM_REMOTE
    #: ``True`` the remote exists, ``False`` provably not, ``None`` unknown.
    remote_configured: Optional[bool] = None
    #: Whether the network leg was attempted at all this pass.
    remote_asked: bool = False
    #: Tip SHA the REMOTE advertises for ``branch`` (never derived from HEAD).
    remote_sha: Optional[str] = None
    #: ``True`` this checkout contains the upstream tip (level or ahead),
    #: ``False`` provably not, ``None`` undetermined.
    contains_remote_tip: Optional[bool] = None
    #: Exact commits behind the upstream tip, when it is present locally.
    behind: Optional[int] = None
    #: Commits behind the LAST-FETCHED ``<remote>/<branch>`` ref. A lower
    #: bound with no freshness guarantee — see :func:`collect_source_facts`.
    behind_local_ref: Optional[int] = None
    #: Does a local branch of :attr:`branch` exist? Gates the remediation:
    #: printing ``git checkout main`` when no such branch exists is a command
    #: that cannot work.
    local_branch_exists: Optional[bool] = None
    #: leg → the git error text, for the ``unknown`` summaries.
    errors: dict = field(default_factory=dict)


def _git(
    repo: Path, args: Sequence[str], *, timeout: int = GIT_TIMEOUT_SECONDS
) -> tuple[Optional[int], str, str]:
    """Run read-only ``git -C <repo> <args>``. Returns ``(rc, out, err)``.

    ``rc is None`` means git could not be RUN at all (absent, timed out,
    spawn failure) — distinct from a git that ran and exited non-zero, because
    the two justify different verdicts: the first is ``unknown``, the second is
    often a positive answer (``symbolic-ref`` exits 1 to SAY "detached").

    ``GIT_TERMINAL_PROMPT=0`` is not decoration: a ``ls-remote`` against a
    remote whose credentials are missing will otherwise block on a password
    prompt with no terminal to type into, and a health check that hangs is
    worse than one that says "I could not check". The timeout is the backstop
    for every other way a network call can wedge.

    Every command this module passes is READ-ONLY plumbing. Nothing here
    fetches, writes a ref, takes a lock, or mutates the user's repository —
    ``vco doctor`` is a report, and a report that changes what it reports on
    cannot be run twice with confidence.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(  # noqa: S603 — argv is ours, never shell
            ["git", "-C", str(repo), *args],
            capture_output=True,
            # Pinned decode rather than the locale default: on a Windows
            # runner `text=True` decodes with cp1252, and a repo path holding
            # a non-ASCII character would raise UnicodeDecodeError out of an
            # arm that catches only OSError/SubprocessError — a probe crash
            # (rendered `unknown`, but noisily) on nothing worse than an
            # accented directory name.
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return None, "", f"git {' '.join(args)} timed out after {timeout}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "", f"git {' '.join(args)} could not run: {exc}"
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def same_location(
    a: str,
    b: str,
    *,
    normcase: Optional[Callable[[str], str]] = None,
    realpath: Optional[Callable[[str], str]] = None,
) -> bool:
    """Do ``a`` and ``b`` name the same place on THIS OS?

    Two OS-dependent decisions, both injectable so a test can drive the
    Windows shape from a POSIX runner (and vice versa) instead of asserting
    one platform and hoping:

    * ``realpath`` — follows symlinks/junctions, so a PATH symlink and its
      target compare equal.
    * ``normcase`` — on Windows lowercases AND folds ``/`` to ``\\``, which is
      exactly what makes ``C:/repos/vco`` (git's output shape) comparable
      with ``C:\\Repos\\VCO`` (the filesystem's). On POSIX it is the identity, so
      case stays significant, which is also correct there.

    Never raises: an unresolvable path is "not the same place".
    """
    nc = normcase or os.path.normcase
    rp = realpath or os.path.realpath
    try:
        return nc(rp(a)) == nc(rp(b))
    except (OSError, ValueError):
        return False


def collect_source_facts(folder: Path, *, ask_remote: bool = True) -> SourceFacts:
    """Measure ``folder``'s currency against :data:`UPSTREAM_REMOTE`.

    With ``ask_remote=False`` this performs **no network I/O**, and the
    currency verdict built from it can then never be ``ok``: a local
    remote-tracking ref carries no evidence of WHEN it was last updated, and
    the launcher's own fetch passes ``--no-write-fetch-head``
    (``self_update.rs::serialized_fetch_upstream``), so not even
    ``FETCH_HEAD``'s mtime answers it. Reporting "level with
    ``vco_upstream/main``" off a ref last written five weeks ago would be a
    fresh instance of the exact defect this probe exists to catch, so the
    local ref is only ever allowed to CONVICT (``behind_local_ref > 0`` is
    positive evidence of staleness), never to acquit.
    """
    facts: dict = {"remote": UPSTREAM_REMOTE, "remote_asked": False}
    errors: dict = {}

    rc, out, err = _git(folder, ["rev-parse", "--show-toplevel"])
    if rc is None:
        errors["git"] = err
        return SourceFacts(errors=errors, **facts)
    if rc != 0 or not out:
        # Not a work tree (a release tarball install, or a folder git refuses
        # to answer for). NOT an error: the probe declines rather than
        # inventing a verdict.
        return SourceFacts(errors=errors, **facts)
    if not same_location(out, str(folder)):
        # `folder` sits inside SOME repo, but is not its root. Answering about
        # the enclosing repo would be a true statement about the wrong tree.
        errors["toplevel"] = f"{folder} is not the root of the git repo at {out}"
        return SourceFacts(errors=errors, **facts)
    facts["is_git_toplevel"] = True

    # (a) Detached HEAD. `symbolic-ref -q` exits 1 to SAY "not a symbolic
    # ref" — a positive answer, not a failure. `rev-parse --abbrev-ref HEAD`
    # returns the literal string "HEAD" for the same state, which is how five
    # inline call-sites destroyed the fact while normalising it.
    rc, out, err = _git(folder, ["symbolic-ref", "-q", "--short", "HEAD"])
    if rc == 0 and out:
        facts["detached"] = False
        facts["branch"] = out
    elif rc == 1:
        facts["detached"] = True
        facts["branch"] = SOURCE_FALLBACK_BRANCH
    else:
        errors["detached"] = err or f"git symbolic-ref exited {rc}"

    rc, out, err = _git(folder, ["rev-parse", "HEAD"])
    if rc == 0 and out:
        facts["head_sha"] = out
    else:
        errors["head"] = err or f"git rev-parse HEAD exited {rc}"

    branch = facts.get("branch", SOURCE_FALLBACK_BRANCH)
    rc, _out, _err = _git(
        folder, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"]
    )
    if rc is not None:
        facts["local_branch_exists"] = rc == 0

    rc, _out, err = _git(folder, ["remote", "get-url", UPSTREAM_REMOTE])
    if rc is None:
        errors["remote"] = err
    else:
        facts["remote_configured"] = rc == 0

    # The local remote-tracking ref: a lower bound that may convict.
    rc, out, _err = _git(
        folder, ["rev-list", "--count", f"HEAD..{UPSTREAM_REMOTE}/{branch}"]
    )
    if rc == 0 and out.isdigit():
        facts["behind_local_ref"] = int(out)

    if not ask_remote or facts.get("remote_configured") is not True:
        return SourceFacts(errors=errors, **facts)

    # (b) Ask the REMOTE for its tip. `ls-remote` reads; it does not fetch,
    # write a ref, or add an object to the repo.
    facts["remote_asked"] = True
    rc, out, err = _git(
        folder, ["ls-remote", "--exit-code", UPSTREAM_REMOTE, f"refs/heads/{branch}"]
    )
    if rc == 2:
        # `--exit-code` documents 2 as "no matching refs" — a POSITIVE answer
        # that this branch does not exist upstream (a local feature branch, a
        # renamed default). Distinct from "the remote could not be reached",
        # and the reader needs to know which of the two happened.
        errors["remote_tip"] = (
            f"`{branch}` does not exist on {UPSTREAM_REMOTE}, so there is no "
            "upstream tip to compare this checkout against"
        )
        return SourceFacts(errors=errors, **facts)
    if rc != 0 or not out:
        errors["remote_tip"] = err or f"git ls-remote exited {rc}"
        return SourceFacts(errors=errors, **facts)
    fields = out.split()
    remote_sha = fields[0].strip() if fields else ""
    if not remote_sha:
        errors["remote_tip"] = f"unparseable ls-remote output: {out!r}"
        return SourceFacts(errors=errors, **facts)
    facts["remote_sha"] = remote_sha

    head_sha = facts.get("head_sha")
    if head_sha and head_sha == remote_sha:
        facts["contains_remote_tip"] = True
        facts["behind"] = 0
        return SourceFacts(errors=errors, **facts)

    # Is the advertised tip even IN this repository? Its ABSENCE is positive
    # evidence: a commit we do not have cannot be one we contain. That arm is
    # what makes the verdict work for the incident shape — a clone that never
    # fetched has no object to run ancestry against.
    rc, _out, err = _git(folder, ["cat-file", "-e", f"{remote_sha}^{{commit}}"])
    if rc is None:
        errors["ancestry"] = err
        return SourceFacts(errors=errors, **facts)
    if rc != 0:
        facts["contains_remote_tip"] = False
        return SourceFacts(errors=errors, **facts)

    rc, _out, err = _git(folder, ["merge-base", "--is-ancestor", remote_sha, "HEAD"])
    if rc == 0:
        facts["contains_remote_tip"] = True
    elif rc == 1:
        facts["contains_remote_tip"] = False
        rc2, out2, _err2 = _git(folder, ["rev-list", "--count", f"HEAD..{remote_sha}"])
        if rc2 == 0 and out2.isdigit():
            facts["behind"] = int(out2)
    else:
        errors["ancestry"] = err or f"git merge-base exited {rc}"
    return SourceFacts(errors=errors, **facts)


def _short(sha: Optional[str]) -> str:
    return (sha or "?")[:12]


def display_path(path: "str | Path") -> str:
    """Absolute, symlink-resolved rendering of ``path`` for a PRINTED command.

    Found by running the probe for real (``vco doctor`` from a relative cwd):
    the folder a caller passes may be ``.``, and a remediation that then reads
    ``ln -sfn tools/vct-secrets/vct ~/.local/bin/vct`` creates a BROKEN symlink
    if pasted from anywhere else — a printed command that cannot work, which is
    the exact category R16 bans. Every path a remediation string interpolates
    goes through here.

    Soft-fails to the input on any resolution error: a path we cannot resolve
    is still better rendered than dropped.
    """
    try:
        return str(Path(path).resolve())
    except (OSError, RuntimeError, ValueError):
        return str(path)


def _reattach_remediation(folder: Path, facts: SourceFacts) -> str:
    """The block printed for a detached HEAD. LOOK-FIRST, never destructive.

    Every line up to the last is a READ. The one action is ``git checkout``,
    which git itself refuses rather than overwriting anything — and it is
    printed only when the branch it names EXISTS locally, because a printed
    command that cannot work is a promise this release forbids.

    The ancestry line is not decoration either: checking out the branch while
    HEAD holds commits upstream does not have would leave them unreferenced.
    The launcher's one-click reattach (v0.2.92+, Preferences → Launcher
    updates) applies the same three guards — clean tree, detached, contained
    upstream — so the two paths cannot disagree about when reattaching is safe.

    v0.2.92 (R42 sweep): the ancestry line used to end in
    ``&& echo 'upstream contains this commit'`` — two POSIX-isms in one
    printed command. Windows PowerShell 5.1 rejects ``&&`` outright, and
    cmd.exe passes the single quotes through as part of the string, so the
    one line that tells a user whether reattaching is SAFE was un-runnable on
    the OS whose launcher offers the one-click version of it. ``git
    merge-base --is-ancestor`` already answers through its EXIT CODE, which
    every shell reports; the line now says how to read it instead of chaining
    an ``echo`` that only bash could run.
    """
    root = display_path(folder)
    branch = facts.branch
    lines = [
        f"# HEAD is detached at {_short(facts.head_sha)}. Look before acting:",
        f"#   git -C {root} status",
        f"#   git -C {root} log --oneline -1",
        "# Is this commit already upstream? The next command answers by EXIT",
        "# CODE: 0 = yes (nothing would be lost), 1 = no (commits here are not",
        "# on the remote — do NOT reattach until you have saved them).",
        f"#   git -C {root} merge-base --is-ancestor HEAD "
        f"{facts.remote}/{branch}",
    ]
    if facts.local_branch_exists:
        lines += [
            "# Reattach (the launcher v0.2.92+ has a one-click version with the",
            "# same guards on Preferences -> Launcher updates):",
            f"#   git -C {root} checkout {branch}",
            "# Nothing here deletes anything: git refuses rather than overwrite.",
        ]
    else:
        lines += [
            f"# There is no local `{branch}` branch to return to, so no checkout",
            "# command is printed here — inspect the output above and decide",
            f"#   git -C {root} branch -a",
        ]
    return "\n".join(lines)


def _currency_remediation(folder: Path, facts: SourceFacts) -> str:
    """The block printed when the checkout is provably not current."""
    root = display_path(folder)
    return "\n".join(
        [
            "# See what this checkout is missing (fetch is yours to run; the",
            "# doctor never mutates your repository):",
            f"#   git -C {root} fetch {facts.remote} {facts.branch}",
            f"#   git -C {root} log --oneline HEAD..{facts.remote}/{facts.branch}",
            "# Apply it the supported way — the launcher's Preferences ->",
            "# Updates page, or from a terminal (two lines: PowerShell 5.1,",
            "# the default on Windows, rejects `cd X && Y`):",
            f"#   cd {root}",
            "#   python install.py --update",
        ]
    )


def probe_source_currency(folder: Path, res: DoctorResolvers, ctx: dict) -> list[Finding]:
    """Is this orchestrator checkout attached, and is it current with upstream?

    Two findings, because they are two questions with two remediations:

    * ``head_attached`` — a detached HEAD is what silently disabled the
      launcher's self-update surface for five weeks. It is answered from
      LOCAL git only, so it is available even offline.
    * ``source_currency`` — the distance question. ``ok`` requires POSITIVE
      evidence from the remote; see :func:`collect_source_facts` for why a
      local ref may convict but never acquit.

    Returns NO findings when ``folder`` is not an orchestrator clone, and none
    for the currency question when it is not a git work tree — the
    not-applicable case is a fourth state, distinct from ``unknown``, and
    inventing an ``unknown`` for every user project would be noise rather than
    evidence. ``vco_lib.paths.looks_like_orchestrator_root`` is the ONE
    definition of "is this the orchestrator clone"; this probe does not add a
    second.
    """
    root = Path(folder)
    try:
        from vco_lib.paths import looks_like_orchestrator_root
    except Exception:  # noqa: BLE001 — a broken paths import is not a verdict
        return []
    if not looks_like_orchestrator_root(root):
        return []

    facts = res.resolve_source_facts(root, ask_remote=_ask_remote_for(ctx))
    if not facts.is_git_toplevel:
        detail = {"reason": "not a git work tree", "errors": dict(facts.errors)}
        if facts.errors.get("git") or facts.errors.get("toplevel"):
            return [
                Finding(
                    probe="source_currency",
                    status=STATUS_UNKNOWN,
                    summary=(
                        "source currency could not be determined: "
                        + (
                            facts.errors.get("git")
                            or facts.errors.get("toplevel")
                            or "?"
                        )
                    ),
                    detail=detail,
                )
            ]
        return [
            Finding(
                probe="source_currency",
                status=STATUS_UNKNOWN,
                summary=(
                    f"{root} is not a git checkout, so there is no upstream to "
                    "compare against — currency cannot be determined from here "
                    "(a release-tarball install has this shape)"
                ),
                detail=detail,
            )
        ]

    # The install age rides the CURRENCY verdict rather than being graded on
    # its own (see :func:`probe_last_update_run`): "59 commits behind, and the
    # last completed update here was 38 days ago" is the incident's sentence,
    # and neither half says it alone.
    return [
        _head_attached_finding(root, facts),
        _currency_finding(root, facts, age_days=install_age_days(root)),
    ]


def _ask_remote_for(ctx: dict) -> bool:
    """Should this pass make the ONE network call? ``full`` scope only.

    The boot subset's budget is in-process resolution and file reads; a
    network round trip per launcher start is outside it, and the launcher
    already performs its own at boot (``installer::check_for_updates``). The
    probe is registered ``full``-only today, so this reads ``full`` in
    production — it exists because :func:`collect_source_facts` is callable
    with either answer and the two must not be decided in two places.
    """
    return str(ctx.get("scope") or SCOPE_FULL) != SCOPE_BOOT


def _head_attached_finding(root: Path, facts: SourceFacts) -> Finding:
    if facts.detached is None:
        return Finding(
            probe="head_attached",
            status=STATUS_UNKNOWN,
            summary=(
                "could not determine whether HEAD is attached: "
                + (facts.errors.get("detached") or facts.errors.get("git") or "?")
            ),
            detail={"errors": dict(facts.errors)},
        )
    if facts.detached:
        return Finding(
            probe="head_attached",
            status=STATUS_PROBLEM,
            summary=(
                f"HEAD is DETACHED at {_short(facts.head_sha)} — no branch. The "
                "launcher's self-update check counts commits against a BRANCH, "
                "so in this state it reports 'up to date' no matter how far "
                "behind the checkout is, and 'Resync now' cannot run at all"
            ),
            fix=FIX_DEFER,
            command=_reattach_remediation(root, facts),
            detail={
                "head_sha": facts.head_sha,
                "fallback_branch": facts.branch,
                "local_branch_exists": facts.local_branch_exists,
            },
        )
    return Finding(
        probe="head_attached",
        status=STATUS_OK,
        summary=f"HEAD is on branch `{facts.branch}` at {_short(facts.head_sha)}",
        detail={"branch": facts.branch, "head_sha": facts.head_sha},
    )


def _currency_finding(
    root: Path, facts: SourceFacts, *, age_days: Optional[float] = None
) -> Finding:
    """Turn :class:`SourceFacts` into the currency verdict.

    The lattice, stated once so it cannot be re-derived differently: a
    POSITIVELY-KNOWN problem outranks an undetermined leg; an undetermined leg
    outranks ``ok``. ``ok`` therefore requires evidence, never the absence of
    evidence.

    ``age_days`` (days since the last completed ``install.py`` run) is appended
    to a PROBLEM summary only. On a healthy checkout it is not decision-
    relevant, and on a stale one it is the difference between "there is an
    update" and "you have not received one in five weeks".
    """
    ref = f"{facts.remote}/{facts.branch}"
    age_tail = (
        f"; the last completed install.py run here was {age_days:.0f} day(s) ago"
        if age_days is not None
        else ""
    )
    detail = {
        "last_install_age_days": age_days,
        "remote": facts.remote,
        "branch": facts.branch,
        "head_sha": facts.head_sha,
        "remote_sha": facts.remote_sha,
        "remote_asked": facts.remote_asked,
        "behind": facts.behind,
        "behind_local_ref": facts.behind_local_ref,
        "contains_remote_tip": facts.contains_remote_tip,
        "errors": dict(facts.errors),
    }

    if facts.contains_remote_tip is False:
        if facts.behind:
            head = f"this checkout is {facts.behind} commit(s) behind {ref}"
        elif facts.remote_sha:
            head = (
                f"this checkout does not contain {ref}'s current tip "
                f"{_short(facts.remote_sha)} (the object is not in this repo, so "
                "the exact distance needs a fetch)"
            )
        else:
            head = f"this checkout is behind {ref}"
        return Finding(
            probe="source_currency",
            status=STATUS_PROBLEM,
            summary=head + age_tail,
            fix=FIX_DEFER,
            command=_currency_remediation(root, facts),
            detail=detail,
        )

    # A local remote-tracking ref may CONVICT even when the remote could not
    # be reached — being behind a ref we already have is not in doubt.
    if facts.behind_local_ref:
        return Finding(
            probe="source_currency",
            status=STATUS_PROBLEM,
            summary=(
                f"this checkout is at least {facts.behind_local_ref} commit(s) "
                f"behind the last-fetched {ref}"
                + (
                    f" (the remote itself could not be reached: "
                    f"{facts.errors.get('remote_tip')})"
                    if facts.errors.get("remote_tip")
                    else ""
                )
                + age_tail
            ),
            fix=FIX_DEFER,
            command=_currency_remediation(root, facts),
            detail=detail,
        )

    if facts.contains_remote_tip is True:
        return Finding(
            probe="source_currency",
            status=STATUS_OK,
            summary=(
                f"current with {ref} at {_short(facts.remote_sha)}"
                if facts.behind == 0 and facts.head_sha == facts.remote_sha
                else (
                    f"current with {ref}: this checkout contains its tip "
                    f"{_short(facts.remote_sha)} (local commits on top)"
                )
            ),
            detail=detail,
        )

    if facts.remote_configured is False:
        reason = (
            f"no `{facts.remote}` remote is configured here, so there is nothing "
            "to compare against — the launcher adds it on the first update it runs"
        )
    elif not facts.remote_asked:
        reason = (
            f"the remote was not queried in this pass, and the local {ref} ref "
            "carries no record of when it was last updated (VCO's own fetch "
            "passes --no-write-fetch-head), so 'level with it' is not evidence "
            "of being current"
        )
    else:
        reason = (
            facts.errors.get("remote_tip")
            or facts.errors.get("ancestry")
            or facts.errors.get("git")
            or "the comparison could not be completed"
        )
    return Finding(
        probe="source_currency",
        status=STATUS_UNKNOWN,
        summary=f"currency against {ref} could not be determined: {reason}",
        detail=detail,
    )


# ---------------------------------------------------------------------------
# When did an update last succeed?
# ---------------------------------------------------------------------------

#: The event install.py appends when a run reaches the end
#: (``install.py:6762`` — ``_log_install_event("session", "ok", …)``).
INSTALL_LOG_REL = ("state", "logs", "install.jsonl")
INSTALL_SESSION_STEP = "session"
INSTALL_SESSION_OK = "ok"


def last_successful_install(folder: Path) -> Optional[dict]:
    """The newest completed ``install.py`` session row, or ``None``.

    Reads the same durable log the launcher's ``read_install_log`` command and
    install.py's own resume logic read. Scans the whole file and keeps the
    LAST matching row rather than tailing: the file is append-only and small
    (tens of rows per run), and a tail would have to guess a byte window.
    """
    path = Path(folder).joinpath(*INSTALL_LOG_REL)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    newest: Optional[dict] = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if (
            row.get("step") == INSTALL_SESSION_STEP
            and row.get("phase") == INSTALL_SESSION_OK
        ):
            newest = row
    return newest


def _parse_install_ts(value: Any) -> Optional[datetime]:
    """``2026-09-02T10:11:12Z`` → aware datetime, or ``None``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def install_age_days(folder: Path, *, now: Optional[datetime] = None) -> Optional[float]:
    """Days since the last completed ``install.py`` run, or ``None``."""
    row = last_successful_install(folder)
    if row is None:
        return None
    when = _parse_install_ts(row.get("ts"))
    if when is None:
        return None
    delta = (now or datetime.now(timezone.utc)) - when
    return delta.total_seconds() / 86400.0


def probe_last_update_run(folder: Path, res: DoctorResolvers, ctx: dict) -> list[Finding]:
    """When did an install/update last COMPLETE on this install root?

    Reported, never judged. An install root that has run nothing for months is
    either deliberately pinned or silently not updating, and this reading
    cannot tell the two apart — :func:`probe_source_currency` answers the
    question the age is a proxy for, and answers it with evidence. Grading age
    alone would make the doctor cry wolf on every user who is simply happy
    with the version they have, and a report that cries wolf is one nobody
    reads (the same rule ``probe_npm_pins`` applies to absent pins).

    So the tri-state here is ``ok`` (measured) vs ``unknown`` (nothing to
    measure); the age itself is carried into the currency finding's summary,
    where it changes what the reader should do.
    """
    root = Path(folder)
    try:
        from vco_lib.paths import looks_like_orchestrator_root
    except Exception:  # noqa: BLE001
        return []
    if not looks_like_orchestrator_root(root):
        return []

    row = last_successful_install(root)
    path = root.joinpath(*INSTALL_LOG_REL)
    if row is None:
        return [
            Finding(
                probe="last_update_run",
                status=STATUS_UNKNOWN,
                summary=(
                    f"no completed install.py session recorded in {path} "
                    + ("(the log does not exist)" if not path.exists() else "(the log has no `session ok` row)")
                ),
                detail={"install_log": str(path), "exists": path.exists()},
            )
        ]
    age = install_age_days(root)
    mode = ""
    data = row.get("data")
    if isinstance(data, dict):
        mode = str(data.get("mode") or "")
    if not mode:
        mode = str(row.get("detail") or "").split(" ")[0]
    return [
        Finding(
            probe="last_update_run",
            status=STATUS_OK,
            summary=(
                f"last completed install.py run: {row.get('ts')}"
                + (f" ({age:.0f} day(s) ago)" if age is not None else "")
                + (f", mode={mode}" if mode else "")
            ),
            detail={
                "ts": row.get("ts"),
                "age_days": age,
                "mode": mode,
                "install_log": str(path),
            },
        )
    ]


# ---------------------------------------------------------------------------
# Install completeness — does the completion marker rest on an installer run?
# ---------------------------------------------------------------------------

#: ``state/install-manifest.json``, relative to the install root.
INSTALL_MANIFEST_REL = ("state", "install-manifest.json")

#: ``install_method`` values only the RUST writer
#: (``launcher/src-tauri/src/commands/manifest.rs::refresh_install_manifest``)
#: produces. install.py's own writer spells ``install.py`` / ``update`` /
#: ``lightweight``, so seeing one of these means the LAST hand on the marker
#: was a path that never ran install.py. Carried in the summary as the
#: EXPLANATION; never the conviction on its own — a resync that pulled nothing
#: writes one of these and owes no work.
RUST_INSTALL_METHODS: tuple[str, ...] = ("launcher_update", "orchestrator_update")

#: A git object name, for the commit leg. ``vco_commit`` is a *short* SHA and
#: ``source_commit`` a full one, so the comparison is a prefix test — but only
#: after both sides are proven to BE object names. ``recorded_manifest_version``
#: passes the dedicated ``vco_commit`` field through unvalidated (a hand-edited
#: ``"unknown"`` survives it), and comparing that against a real SHA would
#: convict on a string nobody promised was a commit.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _read_json_dict(path: Path) -> Optional[dict]:
    """``path`` parsed as a JSON object, or ``None``. Never raises."""
    try:
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _commits_disagree(attested: Any, built: Any) -> Optional[bool]:
    """Do two recorded commits name different objects? ``None`` = unknowable.

    ``None`` whenever either side is absent or is not an object name — the
    ``.git/HEAD`` reader install.py uses returns ``None`` for a PACKED ref, so
    a perfectly healthy clone can record no ``source_commit`` at all, and that
    must never read as disagreement.
    """
    a = attested.strip().lower() if isinstance(attested, str) else ""
    b = built.strip().lower() if isinstance(built, str) else ""
    if not _SHA_RE.match(a) or not _SHA_RE.match(b):
        return None
    return not (a.startswith(b) or b.startswith(a))


def probe_install_completeness(
    folder: Path, res: DoctorResolvers, ctx: dict
) -> list[Finding]:
    """Does ``install-manifest.json``'s claim rest on an installer run?

    The state this exists for (the v0.2.95 surface map's H1/H2/H3): the
    launcher's ``apply_launcher_update`` / ``force_resync_launcher`` advance
    the whole source tree and rebuild only the launcher, and
    ``update_orchestrator_at`` file-copies a newer tree into another clone.
    None of the three runs ``install.py``, so venv, pip, hooks, templates, MCP
    registrations, the KG seed, the schema and the boot service all stay at the
    OLD version — and then each one calls ``refresh_install_manifest``, which
    re-asserts ``installed: true`` over them. The marker attests work that
    nothing did, and **nothing looked**: ``source_currency`` is 0-behind
    afterwards (so :func:`_currency_finding` drops the install age it only
    appends to a PROBLEM), and ``probe_last_update_run`` reports the stale date
    as ``ok`` by design.

    What that writer does TODAY, because it decides which leg below can fire.
    v0.2.95 WP-1 changed ``commands::manifest::refresh_install_manifest``: it no
    longer re-reads ``version`` nor stamps ``completed_at`` (install.py is now
    the only writer of both), it still advances ``source_commit`` from
    ``.git/HEAD``, and it stamps ``post_source_only: true`` — the writer NAMING
    the state instead of leaving it to be inferred. So the population this probe
    serves splits three ways, and the three legs are not redundant:

    * a marker last written by a PRE-0.2.95 launcher carries the version that
      launcher pulled, and the bundle manifest does not → the VERSION leg. That
      population is real: it is everyone who arrives by upgrading INTO 0.2.95;
    * a marker last written by a 0.2.95+ launcher agrees on version by
      construction, so only the COMMIT leg can speak — and it is silent
      whenever ``.git/HEAD`` did not move (``update_orchestrator_at``
      file-copies a tree into a clone and leaves its HEAD alone) or could not
      be read (a packed ref, a worktree HEAD file: ``read_git_rev`` returns
      ``None`` and the old ``source_commit`` survives untouched);
    * the FLAG leg is the only one that survives both of those, and it is the
      writer's own word. A probe that ignored it would answer ``ok`` — "rests
      on a real installer run" — for a manifest that says in as many words that
      the last hand on it installed nothing.

    Why this probe may judge where its neighbour deliberately does not.
    ``probe_last_update_run`` refuses to grade the install AGE because age is a
    proxy with two causes — deliberately pinned, or silently not updating — and
    one reading cannot separate them. This probe never reads age as evidence.
    It compares two INDEPENDENT records of one fact, and the fact is not "is
    this install old" but "which source was this install built from":

    * ``state/install-manifest.json`` — written by install.py AND by the three
      launcher paths above;
    * ``.claude/.vco-manifest.json`` — written ONLY by the bundle engine
      (``project_init.install_project_bundle``, which install.py reaches
      through ``self_install``). No launcher update path writes it, and it is
      gitignored, so ``update_orchestrator_at``'s gitignore-aware copy does not
      carry the source clone's copy into the target either.

    Two records, one writer in common. When they name different sources, the
    completion marker was advanced by something that did not run the
    installer — positive evidence, not a proxy, and the ambiguity that stops
    its neighbour from judging does not arise. A user who is happily pinned
    has one install.py run behind both records and reads ``ok`` here forever.
    The flag leg is the same evidence arriving by declaration rather than by
    comparison, and it is not a proxy either: only a path that advanced the
    source tree without installing writes it, and only an installer run clears
    it (install.py's ``_write_install_manifest`` rebuilds the manifest from a
    literal dict, so the key simply does not survive a real run).

    Conservative in every direction that matters. No manifest, an unparseable
    one, a pre-v0.2.92 bundle manifest whose ``vco_version`` holds a git SHA
    (``recorded_manifest_version`` hands that back as a COMMIT, never as a
    version), a packed ref that left ``source_commit`` empty, an ABSENT
    ``post_source_only`` (which is every manifest written before v0.2.95, and
    every manifest install.py writes at all) — each yields ``unknown`` or
    ``ok``, never ``problem``. A false problem here would tell a healthy
    install it is broken and send its owner into an update they do not need,
    which is worse than the silence this closes.

    Read-only: three file reads, no subprocess in the decision path. The one
    subprocess (``vco_version.resolve`` → ``git rev-parse --short HEAD``) runs
    only AFTER a problem is established, to name the checkout in the report.
    """
    root = Path(folder)
    try:
        from vco_lib.paths import looks_like_orchestrator_root
    except Exception:  # noqa: BLE001
        return []
    if not looks_like_orchestrator_root(root):
        # Not the orchestrator clone. Same fourth state `probe_source_currency`
        # names: not-applicable, which is neither `unknown` nor a finding.
        return []

    manifest_path = root.joinpath(*INSTALL_MANIFEST_REL)
    manifest = _read_json_dict(manifest_path)
    if manifest is None or manifest.get("installed") is not True:
        return [
            Finding(
                probe="install_completeness",
                status=STATUS_UNKNOWN,
                summary=(
                    f"no completed install is recorded at {manifest_path} — "
                    "nothing to corroborate (run `python install.py --update` "
                    "from this root to write one)"
                ),
                detail={
                    "install_manifest": str(manifest_path),
                    "exists": manifest_path.exists(),
                },
            )
        ]

    try:
        from vco_lib.manifest_paths import manifest_path as bundle_manifest_path
        from vco_lib.vco_version import recorded_manifest_version
    except Exception as exc:  # noqa: BLE001 — a broken import is unknown, never a verdict
        return [
            Finding(
                probe="install_completeness",
                status=STATUS_UNKNOWN,
                summary=f"could not load the bundle-manifest reader: {exc}",
                detail={"install_manifest": str(manifest_path)},
            )
        ]

    bundle_path = bundle_manifest_path(root)
    bundle = _read_json_dict(bundle_path)
    built_version, built_commit = recorded_manifest_version(bundle or {})
    if bundle is None or (built_version is None and built_commit is None):
        return [
            Finding(
                probe="install_completeness",
                status=STATUS_UNKNOWN,
                summary=(
                    f"{bundle_path} records no source this install was built "
                    "from, so the install manifest's claim cannot be "
                    "corroborated"
                ),
                detail={
                    "install_manifest": str(manifest_path),
                    "bundle_manifest": str(bundle_path),
                    "bundle_manifest_exists": bundle_path.exists(),
                },
            )
        ]

    attested_version = manifest.get("version")
    attested_version = (
        attested_version.strip() if isinstance(attested_version, str) else ""
    )
    attested_commit = manifest.get("source_commit")
    install_method = manifest.get("install_method")
    install_method = install_method if isinstance(install_method, str) else ""
    completed_at = _parse_install_ts(manifest.get("completed_at"))

    session_row = last_successful_install(root)
    session_ts_raw = session_row.get("ts") if isinstance(session_row, dict) else None
    session_ts = _parse_install_ts(session_ts_raw)

    # The WRITER's own name for this state (v0.2.95 WP-1).
    # ``manifest::refresh_install_manifest`` stamps ``post_source_only: true``
    # on every path that advances the source tree without running install.py,
    # and install.py's ``_write_install_manifest`` rebuilds the manifest from a
    # literal dict, so a real installer run drops it. That makes it evidence a
    # comparison cannot produce — see the docstring for the two shapes where
    # both other legs are structurally blind.
    #
    # ``is True`` EXACTLY, and what the other answers mean:
    #
    # * ABSENT — no evidence, never innocence. Every manifest written before
    #   v0.2.95 lacks the key; so does every manifest install.py writes, at any
    #   version; so does one written by a pre-0.2.95 launcher binary that took
    #   a source-only path. Convicting on absence would convict every healthy
    #   install on the planet, so absence leaves the verdict entirely to the
    #   version and commit legs (which is exactly the population they cover).
    # * ``false`` / a string / a number — no writer produces these; a
    #   hand-edited or foreign value is not the writer speaking, so it is read
    #   as absent rather than guessed at.
    #
    # The flag's truthfulness is the WRITER's contract, and convicting on it
    # makes that contract load-bearing in one direction: a refresh call from a
    # path that did NOT move the source tree would fire this probe (and
    # `install_stale`) on a healthy install. Nothing readable from the manifest
    # can separate "nothing moved" from "a tree was copied in without moving
    # .git/HEAD" — both leave two agreeing records — so the invariant lives with
    # the callers of `refresh_install_manifest`, which is where v0.2.95 put it:
    # `ArtefactSource::Unchanged` exists precisely so the already-up-to-date
    # branch records nothing instead of claiming a source advance. If a future
    # caller stamps the flag without advancing the tree, THAT is the defect —
    # do not soften the leg here to absorb it.
    post_source_only = manifest.get("post_source_only") is True

    detail = {
        "install_manifest": str(manifest_path),
        "bundle_manifest": str(bundle_path),
        "attested_version": attested_version or None,
        "attested_commit": attested_commit if isinstance(attested_commit, str) else None,
        "built_version": built_version,
        "built_commit": built_commit,
        "install_method": install_method or None,
        "marker_written_by_launcher_path": install_method in RUST_INSTALL_METHODS,
        "post_source_only": post_source_only,
        "completed_at": manifest.get("completed_at"),
        "last_install_session_ok": session_ts_raw,
    }

    # ACQUITTAL, and the one place the install log is load-bearing. install.py
    # logs `session ok` and only THEN writes the manifest, so on every run it
    # performs the session stamp is at or before `completed_at`. A stamp that
    # is strictly LATER means an installer run finished after the marker was
    # written — its (soft-fail) manifest write did not land. The marker is then
    # BEHIND the work, not ahead of it: stale bookkeeping, nothing owed.
    #
    # It acquits `post_source_only` too, which is why the flag leg is placed
    # AFTER this and not before: the only way a surviving flag coexists with a
    # LATER installer session is the same soft-failed manifest write (a run that
    # completed cannot have written the flag — install.py never writes it), so
    # the flag is stale bookkeeping in exactly the case this leg already covers.
    # Measured on the live root, `session ok` precedes `completed_at` by ~4s on
    # a healthy run, which is why the gap is an acquittal and never a trigger.
    if session_ts is not None and completed_at is not None and session_ts > completed_at:
        return [
            Finding(
                probe="install_completeness",
                status=STATUS_OK,
                summary=(
                    f"an install.py session completed at {session_ts_raw}, after "
                    f"the install manifest was last written ({manifest.get('completed_at')}) "
                    "— the marker lags the installer rather than running ahead of it"
                ),
                detail=detail,
            )
        ]

    versions_disagree = (
        attested_version != ""
        and built_version is not None
        and attested_version.lstrip("v") != built_version
    )
    commits_disagree = _commits_disagree(attested_commit, built_commit)
    detail["versions_disagree"] = versions_disagree
    detail["commits_disagree"] = commits_disagree

    if not versions_disagree and commits_disagree is not True and not post_source_only:
        return [
            Finding(
                probe="install_completeness",
                status=STATUS_OK,
                summary=(
                    "the install manifest and the bundle manifest name the same "
                    f"source ({attested_version or built_version or '?'}"
                    + (f" / {built_commit}" if built_commit else "")
                    + ") — this install's completion marker rests on a real "
                    "installer run"
                ),
                detail=detail,
            )
        ]

    # Conviction precedence is REPORTING precedence, not confidence: whichever
    # leg can name the two sources most usefully goes in the summary. The flag
    # names no source, so it is last — and it is the one that fires alone
    # exactly where the other two are structurally blind.
    if versions_disagree:
        detail["convicted_on"] = "version"
    elif commits_disagree is True:
        detail["convicted_on"] = "commit"
    else:
        detail["convicted_on"] = "post_source_only"
    # Name the checkout too, so the report says what the user would be moving
    # FROM and TO. Only on the problem path: this is the probe's one subprocess.
    try:
        from vco_lib import vco_version as _vv

        here = _vv.resolve(root)
        detail["checkout_version"] = here.semver
        detail["checkout_commit"] = here.commit
    except Exception:  # noqa: BLE001 — enrichment never decides and never raises
        detail["checkout_version"] = None
        detail["checkout_commit"] = None

    if versions_disagree:
        head = (
            f"install-manifest.json attests version {attested_version} as "
            f"installed, but this install's `.claude/` bundle was last written "
            f"at {built_version}"
        )
    elif commits_disagree is True:
        head = (
            f"install-manifest.json attests commit {_short(attested_commit)}, "
            f"but this install's `.claude/` bundle was last written at "
            f"{_short(built_commit)}"
        )
    else:
        head = (
            "install-manifest.json carries `post_source_only`, the flag its "
            "writer sets when a path advances the source tree without running "
            "install.py — so the `installed: true` beside it covers the tree as "
            f"it stood at the last installer run "
            f"({attested_version or built_version or '?'}), not the tree on "
            "disk now"
        )
    tail = ""
    if install_method in RUST_INSTALL_METHODS:
        tail = (
            f" — the marker was last written by the launcher's `{install_method}` "
            "path, which advances the source tree and rebuilds the launcher "
            "without running install.py"
        )
    if session_ts_raw:
        tail += f"; the last completed install.py run here was {session_ts_raw}"
    else:
        tail += "; no completed install.py session is recorded on this root at all"
    return [
        Finding(
            probe="install_completeness",
            status=STATUS_PROBLEM,
            summary=head + tail,
            fix=FIX_DEFER,
            condition_id=CID_INSTALL_MARKER_UNBACKED,
            command=remedy_shell.steps(
                f"cd {remedy_shell.quote(root.resolve())}",
                "python install.py --update",
            ),
            detail=detail,
        )
    ]


# ---------------------------------------------------------------------------
# Diagnostics list — the ask, generated from reality
# ---------------------------------------------------------------------------


def diagnostic_file_candidates(folder: Path) -> list[dict]:
    """The support-request artefact list, resolved for THIS machine.

    One row per artefact named by ``docs/post-install/UPDATE-RECOVERY.md``'s
    "Always-present diagnostics" table, each with the honest ``present when``
    note that table carries. Rows are resolved as GLOBS where the writer
    rotates by date (``launcher.<date>.log``), because naming a fixed
    ``launcher.log`` would be a third instance of the defect this closes: a
    document asking for a filename no writer produces.
    """
    rows: list[dict] = []
    logs: Optional[Path] = None
    try:
        from vco_lib.paths import vct_root_dir

        logs = Path(vct_root_dir()) / "logs"
        vct_root: Optional[Path] = Path(vct_root_dir())
    except Exception:  # noqa: BLE001 — an unresolvable state dir is "unknown"
        vct_root = None
    root = Path(folder)
    if logs is not None:
        rows.append(
            {
                "id": "launcher_log",
                "glob": str(logs / "launcher.*.log"),
                "dir": logs,
                "pattern": "launcher.*.log",
                "when": "every launcher start, v0.2.92+",
            }
        )
        rows.append(
            {
                "id": "hub_log",
                "glob": str(logs / "hub.*.log"),
                "dir": logs,
                "pattern": "hub.*.log",
                "when": "whenever vct-hub has run, v0.2.92+",
            }
        )
    rows.append(
        {
            "id": "install_log",
            "path": root.joinpath(*INSTALL_LOG_REL),
            "when": "after any install.py run",
        }
    )
    rows.append(
        {
            "id": "deferral_ledger",
            "path": root / ".claude" / "context" / "UPDATE_DEFERRED.md",
            "when": "whenever something was deferred",
        }
    )
    if vct_root is not None:
        rows.append(
            {
                "id": "update_log",
                "path": vct_root / "update.log",
                "when": "ONLY after a binary swap ever ran; absence is normal",
            }
        )
        rows.append(
            {
                "id": "launcher_update_state",
                "path": vct_root / "launcher-update-state.json",
                "when": "after the launcher's first update check",
            }
        )
    return rows


def _stat_row(row: dict) -> dict:
    """Resolve one candidate to ``{present, files[], error}``. Never raises."""
    out = dict(row)
    out.pop("dir", None)
    files: list[dict] = []
    try:
        if "path" in row:
            paths = [Path(row["path"])]
            out["path"] = str(row["path"])
        else:
            directory = Path(row["dir"])
            paths = sorted(directory.glob(row["pattern"])) if directory.is_dir() else []
        for path in paths:
            try:
                st = path.stat()
            except OSError:
                continue
            files.append(
                {
                    "path": str(path),
                    "size_bytes": st.st_size,
                    "modified": datetime.fromtimestamp(
                        st.st_mtime, timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            )
    except OSError as exc:
        out["error"] = str(exc)
    out["files"] = files
    out["present"] = bool(files)
    return out


def probe_diagnostic_files(folder: Path, res: DoctorResolvers, ctx: dict) -> list[Finding]:
    """List the diagnostic files that DO exist on this machine.

    A manifest, not a check — and deliberately so. Which diagnostics exist is
    never itself a defect (a fresh install has none, a CLI-only user has no
    launcher log), so this probe has two reachable states: ``ok`` when the
    machine could be inspected, and ``unknown`` when it could not. Inventing a
    problem tier here would be the cry-wolf failure; the value is that the ask
    is now generated from the filesystem instead of from a document.

    The document in question is ``docs/post-install/UPDATE-RECOVERY.md``,
    which asked users to copy ``~/.vct/update.log`` as step 0 of a recipe
    labelled "any version; needs no working launcher" — while that file is
    written only AFTER a binary-swap handoff, so anyone who never completed an
    update had nothing to copy and reasonably concluded their diagnostics were
    broken. Every absent row here says when it would have existed.
    """
    rows = [_stat_row(row) for row in diagnostic_file_candidates(Path(folder))]
    if not rows or all(row.get("error") for row in rows):
        return [
            Finding(
                probe="diagnostic_files",
                status=STATUS_UNKNOWN,
                summary="the diagnostic locations could not be inspected",
                detail={"candidates": rows},
            )
        ]
    present = [r for r in rows if r.get("present")]
    absent = [r for r in rows if not r.get("present")]
    listed = ", ".join(
        f"{f['path']} ({f['size_bytes']} B, {f['modified'][:10]})"
        for r in present
        for f in r["files"]
    )
    n_files = sum(len(r["files"]) for r in present)
    return [
        Finding(
            probe="diagnostic_files",
            status=STATUS_OK,
            summary=(
                (
                    f"{n_files} diagnostic file(s) to include in a support "
                    f"request: {listed}"
                )
                if present
                else "no diagnostic file exists on this machine yet"
            )
            + (
                " | not written yet (this is normal): "
                + "; ".join(
                    f"{r.get('path') or r.get('glob')} — {r['when']}" for r in absent
                )
                if absent
                else ""
            ),
            detail={"present": present, "absent": absent},
        )
    ]


# ---------------------------------------------------------------------------
# WP-7 — a `vct` deployed by copy months ago, still first on PATH
# ---------------------------------------------------------------------------

#: Where the checkout keeps the canonical secrets CLI.
VCT_CLI_REL = ("tools", "vct-secrets", "vct")

#: The capability-stamp line both copies carry (``tools/vct-secrets/vct:45``).
VCT_GUARDS_PREFIX = "VCT_GUARDS="

#: A string every version of OUR secrets CLI contains. Used to refuse to
#: grade an unrelated program that happens to be called ``vct``.
VCT_CLI_MARKER = "VCT_SECRETS_DIR"


def parse_vct_guards(text: str) -> Optional[list[str]]:
    """Guard tokens from a ``vct`` script's capability stamp, or ``None``.

    ``None`` means the stamp is ABSENT — which is the loudest signal available,
    because a copy predating the stamp has none of the guards it names.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(VCT_GUARDS_PREFIX):
            continue
        value = stripped[len(VCT_GUARDS_PREFIX) :].strip().strip('"').strip("'")
        return [tok for tok in value.split() if tok]
    return None


def _stale_vct_remediation(checkout_cli: Path, deployed: str) -> str:
    """Advice for a stale deployed ``vct``. Detection only — never a repair.

    ``~/.vct-secrets/`` is a directory install.py states in three places it
    must never touch, and the deployed file may be the only copy a user has
    customised. So the doctor names the drift and hands over the two commands
    the CLI's own ``doctor`` prints, unchanged, so the two surfaces cannot
    give different advice.

    v0.2.92 (R42 sweep): this block was POSIX-only (``ln -sfn`` / ``cp -a`` /
    ``~/.local/bin``) on the assumption that the probe cannot fire on
    Windows. It can. ``vct`` is a bash script with no extension, and
    :func:`shutil.which` on Windows checks the BARE name in addition to the
    PATHEXT variants (it is tried last, but it is tried) — so a user who
    followed the old "copy it onto PATH" documentation and runs it under Git
    Bash resolves here, and got handed three commands their shell does not
    have. The Windows branch refreshes by COPY, because a symlink there needs
    Developer Mode or an elevated shell and this advice must not require
    either.
    """
    canonical = display_path(checkout_cli)
    tree = display_path(checkout_cli.parent)
    if remedy_shell.is_windows():
        deployed_dir = display_path(Path(deployed).parent)
        return "\n".join(
            [
                f"# PATH `vct` runs {deployed}",
                f"# which is not {canonical}.",
                "# VCO never edits a deployed copy, so this is yours to",
                "# refresh — copy the checkout TREE over it (lib/ included):",
                "#   Copy-Item -Recurse -Force "
                + remedy_shell.quote(tree.rstrip("\\/") + "\\*") + " "
                + remedy_shell.quote(deployed_dir),
                "# `vct` is a bash script: run it from Git Bash or WSL, not",
                "# from cmd/PowerShell.",
                "# Verify afterwards — it must list every guard:",
                "#   vct version",
            ]
        )
    return "\n".join(
        [
            f"# PATH `vct` runs {deployed}",
            f"# which is not {canonical}.",
            "# VCO never edits anything under ~/.vct-secrets/, so this is yours",
            "# to refresh. EITHER of these fixes it:",
            f"#   ln -sfn {canonical} ~/.local/bin/vct   # preferred: cannot go stale",
            f"#   cp -a {tree}/. ~/.vct-secrets/   # copy the TREE, incl. lib/",
            "# Verify afterwards — it must list every guard:",
            "#   vct version",
        ]
    )


def probe_stale_vct_deploy(folder: Path, res: DoctorResolvers, ctx: dict) -> list[Finding]:
    """Is the ``vct`` on PATH the checkout's, or a copy that has rotted?

    ``vct`` is deployed by COPY in older documentation, and nothing in
    install.py refreshes that copy — so a user can be running a months-old CLI
    (missing every write-time guard added since) while reading current docs.
    The stale copy cannot detect itself: the CLI's own ``_doctor_check_deployed_copy``
    only runs if you already knew to invoke the checkout's copy, which is
    precisely what someone in this state does not do.

    Measured, never inferred: the verdict comes from the file PATH actually
    resolves to, read through :func:`shutil.which` and ``realpath``, not from
    a version string, a timestamp, or the existence of ``~/.vct-secrets/vct``.
    A version number says what its author claimed; the ``VCT_GUARDS`` stamp
    says which guards are in the bytes that will run.

    Four outcomes, and the not-applicable one is the common case:

    * NO findings — this folder is not an orchestrator checkout, or nothing
      named ``vct`` is on PATH. **Absence is not staleness**, and a health
      report that grades a tool the user never deployed is noise. (On a
      native-Windows interpreter this is also the answer for a Git-Bash/WSL
      deployment, which ``shutil.which`` cannot see; named in the report.)
    * ``ok`` — PATH resolves to the checkout copy, or to a copy carrying every
      guard the checkout declares.
    * ``problem`` — guards are missing, named one by one.
    * ``unknown`` — something is on PATH but could not be read or identified.
    """
    checkout_cli = Path(folder).joinpath(*VCT_CLI_REL)
    if not checkout_cli.is_file():
        return []
    resolved = res.resolve_path_command("vct")
    if not resolved:
        return []

    try:
        checkout_text = checkout_cli.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [
            Finding(
                probe="stale_vct_deploy",
                status=STATUS_UNKNOWN,
                summary=f"could not read the reference CLI at {checkout_cli}: {exc}",
            )
        ]
    reference = parse_vct_guards(checkout_text)
    if not reference:
        return [
            Finding(
                probe="stale_vct_deploy",
                status=STATUS_UNKNOWN,
                summary=(
                    f"{checkout_cli} carries no {VCT_GUARDS_PREFIX} stamp, so there "
                    "is nothing to compare a deployed copy against"
                ),
            )
        ]

    if same_location(resolved, str(checkout_cli)):
        return [
            Finding(
                probe="stale_vct_deploy",
                status=STATUS_OK,
                summary=f"`vct` on PATH resolves to this checkout ({resolved})",
                detail={"resolved": resolved, "guards": reference},
            )
        ]

    try:
        deployed_text = Path(resolved).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [
            Finding(
                probe="stale_vct_deploy",
                status=STATUS_UNKNOWN,
                summary=f"`vct` on PATH is at {resolved} but could not be read: {exc}",
                detail={"resolved": resolved},
            )
        ]
    if VCT_CLI_MARKER not in deployed_text:
        return [
            Finding(
                probe="stale_vct_deploy",
                status=STATUS_UNKNOWN,
                summary=(
                    f"the `vct` on PATH ({resolved}) does not look like VCO's "
                    "secrets CLI, so it is not graded as a stale deployment"
                ),
                detail={"resolved": resolved},
            )
        ]

    deployed_guards = parse_vct_guards(deployed_text)
    if deployed_guards is None:
        return [
            Finding(
                probe="stale_vct_deploy",
                status=STATUS_PROBLEM,
                summary=(
                    f"the `vct` you run ({resolved}) predates the guard "
                    f"capability stamp entirely — it is missing ALL of: "
                    f"{' '.join(reference)}"
                ),
                fix=FIX_DEFER,
                command=_stale_vct_remediation(checkout_cli, resolved),
                detail={
                    "resolved": resolved,
                    "missing": reference,
                    "reference": reference,
                },
            )
        ]
    missing = [g for g in reference if g not in deployed_guards]
    if missing:
        return [
            Finding(
                probe="stale_vct_deploy",
                status=STATUS_PROBLEM,
                summary=(
                    f"the `vct` you run ({resolved}) is missing guard(s): "
                    f"{' '.join(missing)}"
                ),
                fix=FIX_DEFER,
                command=_stale_vct_remediation(checkout_cli, resolved),
                detail={
                    "resolved": resolved,
                    "missing": missing,
                    "deployed": deployed_guards,
                    "reference": reference,
                },
            )
        ]
    return [
        Finding(
            probe="stale_vct_deploy",
            status=STATUS_OK,
            summary=(
                f"`vct` on PATH ({resolved}) is a separate copy but carries every "
                f"guard this checkout declares"
            ),
            detail={"resolved": resolved, "deployed": deployed_guards},
        )
    ]


#: v0.2.92 WP-D — bundle-staleness census condition. Emitted and resolved by
#: ``vco_lib.bundle_staleness`` (the census owns the lifecycle end-to-end:
#: a full census emits when ``stale > 0`` and resolves when ``stale == 0``);
#: this probe REPORTS the same verdicts as findings. Deliberately NOT in
#: :data:`DOCTOR_OWNED_CIDS` — the doctor re-emitting it would fork the
#: lifecycle the census already owns (same discipline as
#: ``launcher_binary_stale``: report it, let the owner emit it).
CID_BUNDLES_STALE = "project_bundles_stale"


def probe_bundle_staleness(folder: Path, res: DoctorResolvers, ctx: dict) -> list[Finding]:
    """Are any registered projects' bundles older than the running orchestrator?

    The D15 incident in one sentence: 12 of a real user's 13 projects carried
    bundles months old while the orchestrator updated weekly, because
    per-project bundle updates are manual and nothing reported the gap. This
    probe is the report — a thin call into :mod:`vco_lib.bundle_staleness`,
    which is the ONE census (engine dry-run per project; verdicts are
    state-keyed on file hashes, never version deltas — R26).

    Returns NO findings when ``folder`` is not the orchestrator root (a
    per-project ``vco doctor`` run does not census the machine; the
    per-project verdict surface is the census CLI + the launcher GUI).
    Registry unavailable → ONE ``unknown`` finding saying so — a fresh root
    install before the first launcher boot is the normal cause, and "could
    not determine" must never render as "0 stale". Otherwise: one summary
    finding (``problem`` iff ``stale > 0``, carrying the condition id and
    the remedy) plus one finding per stale / unknown project, named, with
    the exact per-project command.

    Full-scope only (see the PROBES note below): the census runs one engine
    dry-run per registered project — seconds, not milliseconds.
    """
    root = Path(folder)
    try:
        from vco_lib.paths import looks_like_orchestrator_root
    except Exception:  # noqa: BLE001 — a broken paths import is not a verdict
        return []
    if not looks_like_orchestrator_root(root):
        return []

    from vco_lib import bundle_staleness

    try:
        payload = bundle_staleness.run_census(root, refresh_ledger=True)
    except Exception as exc:  # noqa: BLE001 — census must never fail the pass
        return [
            Finding(
                probe="bundle_staleness",
                status=STATUS_UNKNOWN,
                summary=f"bundle census could not run: {exc}",
            )
        ]

    summary = payload.get("summary") or {}
    stale = int(summary.get("stale") or 0)
    unknown = int(summary.get("unknown") or 0)
    current = int(summary.get("current") or 0)
    rows = payload.get("projects") or []
    running = payload.get("running") or {}
    version = running.get("version") or "unknown"
    remedy = payload.get("remedy") or {}

    if payload.get("registry") == "unavailable":
        return [
            Finding(
                probe="bundle_staleness",
                status=STATUS_UNKNOWN,
                summary=(
                    "bundle census: project registry unavailable (launcher.db "
                    "not found or unreadable) — no project's bundle state "
                    "could be determined. Normal on a fresh root install "
                    "before the first launcher boot; never read as '0 stale'."
                ),
                detail={"registry": "unavailable"},
            )
        ]

    findings: list[Finding] = []
    if stale > 0:
        stale_names = [
            r.get("name") or "?" for r in rows if r.get("verdict") == "stale"
        ]
        findings.append(
            Finding(
                probe="bundle_staleness",
                status=STATUS_PROBLEM,
                summary=(
                    f"{stale} of {len(rows)} registered project(s) have a "
                    f"bundle older than the current orchestrator ({version}): "
                    f"{', '.join(stale_names)}. Per-project bundle updates "
                    "are manual — an orchestrator update never touches them"
                ),
                fix=FIX_DEFER,
                condition_id=CID_BUNDLES_STALE,
                command=str(remedy.get("cli") or ""),
                detail={
                    "stale": stale,
                    "unknown": unknown,
                    "current": current,
                    "stale_projects": stale_names,
                    "gui_remedy": remedy.get("gui"),
                },
            )
        )
    else:
        findings.append(
            Finding(
                probe="bundle_staleness",
                status=STATUS_OK,
                summary=(
                    f"all {current} registered project bundle(s) match the "
                    f"shipped set for orchestrator {version}"
                    + (
                        f" ({unknown} could not be determined)"
                        if unknown
                        else ""
                    )
                ),
                condition_id=CID_BUNDLES_STALE,
                detail={
                    "stale": 0,
                    "unknown": unknown,
                    "current": current,
                },
            )
        )

    for row in rows:
        verdict = row.get("verdict")
        name = row.get("name") or "?"
        if verdict == "stale":
            changed = row.get("changed_files") or []
            findings.append(
                Finding(
                    probe="bundle_staleness",
                    status=STATUS_PROBLEM,
                    summary=(
                        f"{name}: {len(changed)} bundle file(s) differ from "
                        f"what an update would ship"
                    ),
                    fix=FIX_DEFER,
                    command=(
                        "python -m vco_lib.project_init install-bundle "
                        f"--folder \"{row.get('folder')}\" --update --json"
                    ),
                    detail={
                        "project": name,
                        "changed_files": changed,
                        "recorded": row.get("recorded"),
                        "user_modified": row.get("user_modified", 0),
                    },
                )
            )
        elif verdict == "unknown":
            findings.append(
                Finding(
                    probe="bundle_staleness",
                    status=STATUS_UNKNOWN,
                    summary=f"{name}: bundle state unknown ({row.get('reason')})",
                    detail={"project": name, "reason": row.get("reason")},
                )
            )
    return findings


#: probe id → (callable, scopes).
#:
#: ``boot`` is the cheap subset: in-process resolution + file reads only. Two
#: probes are deliberately EXCLUDED from it:
#:
#: * ``npm_pins`` — one ``npm list -g`` subprocess per pin; seconds, not
#:   milliseconds, and boot latency is user-visible.
#: * ``prereqs`` — needs the ``--bootstrap`` envelope, which only install.py
#:   builds.
#:
#: ``launcher_binary_fresh`` is excluded for a different reason: at BOOT the
#: launcher runs its OWN Rust freshness probe (``binary_freshness::
#: reconcile_dist_at_rest``), which sees the running process's compiled-in
#: version — the one input the Python leg structurally cannot read. Running
#: both would be two implementations answering the same question from
#: different evidence, and the weaker one would sometimes contradict the
#: stronger. The Python leg stays for the ``full`` scope (install/update + the
#: CLI), where no launcher process is doing the asking.
#:
#: ``disk_space`` IS in the boot subset: one ``shutil.disk_usage`` call per
#: distinct filesystem (see :func:`probe_disk_space`) is cheaper than any file
#: read the other boot probes already do, and a machine that cannot write is
#: exactly the state a user needs to hear about BEFORE they start working, not
#: at their next update.
#:
#: **The v0.2.92 probes are ``full``-only, and the reason is a promise, not a
#: cost.** ``deferral_ledger.rs::run_boot_doctor_and_retries`` counts every
#: ``problem`` finding the boot pass returns and logs "N problem(s) recorded
#: in the deferral ledger — see the launcher's Updates page". That sentence is
#: true today because every boot-scope problem either carries a doctor-owned
#: ``condition_id`` that :func:`emit_findings` writes, or reports entries that
#: are already IN the ledger. ``source_currency`` / ``head_attached`` /
#: ``stale_vct_deploy`` have no registered condition of their own — registering
#: one means a row in ``vco_lib/deferral_conditions.toml``, which this lane
#: does not own — so putting them in the boot subset would make the launcher
#: point users at a panel that cannot show them. That is the same class of
#: defect as ``update.log``: a diagnostic that names something absent. They run
#: where they are read: install/update's end-of-run report and ``vco doctor``.
def probe_claude_code_trust(folder: Path, res: DoctorResolvers, ctx: dict) -> list[Finding]:
    """Is this folder's Claude Code trust flag intact? (register 14(b))

    The 2026-09-20 notification storm's engine: the Claude Code CLI owns
    and rewrites ``~/.claude.json`` wholesale, and a rewrite cycle can
    leave ``projects["<folder>"].hasTrustDialogAccepted`` False. Every
    headless ``claude -p`` in the folder then fails with "this workspace
    has not been trusted" — which VCO's own shipped generators invoke per
    node. The doctor names the state so the next occurrence reads as one
    actionable line instead of a 300-toast mystery.

    Read-only on ``~/.claude.json`` (the CLI's file; nothing here writes
    it — re-accepting the dialog is the user's act, once, interactively).
    Routed through :func:`vco_lib.paths.user_home` so the suite's
    hermeticity redirect steers it (a probe reading the REAL
    ``~/.claude.json`` under pytest is exactly what conftest's guard
    exists to catch). Positive evidence only: an unreadable file or a
    missing entry is NOT a verdict (the folder may simply never have
    been prompted).
    """
    import json  # noqa: PLC0415

    from vco_lib.paths import user_home  # noqa: PLC0415

    cc_json = user_home() / ".claude.json"
    try:
        data = json.loads(cc_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [Finding(
            probe="claude_code_trust",
            status=STATUS_UNKNOWN,
            summary="could not read ~/.claude.json — trust state unknown",
        )]
    entry = (data.get("projects") or {}).get(str(folder))
    if not isinstance(entry, dict):
        return [Finding(
            probe="claude_code_trust",
            status=STATUS_OK,
            summary="no Claude Code trust entry for this folder yet",
        )]
    if entry.get("hasTrustDialogAccepted") is False:
        return [Finding(
            probe="claude_code_trust",
            status=STATUS_PROBLEM,
            summary=(
                "this workspace's Claude Code trust flag is False — every "
                "headless claude invocation (KG/code summaries) fails with "
                "'not been trusted'"
            ),
            command="",
            detail={"recovery": (
                "run claude interactively once in this folder and accept "
                "the trust dialog"
            )},
        )]
    return [Finding(
        probe="claude_code_trust",
        status=STATUS_OK,
        summary="Claude Code trust flag intact for this folder",
    )]


PROBES: dict = {
    "mcp_commands_spawnable": (probe_mcp_commands_spawnable, (SCOPE_FULL, SCOPE_BOOT)),
    "launcher_binary_fresh": (probe_launcher_binary_fresh, (SCOPE_FULL,)),
    "deferral_ledger": (probe_deferral_ledger, (SCOPE_FULL, SCOPE_BOOT)),
    "disk_space": (probe_disk_space, (SCOPE_FULL, SCOPE_BOOT)),
    "prereqs": (probe_prereqs, (SCOPE_FULL,)),
    "npm_pins": (probe_npm_pins, (SCOPE_FULL,)),
    "vco_lib_editable": (probe_vco_lib_editable, (SCOPE_FULL,)),
    "source_currency": (probe_source_currency, (SCOPE_FULL,)),
    "last_update_run": (probe_last_update_run, (SCOPE_FULL,)),
    # v0.2.95 WP-2: full-only, for a COST reason this time (the v0.2.92
    # probes above are full-only for a promise reason — no registered
    # condition — which does not apply here: this one has a row in
    # `deferral_conditions.toml`, so the launcher's boot counter could
    # legitimately point at the Updates panel for it). The decision itself is
    # three file reads, but one of them goes through
    # :func:`last_successful_install`, which scans the WHOLE append-only
    # `install.jsonl` — 34 ms on the 4.4 MB log of a two-year-old install
    # root, and the file has no rotation, so that number only grows. Boot
    # latency is user-visible; the question is about a COMPLETED install, and
    # its readers are install.py's end-of-run report and `vco doctor`.
    # Promoting it to `boot` is a real option and a scope decision, not a
    # refactor: it would make the launcher's own self-update the surface that
    # reports its own omission at the very next boot.
    "install_completeness": (probe_install_completeness, (SCOPE_FULL,)),
    "diagnostic_files": (probe_diagnostic_files, (SCOPE_FULL,)),
    "stale_vct_deploy": (probe_stale_vct_deploy, (SCOPE_FULL,)),
    # v0.2.92 WP-D (R27 surface a): full-only — the census runs one engine
    # dry-run per registered project. It also EMITS/RESOLVES the
    # `project_bundles_stale` ledger entry itself (the census owns the
    # lifecycle; the doctor only reports it), so the boot-ledger promise
    # above is preserved: no boot-scope problem can name a condition the
    # panel cannot show, because this probe never runs at boot.
    "bundle_staleness": (probe_bundle_staleness, (SCOPE_FULL,)),
    # v0.2.92 D18 re-closure (Fable R6 MAJOR-R6-1): full-only — the scan is
    # one Weaviate schema listing plus one count and one bounded sample per
    # *_KnowledgeGraph class (network reads; see the probe's docstring for
    # the boot-promise reasoning). Doctor-owned cid, emitted here and cleared
    # by the same reading (self-resolving + the registry probe
    # `kg_binding_evidence_still_mismatched`).
    "kg_binding_evidence": (probe_kg_binding_evidence, (SCOPE_FULL,)),
    # v0.2.92 BLOCKER-1: full-only — one localhost /health call. See the
    # probe's docstring for why this is the only service whose IMAGE can go
    # stale under a healthy update.
    "code_embed_image": (probe_code_embed_image, (SCOPE_FULL,)),
    # v0.2.95 R5c: full-only — it RUNS the registered entry point
    # (`--version`), and it reports nothing at all on the majority of machines,
    # where the gateway's login-time autostart was never opted into.
    "model_gateway_runnable": (probe_model_gateway_runnable, (SCOPE_FULL,)),
    # v0.2.96 WP-7b: full-only — local file reads + one sha256 per knowledge
    # node (the whole tree at the orchestrator root). REPORTED-not-owned cid:
    # `kg_summaries_degraded` belongs to `vco_lib.summary_health` (paired
    # resolution), so this probe never emits or resolves it — the boot-ledger
    # promise is preserved trivially (it never runs at boot) and the
    # end-of-update report names the count, not just the cid.
    "summary_pending": (probe_summary_pending, (SCOPE_FULL,)),
    "claude_code_trust": (probe_claude_code_trust, (SCOPE_FULL,)),
}


#: v0.2.95 F4 — per-process cache for the checkout's install.py, loaded by
#: path. ``None`` (cached failure) matters as much as a module: it stops a
#: standalone doctor run re-executing a broken file once per probe.
_INSTALL_MODULE_CACHE: dict = {}


def _load_install_module(install_root: Path):
    """Load the install root's ``install.py`` BY PATH and cache it.

    The bootstrap envelope and the OS→``launcher/dist/<arch>/`` mapping each
    have exactly ONE home — install.py — and the doctor must not mirror either
    (cross-language rule A: reuse, never duplicate). This loader is how the
    STANDALONE CLI reaches those producer functions directly, in-process: no
    shell-out, no second interpreter, and the producers' own per-sub-probe
    timeouts bound the cost. ``main()`` is guarded by ``__name__`` so importing
    is side-effect-free apart from cheap top-level constants; the ONE
    import-time exit (the Python-version sentinel) is caught here so a
    too-old interpreter yields "facts unavailable", not a dead CLI.

    Returns the module, or ``None`` when the file is absent or failed to
    import. Never raises.
    """
    try:
        root = Path(install_root)
        key = str(root.resolve()) if root.exists() else str(root)
        if key in _INSTALL_MODULE_CACHE:
            return _INSTALL_MODULE_CACHE[key]
        mod = None
        path = root / "install.py"
        if path.is_file():
            import importlib.util

            spec = importlib.util.spec_from_file_location(
                "_vco_doctor_install_producer", path
            )
            if spec is not None and spec.loader is not None:
                candidate = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = candidate
                try:
                    spec.loader.exec_module(candidate)
                    mod = candidate
                except (Exception, SystemExit):  # noqa: BLE001 — no producer, no verdict
                    mod = None
        _INSTALL_MODULE_CACHE[key] = mod
        return mod
    except Exception:  # noqa: BLE001 — obtaining facts must never break the pass
        return None


def supply_missing_install_context(folder: Path, ctx: dict) -> dict:
    """Fill ONLY the missing install.py-owned context keys. Never raises.

    v0.2.95 F4: a standalone ``vco doctor`` used to print
    ``launcher_binary_fresh`` / ``prereqs`` as "not evaluated — the caller
    supplied no envelope", which reads as ABSENCE of a problem. It now obtains
    the facts itself through the SAME producers install.py's own doctor phase
    calls (``_bootstrap_build_envelope`` / ``_launcher_binary_relative_path``
    + ``_read_launcher_version``), loaded by path — one home, two callers.

    Keys the caller already supplied (install.py injects both mid-run) are
    never overwritten, so the in-run path is byte-identical to before. A
    producer that fails leaves its key ABSENT on purpose: the probes then
    report tri-state ``unknown`` naming :data:`BOOTSTRAP_COMMAND`, which is
    the honest sentence — not evaluated, and here is what would evaluate it.
    """
    try:
        root = Path(folder)
        need_envelope = "bootstrap_envelope" not in ctx
        need_launcher = "launcher_probe_extras" not in ctx
        if not (need_envelope or need_launcher):
            return ctx
        mod = _load_install_module(root)
        if mod is None:
            return ctx
        if need_envelope:
            try:
                envelope = mod._bootstrap_build_envelope(root)
            except (Exception, SystemExit):  # noqa: BLE001 — facts stay absent
                envelope = None
            if isinstance(envelope, dict):
                ctx["bootstrap_envelope"] = envelope
        if need_launcher:
            try:
                from vco_lib import deferral_probes

                subdir, fname = mod._launcher_binary_relative_path()
                ctx["launcher_probe_extras"] = deferral_probes.launcher_probe_extras(
                    subdir, fname, mod._read_launcher_version(root)
                )
            except (Exception, SystemExit):  # noqa: BLE001 — facts stay absent
                pass
    except Exception:  # noqa: BLE001 — supplying context is best-effort
        pass
    return ctx


def reconcile_probe_cleared(
    folder: Path, *, extras: Optional[dict] = None, log: Callable[[str], None] = print
) -> list:
    """Resolve ledger entries whose registry probe says they are provably over.

    v0.2.95 F1: ``hub_restart_failed_after_abort`` could survive forever — its
    only clearer ran BEFORE the hub restart step inside ``install.py --update``,
    and a standalone doctor never re-probed foreign cids at all
    (:func:`resolve_healthy_findings` is scoped to doctor-OWNED conditions).
    The doctor is the reconciler's OBSERVE step and runs LAST
    (arch review §7, steps 2–5), so this pass is where a hub that is back up
    gets its row removed — on every surface that renders the ledger, not only
    on the next ``--update``.

    Read → probe → resolve, all through the shared machinery: the report is
    read ONLY (no unlocked write-back — the resolve goes through the ONE
    locked emitter), every entry is probed by the SAME
    :func:`vco_lib.deferral_probes.probe_report` install.py uses, and ONLY a
    positive ``False`` clears; "still applies" and "could not determine" both
    keep the entry. That asymmetry is the safety property. Every clear leaves
    a B-F9 audit row (``resolved_by_registry_probe``) naming the probe.

    Returns the cleared cids. Never raises.
    """
    try:
        from vco_lib import deferral_probes
        from vco_lib.deferral_emit import resolve_conditions
        from vco_lib.deferral_report import DeferralReport

        root = Path(folder)
        report = DeferralReport.read(root)
        if not list(getattr(report, "entries", []) or []):
            return []
        result = deferral_probes.probe_report(root, report, extras)
        cleared: list = []
        for cid in list(result.resolvable):
            name = deferral_probes.registry_probe_name(cid) or ""
            if resolve_conditions(root, [cid]) > 0:
                deferral_probes.record_probe_resolution(root, cid, name)
                cleared.append(cid)
                log(
                    f"[doctor] reconcile: {cid} cleared — registry probe "
                    f"`{name}` re-derived the state and the condition is over."
                )
        return cleared
    except Exception:  # noqa: BLE001 — reconcile is best-effort observability
        return []


def run_doctor(
    folder: Path,
    *,
    scope: str = SCOPE_FULL,
    resolvers: Optional[DoctorResolvers] = None,
    context: Optional[dict] = None,
) -> DoctorReport:
    """Run the probe set and return the report. Never raises.

    Args:
        folder: the project/install folder whose ledger + tree are probed.
        scope: :data:`SCOPE_FULL` (install/update + CLI) or :data:`SCOPE_BOOT`
            (the launcher's cheap subset).
        resolvers: environment seam (tests inject a fake machine).
        context: caller-supplied facts probes cannot derive without copying
            knowledge that has a home elsewhere — ``bootstrap_envelope``
            (install.py's ``--bootstrap`` payload) and
            ``launcher_probe_extras`` (the OS→dist-subdir mapping). The
            engine also puts ``scope`` here, so a probe whose COST differs by
            scope (``source_currency``'s one network call) decides that in
            itself rather than the caller having to remember.

    A probe that raises yields an ``unknown`` finding: the doctor is layered
    ON TOP of a run that already succeeded, so it must never be able to fail
    that run.
    """
    res = resolvers or DoctorResolvers()
    ctx = dict(context or {})
    ctx["scope"] = scope
    report = DoctorReport(folder=Path(folder), scope=scope)
    for probe_id, (fn, scopes) in PROBES.items():
        if scope not in scopes:
            continue
        try:
            report.findings.extend(fn(Path(folder), res, ctx))
        except Exception as exc:  # noqa: BLE001 — one probe never breaks the pass
            report.findings.append(
                Finding(
                    probe=probe_id,
                    status=STATUS_UNKNOWN,
                    summary=f"probe raised: {exc}",
                )
            )
    return report


# ---------------------------------------------------------------------------
# Emission — the `defer` half of the fix boundary
# ---------------------------------------------------------------------------


def _npx_entry(finding: Finding):
    from vco_lib.deferral_report import DeferralEntry

    return DeferralEntry(
        condition_id=CID_NPX_MISSING,
        title="npx not resolvable — npx-based MCPs cannot spawn",
        detected=finding.summary,
        why_deferred=(
            "Installing Node.js changes the user's machine, so VCO "
            "never does it unattended. Until npx resolves, every MCP "
            "registered as `npx` (playwright by default; mermaid when "
            "enabled) fails to start — Claude Code shows only "
            "'Failed to connect', with no indication that the cause is "
            "a missing binary. This entry clears itself on the first "
            "install/update run that finds npx."
        ),
        command_to_apply=finding.command,
        severity="warning",
        kg_node_refs=["docs/TROUBLESHOOTING.md"],
    )


def _disk_space_entry(finding: Finding):
    from vco_lib.deferral_report import DeferralEntry

    detail = finding.detail or {}
    mounts = [m for m in (detail.get("mounts") or []) if isinstance(m, dict)]
    return DeferralEntry(
        condition_id=CID_DISK_SPACE_LOW,
        title="Low disk space — VCO writes may start failing",
        detected=finding.summary,
        why_deferred=(
            "Freeing space means DELETING the user's files, which VCO never "
            "does unattended. This is reported rather than fixed because it is "
            "a true description of the machine, not a VCO fault. Everything "
            "downstream of a full disk fails in a way that does not name the "
            "cause — a Weaviate write, a launcher.db commit, a dist-binary "
            "swap and an RL archive each surface as their own local error — so "
            "the honest place to say it is here, once, up front. The entry "
            "CLEARS ITSELF on the next VCO run whose space check comes back "
            "above the floor; there is nothing to dismiss by hand."
        ),
        command_to_apply=finding.command,
        severity=(
            "critical" if detail.get("severity") == "critical" else "warning"
        ),
        dismiss_fields={"mount_paths": sorted(str(m.get("path", "")) for m in mounts)},
        kg_node_refs=["docs/TROUBLESHOOTING.md"],
    )


def _vco_lib_shadow_entry(finding: Finding):
    from vco_lib.deferral_report import DeferralEntry

    detail = finding.detail or {}
    return DeferralEntry(
        condition_id=CID_VCO_LIB_SHADOWED,
        title="vco_lib is not the checkout — hooks and MCPs run frozen code",
        detected=finding.summary,
        why_deferred=(
            "Repairing this means a pip reinstall plus a delete inside the "
            "user's venv, so VCO only does it as part of an install/update the "
            "user asked for — never from a read-only report. install.py's step "
            "4 performs exactly that repair, under positive-identification "
            "gates, and the doctor re-probes in the same run: this entry is "
            "therefore dropped by the very run that fixes it. It is reported "
            "here because the failure is otherwise SILENT — imports keep "
            "working, against code frozen at install time, so every later "
            "update appears to have no effect."
        ),
        command_to_apply=finding.command,
        severity="critical" if detail.get("state") == "site_packages" else "warning",
        kg_node_refs=["docs/TROUBLESHOOTING.md"],
    )


def _kg_binding_evidence_entry(finding: Finding):
    """The deferral the KG-binding probe owes — the three values, verbatim.

    One entry aggregates every mismatching project (``deferral_entries_for``
    dedupes by cid): each project's three-value statement names the binding,
    the expected name-derived class, and the file-backed evidence classes
    with counts, so the human reading UPDATE_DEFERRED.md can act without
    re-running anything. ``dismiss_fields`` key the dismissal on the
    affected project + evidence-class set, matching the registry's
    ``dismiss_key`` for this cid: cosmetic rewording can never re-fire a
    dismissal, but a NEW project joining the mismatch set must.
    """
    from vco_lib.deferral_report import DeferralEntry

    return DeferralEntry(
        condition_id=CID_KG_BINDING_EVIDENCE_MISMATCH,
        title=(
            "A project's KG data lives in a class its primary binding "
            "does not name"
        ),
        detected=finding.summary,
        why_deferred=(
            "This is the D18 existing-ghost case: the update-time "
            "prefix-adopt pass rebinds only a binding whose class is ABSENT "
            "from Weaviate, so a ghost class that has already received the "
            "project's writes is skipped by construction and silently "
            "unreported. VCO does not repair it itself: choosing between two "
            "EXISTING classes is the user's call (R38 — the previous "
            "automatic 'fix' here would have re-stamped the ghost), so this "
            "entry names the binding, the name-derived expected class and "
            "the file-backed evidence, and the human picks the right class "
            "in the launcher's Identity tab. The entry clears itself on the "
            "next run whose comparison agrees."
        ),
        command_to_apply=finding.command,
        severity="warning",
        dismiss_fields=_kg_binding_dismiss_fields(finding),
        kg_node_refs=["docs/TROUBLESHOOTING.md"],
    )


def _kg_binding_dismiss_fields(finding: Finding) -> dict:
    """``dismiss_key`` payload: the affected projects and evidence classes.

    Read off the finding's structured detail (the same payload the probe
    put there), so the dismissal identity and the reported values can never
    disagree.
    """
    projects = [
        str(p) for p in (finding.detail.get("projects") or [])
        if isinstance(p, str)
    ]
    classes = sorted(
        {
            str(c)
            for c in (finding.detail.get("evidence_classes") or [])
            if isinstance(c, str)
        }
    )
    return {"projects": sorted(projects), "evidence_classes": classes}


#: cid → builder for the ``DeferralEntry`` the doctor emits for it.
def _code_embed_image_entry(finding: Finding):
    from vco_lib.deferral_report import DeferralEntry

    return DeferralEntry(
        condition_id=CID_CODE_EMBED_IMAGE_STALE,
        title="code-embedding service runs an image older than its source",
        detected=finding.summary,
        why_deferred=(
            "Rebuilding a container image is minutes of the user's CPU (and, "
            "on a cold cache, a multi-GB base-image pull), so VCO does it as "
            "part of an install/update the user asked for — not from a "
            "read-only report. `python install.py --update` now passes "
            "`--build` whenever the image is not provably built from the "
            "checkout, and the doctor re-probes in the same run, so this "
            "entry is dropped by the very run that fixes it. It is reported "
            "because the failure is SILENT: `compose up` builds an image only "
            "when it is MISSING, and `--force-recreate` replaces the "
            "container from the SAME image, so the service can keep serving "
            "months-old code through every update. A pre-v0.2.92 image "
            "TRUNCATES over-window code at HTTP 200 instead of refusing it — "
            "the caller never learns that text was dropped. Rebuild the image "
            "BEFORE re-running the code-graph re-sync: doing it the other way "
            "round re-walks the whole graph through the old service."
        ),
        command_to_apply=finding.command,
        severity="warning",
        kg_node_refs=["docs/TROUBLESHOOTING.md"],
    )


def _gateway_unrunnable_entry(finding: Finding):
    from vco_lib.deferral_report import DeferralEntry

    return DeferralEntry(
        condition_id=CID_GATEWAY_UNRUNNABLE,
        title="The model gateway is registered to start at login, but cannot run",
        detected=finding.summary,
        why_deferred=(
            "Nothing here can fix it in place: the argv baked into the "
            "registration is what fails, and re-writing that registration is "
            "an install/update action. The init system is meanwhile retrying "
            "it on its own schedule, so a start from anywhere else would only "
            "add attempts that fail the same way. It is REPORTED because the "
            "failure is silent from outside: the unit is enabled, the launcher "
            "toggle reads `registered`, and on 2026-09-10 a machine sat in "
            "exactly this state for eight hours — the previous gateway process "
            "kept serving until the first restart, and then nothing did. "
            "`python install.py --update` re-renders the registration from the "
            "install root's venv and now verifies the entry point before "
            "writing it, so the run that fixes this also clears this entry."
        ),
        command_to_apply=finding.command,
        severity="warning",
        disposition="action_required",
        kg_node_refs=["docs/CONFIGURATION.md"],
    )


def _install_marker_unbacked_entry(finding: Finding):
    from vco_lib.deferral_report import DeferralEntry

    return DeferralEntry(
        condition_id=CID_INSTALL_MARKER_UNBACKED,
        title="This install's completion marker names a source it was never installed from",
        detected=finding.summary,
        why_deferred=(
            "Re-running the installer is minutes of work the user must choose "
            "to start (it rebuilds the venv's editable install, re-propagates "
            "the `.claude/` bundle, re-registers the MCPs and may re-seed the "
            "KG), so VCO reports it rather than starting one from a read-only "
            "health check. It is reported because the failure is QUIET by "
            "construction: `apply_launcher_update`, `force_resync_launcher` "
            "and `update_orchestrator_at` advance the whole source tree, "
            "rebuild only the launcher, and then re-assert `installed: true` "
            "over a venv, hooks, templates, MCP registrations, KG seed and "
            "schema none of them touched — after which the checkout is "
            "0-behind upstream, so the currency probe reports health and the "
            "install-age reading is dropped as not decision-relevant. Since "
            "v0.2.95 that write leaves `version` alone (install.py owns it) "
            "and stamps `post_source_only` instead, which does light the "
            "launcher's Updates badge — but a badge is a running launcher's "
            "affordance, and the states this catches include a CLI-only "
            "install and a SECOND clone updated in place by "
            "`update_orchestrator_at`, whose launcher may never start. The "
            "evidence used here needs none of that: the install manifest is "
            "written by those paths and says so, while the `.claude/` bundle "
            "manifest is written only by the bundle engine an installer run "
            "reaches. "
            "`python install.py --update` rewrites BOTH from the one tree and "
            "rebuilds the install manifest from scratch (so `post_source_only` "
            "does not survive it), after re-doing the venv, hooks, templates, "
            "MCP registrations, KG seed and schema work the marker already "
            "claimed; the doctor re-probes at the end of that same run — so "
            "the run that fixes this is the run that clears it."
        ),
        command_to_apply=finding.command,
        severity="warning",
        disposition="action_required",
        kg_node_refs=["docs/post-install/UPDATE-RECOVERY.md"],
    )


_ENTRY_BUILDERS: dict = {
    CID_NPX_MISSING: _npx_entry,
    CID_DISK_SPACE_LOW: _disk_space_entry,
    CID_VCO_LIB_SHADOWED: _vco_lib_shadow_entry,
    CID_KG_BINDING_EVIDENCE_MISMATCH: _kg_binding_evidence_entry,
    CID_KG_UNCLAIMED: _kg_unclaimed_entry,
    CID_CODE_EMBED_IMAGE_STALE: _code_embed_image_entry,
    CID_GATEWAY_UNRUNNABLE: _gateway_unrunnable_entry,
    CID_INSTALL_MARKER_UNBACKED: _install_marker_unbacked_entry,
}


def deferral_entries_for(report: DoctorReport) -> list:
    """Build the ``DeferralEntry`` objects a report's `defer` findings owe.

    Only findings that (a) are problems, (b) declare ``defer``, and (c) name a
    :data:`DOCTOR_OWNED_CIDS` ``condition_id`` produce an entry. Findings that
    map onto a cid ANOTHER component owns (``launcher_binary_stale``) are
    deliberately excluded — re-emitting someone else's condition from the
    doctor would fork its lifecycle. The doctor REPORTS those; their owner
    emits them.
    """
    entries = []
    seen: set[str] = set()
    for finding in report.problems:
        cid = finding.condition_id
        if finding.fix != FIX_DEFER or cid not in DOCTOR_OWNED_CIDS or cid in seen:
            continue
        builder = _ENTRY_BUILDERS.get(cid)
        if builder is None:  # pragma: no cover — guarded by DOCTOR_OWNED_CIDS
            continue
        seen.add(cid)
        entries.append(builder(finding))
    return entries


def healthy_condition_ids(report: DoctorReport) -> list[str]:
    """Self-resolving cids whose probe reported OK in THIS pass.

    The symmetric half of :func:`deferral_entries_for`: the same reading that
    would have emitted the entry is the one that clears it, so the promise
    "clears itself once the machine recovers" holds at every invocation point
    rather than only at the next ``--update`` re-probe pass.

    Restricted to :data:`DOCTOR_SELF_RESOLVING_CIDS` on purpose — a condition
    whose OK reading is not positive evidence that it is over (npx: the ledger
    entry may have been emitted by a different install root) must be left to
    its own lifecycle.
    """
    out: list[str] = []
    for finding in report.findings:
        cid = finding.condition_id
        if (
            finding.status == STATUS_OK
            and cid in DOCTOR_SELF_RESOLVING_CIDS
            and cid not in out
        ):
            out.append(cid)
    return out


def emit_findings(folder: Path, report: DoctorReport, *, sink=None) -> list[str]:
    """Emit the report's deferred conditions. Returns the emitted cids.

    Also RESOLVES the self-resolving cids whose probe came back OK — see
    :func:`resolve_healthy_findings`, which runs only on the no-sink path.

    Args:
        sink: optional object with ``add_entry`` (install.py's in-flight run
            report). When given, entries are ADDED to it so install.py's single
            authoritative write carries them — the emitter is never called
            mid-run behind ``finalize()``'s back. Without a sink the locked
            emitter writes directly (the CLI path).
    """
    entries = deferral_entries_for(report)
    if sink is None:
        resolve_healthy_findings(Path(folder), report)
    if not entries:
        return []
    if sink is not None:
        for entry in entries:
            sink.add_entry(entry)
        return [e.condition_id for e in entries]
    try:
        from vco_lib.deferral_emit import emit_entries

        emit_entries(Path(folder), entries)
    except Exception:  # noqa: BLE001 — reporting must never break the caller
        return []
    return [e.condition_id for e in entries]


def resolve_healthy_findings(folder: Path, report: DoctorReport) -> list[str]:
    """Resolve the ledger entries this pass's OK readings clear. Never raises.

    Only ever called on the NO-SINK path. With a sink, install.py's
    ``InstallDeferralFlow.finalize()`` is still pending (the same window that
    keeps ``auto_fix=False`` on the install path), and the ``--update``
    re-probe pass already clears these through the registry probe — so nothing
    is lost by staying out of it.

    v0.2.91 dogfood fix: the ORIGINAL reason for the gate — a mid-run resolve
    being resurrected by ``finalize()``'s rebuild-from-memory — no longer
    holds. ``finalize`` now drops a seeded entry that vanished from disk during
    the run, whoever removed it, so a resolve landing in that window is
    honoured. The gate is KEPT as belt-and-braces (one writer per run is still
    the simpler invariant), not because the race is live. Stating that
    explicitly so the next reader does not treat a stale rationale as a
    constraint.
    """
    cids = healthy_condition_ids(report)
    if not cids:
        return []
    try:
        from vco_lib.deferral_emit import resolve_conditions

        resolve_conditions(Path(folder), cids)
    except Exception:  # noqa: BLE001 — clearing is best-effort observability
        return []
    return cids


def run_and_report(
    folder: Path,
    *,
    scope: str = SCOPE_FULL,
    sink=None,
    resolvers: Optional[DoctorResolvers] = None,
    context: Optional[dict] = None,
    printer: Optional[Callable[[str], None]] = None,
    auto_fix: bool = True,
    emit: bool = True,
) -> DoctorReport:
    """Full doctor pass: probe → print → emit deferrals → dispatch retries.

    This is the ONE function the three invocation points call, so the phase
    can never mean three different things.

    ``auto_fix`` is the one knob the three invocation points legitimately
    differ on, and install.py passes ``False`` for three independent reasons:

    1. **Write race.** A retry resolves its condition through the locked
       emitter, but install.py's ``InstallDeferralFlow.finalize()`` is still
       pending and re-merges foreign entries from disk. A resolve landing
       between that read and its write would be resurrected by install.py's
       own final write — the entry would look immortal for one more cycle.
    2. **Redundant.** An ``--update`` already re-ran the KG seed (step 7c)
       minutes earlier in the same process; retrying it at the end of that run
       repeats work that just happened.
    3. **Blocking.** A full seed can take minutes. An install that appears to
       hang after its last visible step is the exact UX failure v0.2.53's
       dot-cycle work went after.

    The retry's real triggers are therefore the session-start owed-work check
    (where the containers hook has usually just started the backend, nothing
    else is mid-write, and the driver is detached) and the on-demand CLI. The
    install-time pass still REPORTS the owed work by name, so it is visible
    either way. See :mod:`vco_lib.deferral_retry` for the gate order.

    v0.2.95 F1/F4 — a standalone pass (``sink is None``: the CLI, not an
    in-flight install run) first PREFLIGHTS: it obtains the install.py-owned
    facts it was not handed (:func:`supply_missing_install_context`) and then
    reconciles the ledger through the registry probes
    (:func:`reconcile_probe_cleared`) BEFORE probing, so the report this pass
    prints describes the ledger as it stands after provably-over entries were
    cleared — a doctor that reports an entry as actionable and then, moments
    later in the same run, clears it is the F1 defect shape in miniature. The
    reconcile is gated on ``emit`` for the same reason the retries are:
    ``--no-emit`` means "tell me, change nothing". With a sink (the install
    path) both preflight halves are skipped: install.py injects the facts
    itself, its own re-probe pass owns the mid-run clears, and one writer per
    run stays the simpler invariant.
    """
    out = printer or print
    ctx = dict(context or {})
    if sink is None:
        supply_missing_install_context(Path(folder), ctx)
        if emit:
            reconcile_probe_cleared(
                Path(folder), extras=ctx.get("launcher_probe_extras") or {}, log=out
            )
    report = run_doctor(folder, scope=scope, resolvers=resolvers, context=ctx)
    out("")
    out("[doctor] Environment check:")
    for line in report.render_lines():
        out(line)
    emitted = emit_findings(folder, report, sink=sink) if emit else []
    if emitted:
        out(f"[doctor] Deferred (see UPDATE_DEFERRED.md): {', '.join(emitted)}")
    if auto_fix and any(f.fix == FIX_AUTO for f in report.problems):
        from vco_lib import deferral_retry

        results = deferral_retry.dispatch(Path(folder))
        for res in results:
            out(f"[doctor] retry {res.condition_id}: {res.status} — {res.detail}")
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``vco doctor`` / ``python -m vco_lib.doctor``.

    Exit 0 when no probe reported a problem, 1 when any did. ``unknown``
    findings never fail the command — "I could not check" is not "broken".
    """
    parser = argparse.ArgumentParser(
        prog="vco doctor",
        description=(
            "Verify this install's environment assumptions against what is "
            "actually registered and delivered, then report (and, for owed "
            "work only, retry)."
        ),
    )
    add_arguments(parser)
    args = parser.parse_args(argv)
    return run_from_args(args)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Shared flag set — used by ``vco doctor`` and the module CLI."""
    parser.add_argument(
        "--folder", type=Path, default=None,
        help="project/install folder to check (default: current directory)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the machine-readable report",
    )
    parser.add_argument(
        "--scope", choices=(SCOPE_FULL, SCOPE_BOOT), default=SCOPE_FULL,
        help="probe set to run (default: full)",
    )
    parser.add_argument(
        "--no-auto-fix", dest="auto_fix", action="store_false",
        help="report only; never dispatch the owed-work retries",
    )
    parser.add_argument(
        "--no-emit", dest="emit", action="store_false",
        help="do not write deferral entries for deferred findings",
    )


def run_from_args(args: argparse.Namespace) -> int:
    """Execute a parsed doctor invocation. Shared by both CLI surfaces.

    Exit 1 iff a probe reported a PROBLEM. ``--json`` prints only the payload
    (stdout is a machine contract on that path — the v0.2.84 lesson), so the
    human report is never interleaved with it.
    """
    folder = Path(args.folder) if args.folder else Path.cwd()
    emit = getattr(args, "emit", True)
    auto_fix = getattr(args, "auto_fix", True)
    if args.json:
        # v0.2.95 F1/F4: the --json surface is a STANDALONE doctor too, so it
        # gets the same preflight as run_and_report — obtain the facts, then
        # reconcile the ledger — with the reconcile honouring --no-emit and
        # the log line silenced (stdout is a machine contract on this path;
        # the cleared cids are observable through the payload's ledger probe).
        ctx: dict = {}
        supply_missing_install_context(folder, ctx)
        if emit:
            reconcile_probe_cleared(
                folder, extras=ctx.get("launcher_probe_extras") or {}, log=lambda _l: None
            )
        report = run_doctor(folder, scope=args.scope, context=ctx or None)
        if emit:
            emit_findings(folder, report)
        print(json.dumps(report.to_dict()))
        return 0 if report.ok else 1
    report = run_and_report(
        folder,
        scope=args.scope,
        # --no-emit means "tell me, change nothing": no ledger write AND no
        # retry dispatch. A retry is a bigger side effect than the entry it
        # would clear, so the quieter flag must not leave it armed.
        sink=None,
        auto_fix=auto_fix and emit,
        emit=emit,
    )
    return 0 if report.ok else 1


__all__ = [
    "BOOTSTRAP_COMMAND",
    "CID_CODE_EMBED_IMAGE_STALE",
    "CID_DISK_SPACE_LOW",
    "CID_INSTALL_MARKER_UNBACKED",
    "CID_KG_BINDING_EVIDENCE_MISMATCH",
    "CID_NPX_MISSING",
    "CID_VCO_LIB_SHADOWED",
    "DISK_CRITICAL_FREE_BYTES",
    "DISK_MIN_FREE_ENV",
    "DISK_MIN_FREE_GB_DEFAULT",
    "DOCTOR_OWNED_CIDS",
    "DOCTOR_SELF_RESOLVING_CIDS",
    "GIT_TIMEOUT_SECONDS",
    "INSTALL_LOG_REL",
    "INSTALL_MANIFEST_REL",
    "RUST_INSTALL_METHODS",
    "SOURCE_FALLBACK_BRANCH",
    "UPSTREAM_REMOTE",
    "VCT_CLI_REL",
    "VCT_GUARDS_PREFIX",
    "DoctorReport",
    "DoctorResolvers",
    "FIX_AUTO",
    "FIX_DEFER",
    "Finding",
    "PROBES",
    "SCHEMA_VERSION",
    "SCOPE_BOOT",
    "SCOPE_FULL",
    "STATUS_OK",
    "STATUS_PROBLEM",
    "STATUS_UNKNOWN",
    "SourceFacts",
    "add_arguments",
    "bare_command_names",
    "collect_source_facts",
    "command_is_path",
    "deferral_entries_for",
    "diagnostic_file_candidates",
    "disk_dismiss_fields",
    "disk_min_free_bytes",
    "disk_space_below_floor",
    "emit_findings",
    "healthy_condition_ids",
    "install_age_days",
    "last_successful_install",
    "main",
    "measure_disk_space",
    "parse_vct_guards",
    "probe_diagnostic_files",
    "probe_disk_space",
    "probe_install_completeness",
    "probe_kg_binding_evidence",
    "probe_last_update_run",
    "probe_source_currency",
    "probe_stale_vct_deploy",
    "probe_vco_lib_editable",
    "reconcile_probe_cleared",
    "resolve_healthy_findings",
    "run_and_report",
    "run_doctor",
    "run_from_args",
    "same_location",
    "supply_missing_install_context",
]


if __name__ == "__main__":  # pragma: no cover — CLI entry
    sys.exit(main())
