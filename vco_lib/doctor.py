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
mechanism that already exists —

    probe id                    composes
    ─────────────────────────── ────────────────────────────────────────────
    npx_resolvable              vco_lib.npx_resolver (the v0.2.51 ladder)
    mcp_commands_spawnable      ~/.claude.json entries × the same resolver
    npm_pins                    vco_lib.cli.verify (``vco verify-pins``)
    launcher_binary_fresh       vco_lib.deferral_probes (WP-A's freshness leg)
    deferral_ledger             the WP-B registry's own clear probes
    owed_retryable_work         the WP-B registry's ``auto_retryable`` class
    prereqs                     install.py's ``--bootstrap`` envelope (INJECTED
                                by install.py; not re-derived here)

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
with no live services — the v0.2.89 env-probing-hermeticity lesson. Nothing in
this module opens a socket by itself; the probes that need one delegate to a
helper the caller may replace.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

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

#: condition_id emitted when a bare-name MCP command cannot be resolved.
#: Registered ``action_required`` + install-owned, so it disappears on the
#: first run after the user installs Node (drop-when-absent).
CID_NPX_MISSING = "npx_missing_mcp_unspawnable"


@dataclass(frozen=True)
class Finding:
    """One probe's verdict."""

    probe: str
    status: str
    #: One-line human summary. Rendered verbatim in the CLI + the report.
    summary: str
    #: ``auto_fix`` or ``defer``. Meaningless when ``status != problem``.
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
                    "fix": f.fix,
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

    def resolve_deferral_report(self, folder: Path):
        if self.deferral_report is not None:
            return self.deferral_report(folder)
        try:
            from vco_lib.deferral_report import DeferralReport

            return DeferralReport.read(folder)
        except Exception:  # noqa: BLE001 — an unreadable ledger is "unknown"
            return None


def _read_claude_json_mcp_servers() -> dict:
    """``~/.claude.json`` → ``mcpServers`` mapping. ``{}`` on any failure.

    Same file the launcher's badge reads. Absent/unparseable degrades to an
    empty mapping, which yields an ``unknown`` finding rather than a false OK.
    """
    try:
        path = Path.home() / ".claude.json"
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
    """The exact command block the deferral + CLI both print."""
    if npm_present:
        return (
            "# npm is present but npx is not — symlink it onto PATH:\n"
            "#   ln -s \"$(dirname \"$(readlink -f \"$(command -v npm)\")\")/npx\" "
            "~/.local/bin/npx\n"
            "# then reopen Claude Code so the MCPs re-spawn."
        )
    return (
        "# Install Node.js 18+ (https://nodejs.org), then reopen Claude Code:\n"
        "#   node --version && npx --version\n"
        "# Nothing else is needed — VCO re-detects npx on the next run."
    )


def probe_launcher_binary_fresh(folder: Path, res: DoctorResolvers, ctx: dict) -> list[Finding]:
    """Is the delivered launcher binary the one the tree says it should be?

    Composes WP-A's freshness probe through its Python leg
    (``deferral_probes.launcher_binary_stale_still_applies``) rather than
    re-deriving the comparison. Surface-only by construction: a stale RUNNING
    binary is repaired by the user quitting and reopening the launcher, and
    decision #4 forbids the doctor from touching running binaries.
    """
    extras = ctx.get("launcher_probe_extras") or {}
    if not extras:
        return [
            Finding(
                probe="launcher_binary_fresh",
                status=STATUS_UNKNOWN,
                summary=(
                    "launcher freshness not evaluated — the caller supplied no "
                    "dist/binary facts (OS→dist-subdir mapping has one home in "
                    "install.py)"
                ),
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
            summary="launcher dist binary matches the tree",
        )
    ]


