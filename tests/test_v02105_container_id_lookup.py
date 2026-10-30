# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 — the ONE runtime-neutral container-id lookup
(``vco_lib.containers.inspect_container_entry`` / ``container_id``).

Pre-fix, the lookups shelled Go-template forms: plain ``{{.Id}}`` — which
WORKS on podman 4.9 too (verified live) — and the ``{{json .Id}}`` form
``service_adoption._plan_all`` used, which podman 4.9 refuses with exit
125 (``can't evaluate field Id in type interface {}``) while Docker
accepts it. That one form was the whole pre-fix bug (field 2026-10-23:
``hand-to-vco --service ollama`` refused a running ``vco_ollama``). The
fix inspects WITHOUT a template everywhere — the JSON array both runtimes
print carries the id under the same ``"Id"`` key — so no template-field
spelling can differ between the engines again.
"""

import json
import shutil
import subprocess
import types
from typing import Any

import pytest

from vco_lib import containers as c

CID = "7cc5e278b7e4f434c2be605462cee7e73033c0950d4167459ad125da3c3ff1eb"


def _cp(argv, rc=0, out="", err=""):
    return types.SimpleNamespace(argv=list(argv), returncode=rc,
                                 stdout=out, stderr=err)


# ─── the helper itself ───────────────────────────────────────────────────


@pytest.mark.parametrize("payload", [
    # docker's container-inspect JSON and podman's are the SAME shape for
    # this question: an array whose object carries "Id". (Only the
    # Go-template FIELD spelling differed, never the JSON key.)
    json.dumps([{"Id": CID, "Name": "/vco_ollama"}]),
])
def test_template_free_inspect_returns_the_id(payload):
    seen = {}

    def run(argv, **kw):
        seen["argv"] = list(argv)
        return _cp(argv, 0, payload)

    assert c.container_id("vco_ollama", "podman", run=run) == CID
    # NO Go template anywhere in the probe — no template-field spelling
    # can differ between the engines again.
    assert seen["argv"] == ["podman", "inspect", "--type", "container",
                            "vco_ollama"]


def test_a_missing_container_is_none_and_positively_not_found():
    def run(argv, **kw):
        return _cp(argv, 125, "", "Error: no such container vco_ollama")

    assert c.container_id("vco_ollama", "podman", run=run) is None
    _entry, verdict, _detail = c.inspect_container_entry("vco_ollama", "podman", run=run)
    assert verdict == "not_found"


def test_a_probe_error_is_none_and_stays_error_never_not_found():
    """The tri-state: a daemon that cannot be asked is NOT "absent" — the
    data-identity callers must keep ABSENT and UNKNOWN apart."""

    def run(argv, **kw):
        return _cp(argv, 1, "", "Error: cannot connect to the podman socket")

    assert c.container_id("vco_ollama", "podman", run=run) is None
    _entry, verdict, detail = c.inspect_container_entry("vco_ollama", "podman", run=run)
    assert verdict == "error"
    # v0.2.105 review LOW: the diagnostic tail (daemon down, permission
    # denied) rides along, so UNKNOWN surfaces need not say just "error".
    assert "cannot connect to the podman socket" in detail


def test_exit_zero_with_an_empty_payload_reads_as_not_found():
    """Every pre-v0.2.105 call site treated rc-0-empty as not-found; that
    reading is kept (never "error" — it would poison tri-state callers)."""

    def run(argv, **kw):
        return _cp(argv, 0, "", "")

    _entry, verdict, _detail = c.inspect_container_entry("vco_ollama", "podman", run=run)
    assert verdict == "not_found"


def test_unparseable_payload_is_an_error():
    def run(argv, **kw):
        return _cp(argv, 0, "not json", "")

    _entry, verdict, _detail = c.inspect_container_entry("vco_ollama", "podman", run=run)
    assert verdict == "error"


# ─── the CLI the hooks call (A of the A>B>C rule) ────────────────────────


def test_cli_prints_the_id_and_exits_zero(monkeypatch):
    monkeypatch.setattr(
        c, "container_id",
        lambda name, runtime, *, run=None: CID if name == "vco_ollama" else None)
    import io
    import contextlib

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert c.main(["container-id", "vco_ollama", "--runtime", "podman"]) == 0
    assert out.getvalue().strip() == CID


def test_cli_prints_nothing_and_exits_one_when_absent(monkeypatch):
    monkeypatch.setattr(c, "container_id",
                        lambda name, runtime, *, run=None: None)
    import io
    import contextlib

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert c.main(["container-id", "gone", "--runtime", "podman"]) == 1
    assert out.getvalue() == ""


# ─── the migrated Python sites, against a PODMAN-shaped machine ──────────


class _Podman49:
    """A fake machine that behaves like podman 4.9 (verified live on
    4.9.3): the template-free inspect answers with the JSON array, plain
    ``{{.Id}}`` WORKS (prints the id, exit 0), and the ``{{json .Id}}``
    form is the ONE template podman refuses (exit 125, ``can't evaluate
    field Id in type interface {}``)."""

    def run(self, argv: list, **_kw) -> Any:
        argv = [str(a) for a in argv]
        if argv[1] == "inspect" and "--format" in argv:
            fmt = argv[argv.index("--format") + 1]
            if fmt == "{{json .Id}}":
                return _cp(argv, 125, "",
                           "Error: template: inspect:1:18: executing "
                           '"inspect" at <.Id>: can\'t evaluate '
                           "field Id in type interface {}")
            if fmt == "{{.Id}}":
                return _cp(argv, 0, CID)
        if argv[1] == "inspect":
            return _cp(argv, 0, json.dumps([{"Id": CID}]))
        return _cp(argv)


def test_podman_49_shape_plain_template_works_json_form_fails():
    """The corrected premise, pinned: podman 4.9 ACCEPTS plain ``{{.Id}}``
    (exit 0, the id) and refuses only ``{{json .Id}}`` (exit 125) — the
    pre-v0.2.105 bug was that one form (service_adoption), not every
    ``.Id`` template. The fake above must keep modelling this shape."""
    m = _Podman49()
    plain = m.run(["podman", "inspect", "--type", "container",
                   "--format", "{{.Id}}", "vco_ollama"])
    assert plain.returncode == 0
    assert plain.stdout.strip() == CID
    jsoned = m.run(["podman", "inspect", "--type", "container",
                    "--format", "{{json .Id}}", "vco_ollama"])
    assert jsoned.returncode == 125
    assert "can't evaluate field Id" in jsoned.stderr


def test_hand_to_vco_container_ref_lookup_survives_podman(tmp_path,
                                                          monkeypatch):
    """RED-PROOF TARGET: reverting ``service_adoption._plan_all`` to the
    ``{{json .Id}}`` template call turns this red — the podman-shaped fake
    answers it with exit 125 and the plan refuses the container."""
    from vco_lib import service_adoption as sa

    monkeypatch.setattr(sa._containers, "find_existing_container",
                        lambda *_a, **_k: None)
    (tmp_path / "infrastructure").mkdir()
    (tmp_path / "infrastructure" / "docker-compose.yml").write_text(
        "services: {}\n")
    machine = _Podman49()
    plans, _files = sa._plan_all(
        tmp_path, "podman", machine.run, lambda _m: None,
        services=("weaviate",), container_refs={"weaviate": "vco_weaviate"})
    assert plans[0].container == "vco_weaviate", plans[0].reason
    assert "does not exist" not in (plans[0].reason or "")


# ─── the live podman check (read-only) ───────────────────────────────────


def test_live_podman_returns_the_running_vco_ollama_id():
    """Read-only live probe against this machine's runtime: the helper must
    resolve the RUNNING ``vco_ollama`` container's id — the same question
    the one podman-broken template form (``{{json .Id}}``, service_adoption)
    used to answer with exit 125."""
    if shutil.which("podman") is None:
        pytest.skip("no podman on this machine")
    live = subprocess.run(
        ["podman", "inspect", "--type", "container", "vco_ollama"],
        capture_output=True, text=True, timeout=15)
    if live.returncode != 0:
        pytest.skip(f"no vco_ollama container here: "
                    f"{(live.stderr or '').strip()[:120]}")
    cid = c.container_id("vco_ollama", "podman")
    assert cid is not None, "the running vco_ollama resolved to no id"
    assert len(cid) == 64 and all(ch in "0123456789abcdef" for ch in cid)
