# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The "local summary model" switch (v0.2.106, owner ruling 2026-11-12).

VCO downloads exactly ONE local text-generation model into Ollama
(:mod:`vco_lib.embedding_pull_plan` — ``qwen3.5:0.8b`` on a small host, up
to ``qwen3.5:9b`` ≈ 6 GB) as the fallback tier of the KG-summary ladder
(``templates/scripts/summary_backends.py``: claude CLI → z.ai → QwenCloud →
**local Ollama** → OpenAI → Anthropic API). A machine with little free disk
may not want it. This module is the ONE home of that choice:

* **the setting** — :func:`resolve`: ``VCT_LOCAL_SUMMARY_MODEL`` (``on`` /
  ``off``; headless installs, first-install.* pass the environment through
  unchanged), else the launcher's Preferences → KG Summaries toggle
  (``app_state[kg_summary_local_model_enabled]``, ``"true"``/``"false"`` as
  the Rust ``app_state_set_bool`` writes them — MUST MATCH
  ``launcher/src/lib/preferences/loaders.ts``
  ``APP_STATE_KEY_LOCAL_SUMMARY_MODEL``), else ON — the behaviour before
  the switch existed.
* **its consumers** — the pull plan (:func:`embedding_pull_plan.plan_from_machine`:
  OFF → no text-generation model in the plan, so install, update, the
  launcher's "ensure" and the deferral retry all skip it) and the summary
  ladder (``summary_backends.ollama_available``: OFF → the local tier is
  skipped without asking Ollama for a model that was deliberately not
  pulled), plus ``process_documents``' direct local summary.
* **status** — :func:`status` (the launcher's Preferences row and
  ``vco doctor``'s ``local_summary_model`` probe): the setting, where it came
  from, the model and whether Ollama holds it (with its size).
* **remove** — :func:`remove`: turning the switch OFF never deletes a model
  already on disk (it is the user's data). The Preferences page offers an
  explicit, confirmed removal, which this function performs only when the
  switch is OFF (else the next update would download it again), the model is
  not one the plan still needs, and VCO owns the Ollama that holds it (the
  :func:`vco_lib.ollama_pull.service_container` predicate
  ``restart_owned_ollama`` uses).

CLI::

    python -m vco_lib.local_summary_model status [--json]
    python -m vco_lib.local_summary_model remove --yes [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

__all__ = [
    "APP_STATE_KEY",
    "ENV_SWITCH",
    "Setting",
    "enabled",
    "parse_switch",
    "remove",
    "resolve",
    "status",
]

#: Env override (headless installs). Values: on/off (also true/false, 1/0, yes/no).
ENV_SWITCH = "VCT_LOCAL_SUMMARY_MODEL"
#: launcher.db ``app_state`` key the Preferences toggle writes.
APP_STATE_KEY = "kg_summary_local_model_enabled"

SOURCE_ENV = "env"
SOURCE_PREFERENCES = "preferences"
SOURCE_DEFAULT = "default"

_ON = frozenset({"on", "true", "1", "yes"})
_OFF = frozenset({"off", "false", "0", "no"})

#: The exact text a user (or a printed remediation) needs to turn it off.
HOW_TO_TURN_OFF = (
    "launcher Preferences → KG Summaries → \"Local summary model\" off, or "
    f"{ENV_SWITCH}=off in the environment of a headless install"
)


def disk_hint() -> str:
    """The low-disk remediation line(s) (``vco doctor``'s ``disk_space_low``):
    VCO's one optional download and the exact switch that skips it. A
    suggestion only — the owner chose a setting, not an automatic skip."""
    from vco_lib.embedding_selection import SUMMARY_MODEL_SIZE_MB

    biggest = max(SUMMARY_MODEL_SIZE_MB.values()) / 1024
    return (
        f"# To skip VCO's local KG-summary model (up to ~{biggest:.0f} GB; summaries then\n"
        f"#   use Claude/cloud only), turn it off: {HOW_TO_TURN_OFF}.\n"
        "#   A model already downloaded stays until you remove it there.\n"
    )


def parse_switch(raw: Any) -> Optional[bool]:
    """``True``/``False`` for a recognised value, ``None`` otherwise."""
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    if value in _ON:
        return True
    if value in _OFF:
        return False
    return None


@dataclass(frozen=True)
class Setting:
    enabled: bool
    source: str
    #: The unrecognised ``VCT_LOCAL_SUMMARY_MODEL`` value that was ignored.
    invalid_env: Optional[str] = None


def _preferences_value(db_path: Optional[Path]) -> Optional[str]:
    from vco_lib.launcher_db_reader import read_app_state_value

    return read_app_state_value(APP_STATE_KEY, db_path)


def resolve(
    env: Optional[Mapping[str, str]] = None,
    *,
    db_path: Optional[Path] = None,
    preferences: Optional[Callable[[], Optional[str]]] = None,
) -> Setting:
    """The setting and where it came from: env → Preferences → ON.

    An unrecognised env value is IGNORED (said once on stderr, reported in
    ``invalid_env``) rather than read as either answer — a typo must not
    silently delete-or-download gigabytes."""
    env = os.environ if env is None else env
    raw_env = str(env.get(ENV_SWITCH, "") or "").strip()
    invalid: Optional[str] = None
    if raw_env:
        parsed = parse_switch(raw_env)
        if parsed is not None:
            return Setting(parsed, SOURCE_ENV)
        invalid = raw_env
        sys.stderr.write(
            f"[local_summary_model] {ENV_SWITCH}={raw_env!r} is not on/off — ignored\n")
    pref = parse_switch((preferences or (lambda: _preferences_value(db_path)))())
    if pref is not None:
        return Setting(pref, SOURCE_PREFERENCES, invalid)
    return Setting(True, SOURCE_DEFAULT, invalid)


def enabled(env: Optional[Mapping[str, str]] = None, *, db_path: Optional[Path] = None) -> bool:
    """Whether the local summary model is wanted on this machine."""
    return resolve(env, db_path=db_path).enabled


# ── the model and its presence ──────────────────────────────────────────────


def _install_root() -> Path:
    return Path(__file__).resolve().parent.parent


def target_model(root: Optional[Path] = None) -> tuple[str, str]:
    """``(model, source)``: the model the plan pulls when the switch is ON.

    That is the text-generation model install RECORDED (the first entry of
    the recorded profile — exactly what :func:`embedding_pull_plan.plan`
    takes), else the hardware tier from the same table (an install older
    than the record)."""
    from vco_lib.summary_model import (
        SOURCE_HARDWARE, SOURCE_INSTALL_RECORD, _hardware_model, recorded_generation_model)

    rec = recorded_generation_model([Path(root)] if root is not None else None)
    if rec:
        return rec, SOURCE_INSTALL_RECORD
    return _hardware_model(), SOURCE_HARDWARE


def approx_size_bytes(model: str) -> Optional[int]:
    """Nominal on-disk size from the tier table (``SUMMARY_MODEL_SIZE_MB``)."""
    from vco_lib.embedding_selection import SUMMARY_MODEL_SIZE_MB

    mb = SUMMARY_MODEL_SIZE_MB.get(model)
    return int(mb) * 1024 * 1024 if mb else None


def _norm(model: str) -> str:
    return model if ":" in model.rsplit("/", 1)[-1] else f"{model}:latest"


def ollama_presence(base_url: str, model: str, *,
                    get_json: Optional[Callable[[str, float], Any]] = None
                    ) -> tuple[Optional[bool], Optional[int]]:
    """``(present, size_bytes)`` from ``/api/tags``; ``(None, None)`` when
    Ollama cannot be asked (unknown is not "absent")."""
    if get_json is None:
        from vco_lib.ollama_pull import UrllibHttp

        get_json = UrllibHttp().get_json
    try:
        body = get_json(f"{base_url.rstrip('/')}/api/tags", 5.0)
    except (OSError, ValueError, urllib.error.URLError):
        return None, None
    models = body.get("models") if isinstance(body, dict) else None
    if not isinstance(models, list):
        return None, None
    want = _norm(model)
    for m in models:
        if not isinstance(m, dict):
            continue
        if any(isinstance(m.get(k), str) and _norm(m[k]) == want for k in ("name", "model")):
            size = m.get("size")
            return True, int(size) if isinstance(size, (int, float)) and size > 0 else None
    return False, None


def _ollama_url(launcher_db: Optional[Path]) -> str:
    from vco_lib import service_endpoints

    return str(service_endpoints.machine_service_urls(launcher_db)["ollama_url"]).rstrip("/")


def _owner_check(root: Path, launcher_db: Optional[Path]) -> tuple[bool, str]:
    """``(VCO owns this Ollama, why)`` — the ONE ownership predicate
    (:func:`vco_lib.ollama_pull.service_container`, as ``restart_owned_ollama``)."""
    from vco_lib import containers, ollama_pull, service_endpoints

    try:
        row = service_endpoints.load_rows(launcher_db).get("ollama")
    except Exception as exc:  # noqa: BLE001 — unreadable registry: cannot confirm
        return False, f"the service registry could not be read ({exc})"
    resolution = containers.resolve(install_root=root, probe_compose=False)
    runtime = resolution.runtime or ""
    outcome, _name, detail = ollama_pull.service_container(root, runtime, "ollama", row)
    return outcome == ollama_pull.OWNED, detail


def status(
    root: Optional[Path] = None,
    *,
    launcher_db: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    base_url: Optional[str] = None,
    get_json: Optional[Callable[[str, float], Any]] = None,
    owner: Optional[Callable[[], tuple[Optional[bool], str]]] = None,
) -> dict[str, Any]:
    """Everything the Preferences row and ``vco doctor`` show.

    ``owner`` defaults to the real ownership check (a container inspect);
    the doctor passes one answering ``(None, ...)`` so it never inspects
    containers."""
    root = Path(root) if root is not None else _install_root()
    setting = resolve(env, db_path=launcher_db)
    model, model_source = target_model(root)
    url = base_url if base_url is not None else _ollama_url(launcher_db)
    present, size = ollama_presence(url, model, get_json=get_json)
    owns, owner_detail = (owner or (lambda: _owner_check(root, launcher_db)))()
    return {
        "ok": True,
        "enabled": setting.enabled,
        "source": setting.source,
        "invalid_env": setting.invalid_env,
        "env_var": ENV_SWITCH,
        "app_state_key": APP_STATE_KEY,
        "model": model,
        "model_source": model_source,
        "present": present,
        "size_bytes": size,
        "approx_size_bytes": approx_size_bytes(model),
        "ollama_url": url,
        "vco_owns_ollama": owns,
        "owner_detail": owner_detail,
    }


# ── remove ──────────────────────────────────────────────────────────────────


def _http_delete(url: str, payload: Mapping[str, Any], timeout: float) -> int:
    req = urllib.request.Request(
        url, data=json.dumps(dict(payload)).encode(),
        headers={"Content-Type": "application/json"}, method="DELETE")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return int(getattr(resp, "status", 200))


def _plan_models(root: Path, launcher_db: Optional[Path]) -> tuple[str, ...]:
    from vco_lib.embedding_pull_plan import PlanUnavailable, plan_from_machine

    try:
        return plan_from_machine(root, launcher_db).models
    except PlanUnavailable:
        return ()


def remove(
    root: Optional[Path] = None,
    *,
    confirmed: bool,
    launcher_db: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    base_url: Optional[str] = None,
    get_json: Optional[Callable[[str, float], Any]] = None,
    delete: Optional[Callable[[str, Mapping[str, Any], float], int]] = None,
    owner: Optional[Callable[[], tuple[bool, str]]] = None,
    plan_models: Optional[Callable[[], Sequence[str]]] = None,
) -> dict[str, Any]:
    """Delete the local summary model from Ollama — guarded.

    Refused (``ok: False`` + ``reason``) unless ALL hold: the caller
    confirmed; the switch is OFF; the model is not in the machine's pull
    plan (an embedder is never touched); VCO owns this Ollama. A model
    Ollama does not hold is ``ok: True, removed: False`` (nothing to do)."""
    root = Path(root) if root is not None else _install_root()
    model, _src = target_model(root)
    out: dict[str, Any] = {"ok": False, "model": model, "removed": False,
                           "freed_bytes": None, "reason": None, "error": None}

    def refuse(reason: str, error: str) -> dict[str, Any]:
        # `code` is the field the launcher's vco_lib JSON seam reports.
        out.update(reason=reason, code=reason, error=error)
        return out

    if not confirmed:
        return refuse("not_confirmed", "removal needs an explicit confirmation (--yes)")
    setting = resolve(env, db_path=launcher_db)
    if setting.enabled:
        return refuse("still_enabled", (
            "the local summary model is still turned ON, so the next install/update would "
            f"download it again — turn it off first ({HOW_TO_TURN_OFF})"))
    planned = tuple((plan_models or (lambda: _plan_models(root, launcher_db)))())
    if _norm(model) in {_norm(m) for m in planned}:
        return refuse("in_plan",
                      f"{model} is one of the models this machine's plan still uses — not removed")
    owns, detail = (owner or (lambda: _owner_check(root, launcher_db)))()
    if not owns:
        return refuse("not_owned", (
            f"VCO does not own this Ollama ({detail}) — remove {model} with that Ollama's "
            f"own tools (`ollama rm {model}`)"))
    url = base_url if base_url is not None else _ollama_url(launcher_db)
    present, size = ollama_presence(url, model, get_json=get_json)
    if present is None:
        return refuse("ollama_unreachable", f"Ollama at {url} did not answer")
    if not present:
        out.update(ok=True, reason="absent")
        return out
    try:
        code = (delete or _http_delete)(f"{url}/api/delete", {"model": model, "name": model}, 60.0)
    except (OSError, urllib.error.URLError) as exc:
        return refuse("delete_failed", f"Ollama refused the delete of {model}: {exc}")
    if code != 200:
        return refuse("delete_failed", f"Ollama answered HTTP {code} to the delete")
    out.update(ok=True, removed=True, freed_bytes=size or approx_size_bytes(model))
    return out


# ── vco doctor ──────────────────────────────────────────────────────────────


def doctor_status() -> dict[str, Any]:
    """The doctor's reading: :func:`status` without the container inspect."""
    return status(owner=lambda: (None, "not checked by vco doctor"))


def doctor_probe(st: Optional[Mapping[str, Any]]) -> list:
    """``vco doctor``'s ``local_summary_model`` finding — informational.

    Never a problem: both answers of the switch are legitimate, a missing
    model while ON is the pull step's own deferral
    (``ollama_model_pull_failed``), and a model left on disk while OFF is
    the user's data. ``st=None`` (the status could not be read) is UNKNOWN."""
    from vco_lib.doctor import STATUS_OK, STATUS_UNKNOWN, Finding

    if st is None:
        return [Finding(probe="local_summary_model", status=STATUS_UNKNOWN,
                        summary="local summary model setting could not be read")]
    return [Finding(probe="local_summary_model", status=STATUS_OK, summary=describe(st),
                    detail=dict(st))]


# ── CLI ─────────────────────────────────────────────────────────────────────


def _gb(n: Optional[int]) -> str:
    return f"{n / 1024 ** 3:.1f} GB" if n else "size unknown"


def describe(st: Mapping[str, Any]) -> str:
    """One line — the CLI's text form and ``vco doctor``'s summary."""
    state = "ON" if st["enabled"] else "OFF"
    src = {SOURCE_ENV: f"{ENV_SWITCH}", SOURCE_PREFERENCES: "Preferences",
           SOURCE_DEFAULT: "default"}.get(st["source"], st["source"])
    size = st.get("size_bytes") or st.get("approx_size_bytes")
    if st["present"] is None:
        held = f"{st['model']}: Ollama not reachable, presence unknown"
    elif st["present"]:
        held = f"{st['model']} downloaded ({_gb(size)})"
    else:
        held = f"{st['model']} not downloaded"
    line = f"local summary model {state} ({src}) — {held}"
    if not st["enabled"] and st["present"]:
        line += ("; still on disk — remove it from launcher Preferences → KG Summaries "
                 "(VCO never deletes it on its own)")
    elif st["enabled"] and st["present"] is False:
        line += "; the next install/update downloads it"
    return line


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m vco_lib.local_summary_model",
        description="Show, or remove, the local KG-summary fallback model.")
    ap.add_argument("verb", choices=("status", "remove"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--yes", action="store_true", help="remove: confirm the deletion")
    ap.add_argument("--root", type=Path, default=None, help="install root")
    ap.add_argument("--launcher-db", type=Path, default=None)
    args = ap.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.verb == "status":
        st = status(args.root, launcher_db=args.launcher_db)
        sys.stdout.write((json.dumps(st) if args.json else describe(st)) + "\n")
        return 0
    res = remove(args.root, confirmed=args.yes, launcher_db=args.launcher_db)
    if args.json:
        sys.stdout.write(json.dumps(res) + "\n")
    elif res["ok"]:
        sys.stdout.write(f"removed {res['model']}\n" if res["removed"]
                         else f"{res['model']} is not downloaded — nothing to remove\n")
    else:
        sys.stderr.write(f"{res['error']}\n")
    return 0 if res["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