def probe_deferral_ledger(folder: Path, res: DoctorResolvers, ctx: dict) -> list[Finding]:
    """Summarise the ledger by disposition and name the retryable work owed.

    This is the probe that makes the doctor the AUTHORITATIVE end-of-update
    report: it reads the same ledger the CLAUDE.md reminder points at and
    splits it the way WP-B's registry declares, so "3 pending actions" never
    again means "3 records of things already done".
    """
    from vco_lib import deferral_registry, deferral_retry

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
    actionable, informational = deferral_registry.split_by_disposition(cids)
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
        findings.append(
            Finding(
                probe="owed_retryable_work",
                status=STATUS_PROBLEM,
                summary=(
                    f"{len(retryable)} condition(s) VCO can retry itself: "
                    f"{', '.join(retryable)}"
                ),
                fix=FIX_AUTO,
                detail={"condition_ids": retryable},
            )
        )
    return findings


def probe_prereqs(folder: Path, res: DoctorResolvers, ctx: dict) -> list[Finding]:
    """Consume the ``--bootstrap`` envelope's ``missing_prereqs``.

    install.py INJECTS the envelope it already built rather than the doctor
    shelling back into install.py (which would be circular and slow). Without
    an injected envelope this probe reports ``unknown`` — never a false OK.

    This is the "consumer that acts after install" report 6 §B.1 says is
    missing: the envelope's findings previously died in a stdout block.
    """
    envelope = ctx.get("bootstrap_envelope")
    if not isinstance(envelope, dict):
        return [
            Finding(
                probe="prereqs",
                status=STATUS_UNKNOWN,
                summary="no bootstrap envelope supplied — prereqs not re-checked",
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
PROBES: dict = {
    "mcp_commands_spawnable": (probe_mcp_commands_spawnable, (SCOPE_FULL, SCOPE_BOOT)),
    "launcher_binary_fresh": (probe_launcher_binary_fresh, (SCOPE_FULL,)),
    "deferral_ledger": (probe_deferral_ledger, (SCOPE_FULL, SCOPE_BOOT)),
    "prereqs": (probe_prereqs, (SCOPE_FULL,)),
    "npm_pins": (probe_npm_pins, (SCOPE_FULL,)),
}


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
            ``launcher_probe_extras`` (the OS→dist-subdir mapping).

    A probe that raises yields an ``unknown`` finding: the doctor is layered
    ON TOP of a run that already succeeded, so it must never be able to fail
    that run.
    """
    res = resolvers or DoctorResolvers()
    ctx = dict(context or {})
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


def deferral_entries_for(report: DoctorReport) -> list:
    """Build the ``DeferralEntry`` objects a report's `defer` findings owe.

    Only findings that (a) are problems, (b) declare ``defer``, and (c) name a
    ``condition_id`` produce an entry. Findings that map onto a cid ANOTHER
    component owns (``launcher_binary_stale``) are deliberately excluded here —
    re-emitting someone else's condition from the doctor would fork its
    lifecycle. The doctor REPORTS those; their owner emits them.
    """
    from vco_lib.deferral_report import DeferralEntry

    entries = []
    seen: set[str] = set()
    for finding in report.problems:
        cid = finding.condition_id
        if finding.fix != FIX_DEFER or cid != CID_NPX_MISSING or cid in seen:
            continue
        seen.add(cid)
        entries.append(
            DeferralEntry(
                condition_id=cid,
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
        )
    return entries


def emit_findings(folder: Path, report: DoctorReport, *, sink=None) -> list[str]:
    """Emit the report's deferred conditions. Returns the emitted cids.

    Args:
        sink: optional object with ``add_entry`` (install.py's in-flight run
            report). When given, entries are ADDED to it so install.py's single
            authoritative write carries them — the emitter is never called
            mid-run behind ``finalize()``'s back. Without a sink the locked
            emitter writes directly (the CLI path).
    """
    entries = deferral_entries_for(report)
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
    """
    out = printer or print
    report = run_doctor(folder, scope=scope, resolvers=resolvers, context=context)
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
        report = run_doctor(folder, scope=args.scope)
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
    "CID_NPX_MISSING",
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
    "add_arguments",
    "bare_command_names",
    "command_is_path",
    "deferral_entries_for",
    "emit_findings",
    "main",
    "run_and_report",
    "run_doctor",
    "run_from_args",
]


if __name__ == "__main__":  # pragma: no cover — CLI entry
    sys.exit(main())
