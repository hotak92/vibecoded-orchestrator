# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ONE home for "which Ollama models does this machine need" (v0.2.100 AD-6).

Owner rule (2026-09-29): pull ONLY the models VCO actually uses —

* **KG** — its active text embedder (``qwen3-embedding:0.6b`` /
  ``snowflake-arctic-embed2:latest``; OpenAI = no Ollama model), PLUS the
  second KG embedder ONLY when the user opted into dual embeddings;
* **code graph** — exactly ONE embedder, never dual: CodeSage runs in the
  code_embed service (NO Ollama model); otherwise jina or qwen3 (shared with
  the KG);
* **text generation** — the ONE model of the host's capability tier (no
  lower rungs);
* nothing else.

Before v0.2.100 the pull list had four homes (``EMBEDDING_CONFIGS``' static
lists, ``gpu_profile.apply_tier_overrides`` appending, ``install.py``'s
``_build_ollama_pull_list`` and the embedding service's write set), so a
replayed ``cpu`` profile pulled jina while qwen3 served code, and nothing
pulled arctic when the dual opt-in was switched on (L1-F11/F12).

:func:`plan` is PURE. :func:`plan_from_machine` reads the recorded install
profile plus the launcher.db dual-flag cascade (read-only) and is what both
``install.py`` (step 7) and the launcher's dual-flag toggle
(``python -m vco_lib.embedding_pull_plan ensure --json``) run, so the two can
never disagree.

The secondary-KG-embedder rule (:func:`kg_secondary_models`) is also what
``embedding_service.configured_text_models`` fans out to, so "pulled" and
"written" are the same set by construction.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from vco_lib.containers import runtime_command_hint

TEXT_QWEN3 = "qwen3-embedding:0.6b"
TEXT_ARCTIC = "snowflake-arctic-embed2:latest"
CODE_JINA = "unclemusclez/jina-embeddings-v2-base-code:latest"
CODE_CODESAGE = "codesage-large-v2"

#: ACTIVE_EMBEDDING profile name → the Ollama model it uses (None = not Ollama).
_KG_PROFILE_MODEL: Mapping[str, Optional[str]] = {
    "qwen3": TEXT_QWEN3,
    "arctic": TEXT_ARCTIC,
    "openai": None,
}

#: Where install.py records the profile this machine was installed with.
RECORD_REL = Path(".claude") / "state" / "ollama_pull_profile.json"

CODE_EMBED_UNAVAILABLE_CID = "code_embed_backend_unavailable"


@dataclass(frozen=True)
class PullPlan:
    """The exact Ollama model set, split by consequence of a failed pull.

    ``embedding`` models are load-bearing (a failed pull aborts the install);
    ``inference`` models are the text-generation tier. ``code_backend_unavailable``
    is set when the configured code backend (CodeSage via code_embed) was
    probed and is down — reported, never silently replaced.
    """

    embedding: tuple[str, ...]
    inference: tuple[str, ...]
    rationale: tuple[str, ...] = ()
    code_backend_unavailable: bool = False

    @property
    def models(self) -> tuple[str, ...]:
        return _dedup((*self.embedding, *self.inference))

    def to_json(self) -> dict[str, Any]:
        out = asdict(self)
        out["models"] = list(self.models)
        return out


@dataclass(frozen=True)
class KgSelection:
    """One KG embedding choice: the active embedder + the two dual opt-ins."""

    active: str
    write_all: bool = False
    arctic_secondary: bool = False
    origin: str = "install"


class PlanUnavailable(RuntimeError):
    """No recorded install profile and no launcher.db to derive one from."""


def _dedup(items: Iterable[Optional[str]]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for m in items:
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return tuple(out)


def _is_openai(model: str) -> bool:
    low = model.lower()
    return "openai" in low or low.startswith("text-embedding-")


def kg_model_for(active: str) -> Optional[str]:
    """Ollama model for a KG active embedder given as a profile name
    (``qwen3``/``arctic``/``openai``) or a model id; None when not Ollama."""
    if active in _KG_PROFILE_MODEL:
        return _KG_PROFILE_MODEL[active]
    return None if (not active or _is_openai(active)) else active


def kg_secondary_models(
    active_model: str, *, write_all: bool, arctic_secondary: bool
) -> tuple[str, ...]:
    """The Ollama-served SECONDARY KG embedders the dual opt-in writes.

    The rule ``embedding_service.configured_text_models`` fans out to (it
    calls this): nothing without ``DUAL_EMBEDDING_WRITE_ALL_SLOTS``; with it,
    qwen3 unless qwen3 is already active, plus arctic when
    ``DUAL_EMBEDDING_ARCTIC_SECONDARY`` is also on and arctic is not active.
    (The OpenAI secondary needs no Ollama model and stays in the service.)
    """
    if not write_all:
        return ()
    out: list[str] = []
    if active_model != TEXT_QWEN3:
        out.append(TEXT_QWEN3)
    if arctic_secondary and "arctic" not in active_model.lower():
        out.append(TEXT_ARCTIC)
    return tuple(out)


def plan(
    *,
    kg_active: str,
    dual_write_all: bool,
    dual_arctic_secondary: bool,
    code_backend: str,
    code_model: str,
    capability_tier: Sequence[str],
    profile_override: Optional[Sequence[str]],
    code_embed_reachable: Optional[bool] = None,
) -> PullPlan:
    """Pure: the exact model set for one KG choice + the machine's code/tier.

    ``capability_tier`` is the host's text-generation ladder (install.py's
    ``_inference_models_for_capability``); ``profile_override`` is a profile's
    explicit inference cap (``low_resource``), which wins over the tier.
    ``code_embed_reachable=False`` flags a down CodeSage backend — it never
    adds an Ollama code model in its place.
    """
    why: list[str] = []
    kg = kg_model_for(kg_active)
    why.append(f"KG active embedder: {kg or 'none (OpenAI)'}")
    active_id = kg or kg_active
    secondaries = kg_secondary_models(
        active_id, write_all=dual_write_all, arctic_secondary=dual_arctic_secondary
    )
    if secondaries:
        why.append("dual embeddings opted in: + " + ", ".join(secondaries))
    code: Optional[str] = None
    unavailable = False
    if code_backend == "openai" or _is_openai(code_model):
        why.append("code embedder: OpenAI (no Ollama model)")
    elif code_model == CODE_CODESAGE:
        why.append("code embedder: CodeSage via the code_embed service (no Ollama model)")
        if code_embed_reachable is False:
            unavailable = True
            why.append("code_embed service unreachable — reported, no substitute embedder")
    else:
        code = code_model
        why.append(f"code embedder: {code_model}")
    # Owner 2026-09-29: text generation pulls ONLY the single model the tier
    # uses — never the lower rungs. The ladder's first entry is that model (a
    # multi-rung tier recorded by an older run collapses to it here, the ONE
    # home of the rule); an absent lower rung at runtime degrades to the next
    # summary backend (summary_backends.ollama_available), never a pull.
    inference = (tuple(profile_override) if profile_override else tuple(capability_tier))[:1]
    why.append(
        ("text generation (profile cap): " if profile_override else "text generation tier: ")
        + (", ".join(inference) or "none")
    )
    return PullPlan(
        embedding=_dedup((kg, *secondaries, code)),
        inference=_dedup(inference),
        rationale=tuple(why),
        code_backend_unavailable=unavailable,
    )


def merge(plans: Sequence[PullPlan]) -> PullPlan:
    """Union of several plans (one per KG selection), order-preserving."""
    return PullPlan(
        embedding=_dedup(m for p in plans for m in p.embedding),
        inference=_dedup(m for p in plans for m in p.inference),
        rationale=_dedup(r for p in plans for r in p.rationale),
        code_backend_unavailable=any(p.code_backend_unavailable for p in plans),
    )


# ── the recorded profile ────────────────────────────────────────────────────


def record_profile(root: Path, embed_config: Mapping[str, Any],
                   capability_tier: Sequence[str]) -> dict[str, Any]:
    """Persist the install profile the plan derives from (install step 7)."""
    rec = {
        "text_model": embed_config.get("text_model"),
        "code_backend": embed_config.get("code_backend"),
        "code_model": embed_config.get("code_model"),
        "capability_tier": list(capability_tier),
        "profile_override": list(embed_config.get("inference_models_override") or []),
    }
    path = Path(root) / RECORD_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    from vco_lib.atomic import atomic_write_text

    atomic_write_text(path, json.dumps(rec, indent=2) + "\n")
    return rec


def read_record(root: Path) -> Optional[dict[str, Any]]:
    try:
        rec = json.loads((Path(root) / RECORD_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


# ── launcher.db (read-only) ─────────────────────────────────────────────────


def machine_kg_selections(launcher_db: Path) -> tuple[Optional[KgSelection], list[KgSelection]]:
    """``(machine_default, per_project)`` KG selections from launcher.db.

    Read-only, through config_projection's cascades (the ones the env
    projection and the hub use). Raises ``sqlite3.Error``/``OSError`` when the
    DB cannot be read — the caller decides what "unknown" means.
    """
    from vco_lib import config_projection as cp

    conn = cp._open_db_read_only(Path(launcher_db))
    try:
        default_active = cp._global_active_embedding(conn)
        default = KgSelection(
            active=default_active,
            write_all=cp._global_dual_flag(conn, cp.APP_STATE_KEY_DUAL_WRITE_DEFAULT),
            arctic_secondary=cp._global_dual_flag(conn, cp.APP_STATE_KEY_DUAL_ARCTIC_DEFAULT),
            origin="machine default",
        ) if default_active else None
        projects: list[KgSelection] = []
        for row in conn.execute("SELECT id, name FROM projects ORDER BY name").fetchall():
            write_all, _rl, arctic = cp._resolve_dual_flags_cascade(conn, row["id"])
            projects.append(KgSelection(
                active=cp._resolve_active_embedding_cascade(conn, row["id"]),
                write_all=write_all, arctic_secondary=arctic,
                origin=f"project {row['name']}",
            ))
        return default, projects
    finally:
        conn.close()


def plan_from_machine(
    root: Path,
    launcher_db: Optional[Path] = None,
    *,
    code_embed_reachable: Optional[bool] = None,
) -> PullPlan:
    """The machine's plan: recorded profile × every KG selection in launcher.db.

    Without launcher.db (a fresh install before the launcher's first boot)
    nobody can have opted into dual embeddings, so the recorded profile alone
    decides. Without a record (an install older than 0.2.100) the code model
    and machine default come from launcher.db and the inference tier is left
    to the next install run (stated in the rationale, never guessed).
    """
    rec = read_record(root) or {}
    notes: list[str] = []
    if launcher_db is None:
        from vco_lib.paths import launcher_db_path

        launcher_db = launcher_db_path()
    default: Optional[KgSelection] = None
    projects: list[KgSelection] = []
    db_ok = False
    if Path(launcher_db).is_file():
        try:
            default, projects = machine_kg_selections(Path(launcher_db))
            db_ok = True
        except (sqlite3.Error, OSError) as exc:
            notes.append(f"launcher.db unreadable ({exc}); dual opt-ins taken as off")
    if not rec and not db_ok:
        raise PlanUnavailable(
            f"no recorded install profile at {Path(root) / RECORD_REL} and no readable "
            f"launcher.db at {launcher_db} — run install.py first"
        )
    code_model = str(rec.get("code_model") or "")
    code_backend = str(rec.get("code_backend") or "")
    if not code_model and db_ok:
        code_model, code_backend = _db_default_code_model(Path(launcher_db))
    if not rec:
        notes.append("no recorded install profile: text-generation tier left to the next install run")
    selections: list[KgSelection] = []
    if default is not None:
        selections.append(default)
    elif rec.get("text_model"):
        selections.append(KgSelection(active=str(rec["text_model"]), origin="install profile"))
    selections.extend(projects)
    plans = [
        plan(
            kg_active=s.active,
            dual_write_all=s.write_all,
            dual_arctic_secondary=s.arctic_secondary,
            code_backend=code_backend,
            code_model=code_model,
            capability_tier=rec.get("capability_tier") or (),
            profile_override=rec.get("profile_override") or None,
            code_embed_reachable=code_embed_reachable,
        )
        for s in selections
    ]
    merged = merge(plans)
    return PullPlan(
        embedding=merged.embedding,
        inference=merged.inference,
        rationale=_dedup((*notes, *merged.rationale)),
        code_backend_unavailable=merged.code_backend_unavailable,
    )


def _db_default_code_model(launcher_db: Path) -> tuple[str, str]:
    from vco_lib import config_projection as cp

    conn = cp._open_db_read_only(launcher_db)
    try:
        model = cp._fetch_app_state_str(conn, "default_code_embedding") or ""
    finally:
        conn.close()
    backend = "gpu" if model == CODE_CODESAGE else ("openai" if _is_openai(model) else "ollama")
    return model, backend


def plan_for_install(
    root: Path,
    embed_config: Mapping[str, Any],
    *,
    capability_tier: Sequence[str],
    code_embed_url: Optional[str] = None,
    launcher_db: Optional[Path] = None,
    runtime: str = "",
) -> PullPlan:
    """install.py step 7: record this run's profile, then derive the machine
    plan exactly as the launcher toggle does. Applied to the FINAL config —
    after tier overrides and on a replayed ``embedding_mode`` alike. A
    CodeSage config probes the code_embed service (bounded) so an outage is
    reported instead of being papered over with another embedder — and a
    service still LOADING its model (a running container, or a port that
    accepts but has not answered yet) is not an outage (W3R-04)."""
    record_profile(root, embed_config, capability_tier)
    reachable: Optional[bool] = None
    if embed_config.get("code_model") == CODE_CODESAGE:
        from vco_lib import ollama_pull as _op

        state = _op.code_embed_state(
            code_embed_url, container_running=lambda: _op.code_embed_container_running(runtime))
        if state == _op.CODE_EMBED_WARMING:
            print("    - code_embed is loading its model (first start downloads it) — "
                  "warming, not down; nothing to do")
        reachable = None if state is None else state != _op.CODE_EMBED_DOWN
    return plan_from_machine(root, launcher_db, code_embed_reachable=reachable)


# ── the one code_embed-unavailable deferral ────────────────────────────────


def code_embed_unavailable_entry(detail: str) -> Any:
    """``code_embed_backend_unavailable`` — written by install step 7 only, for a
    code_embed that is DOWN (never for one still loading its model, and never
    from the embedding service's construction — W3R-04)."""
    from vco_lib.deferral_report import DeferralEntry

    return DeferralEntry(
        condition_id=CODE_EMBED_UNAVAILABLE_CID,
        title="Code-embedding backend (code_embed service) unavailable",
        detected=detail,
        why_deferred=(
            "The code graph is configured for CodeSage in the code_embed service. VCO "
            "does not switch the code graph to another embedder behind your back — one "
            "code graph keeps exactly one embedder. The knowledge graph is unaffected."
        ),
        command_to_apply=(
            f"Start the service: `{runtime_command_hint('start vco_code_embed')}`, "
            "or re-run `python install.py --update`. Developers who "
            "accept a mixed-embedder code graph may set VCO_CODE_EMBED_ALLOW_FALLBACK=1."
        ),
        severity="warning",
    )


# ── CLI ─────────────────────────────────────────────────────────────────────


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m vco_lib.embedding_pull_plan",
        description="Show or ensure the exact Ollama model set this machine uses.",
    )
    ap.add_argument("verb", choices=("show", "ensure"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--root", type=Path, default=None, help="install root")
    ap.add_argument("--launcher-db", type=Path, default=None)
    ap.add_argument("--wait-s", type=float, default=30.0,
                    help="ensure: bounded wait for Ollama to answer")
    args = ap.parse_args(argv)
    root = args.root or _repo_root()
    try:
        pp = plan_from_machine(root, args.launcher_db)
    except PlanUnavailable as exc:
        return _emit(args.json, {"ok": False, "error": str(exc)}, str(exc), 1)
    if args.verb == "show":
        return _emit(args.json, {"ok": True, "plan": pp.to_json()},
                     "\n".join([*pp.models, "", *pp.rationale]), 0)
    from vco_lib import ollama_pull

    out = ollama_pull.ensure_for_machine(pp, root, launcher_db=args.launcher_db,
                                         wait_s=args.wait_s, quiet=args.json)
    out["plan"] = pp.to_json()
    return _emit(args.json, out, out.get("error") or "ok", 0 if out["ok"] else 1)


def _emit(as_json: bool, payload: dict[str, Any], text: str, rc: int) -> int:
    if as_json:
        print(json.dumps(payload))
    else:
        print(text, file=sys.stderr if rc else sys.stdout)
    return rc


__all__ = [
    "KgSelection", "PlanUnavailable", "PullPlan", "code_embed_unavailable_entry",
    "kg_model_for", "kg_secondary_models", "machine_kg_selections", "merge", "plan",
    "plan_for_install", "plan_from_machine", "read_record", "record_profile",
]

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
