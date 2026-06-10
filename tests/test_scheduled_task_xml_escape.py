# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# tests/test_scheduled_task_xml_escape.py — W-P1-5 (v0.2.53 Track H).
#
# Regression test for the Scheduled Task XML USERDOMAIN/USERNAME injection
# vector found in the 2026-06-10 Windows audit. Pre-fix, install.py read
# USERDOMAIN + USERNAME directly from the environment and substituted them
# raw into the Task XML template. If either contained `&`, `<` or `>`
# (rare but legal in legacy WORKGROUP names like "ACME&CO"), the rendered
# XML was malformed and `schtasks /Create /XML` exited non-zero.
#
# Test strategy: import install.py as a module and monkey-patch
# `os.environ` to inject XML-hostile values. Render the template by
# invoking `_materialize_boot_service_windows` against a tmp install root
# and parse the resulting XML with `xml.etree.ElementTree`. If the parse
# succeeds and the <UserId> element contains the *literal* `&` (not the
# `&amp;` entity, which is what XML stores), the fix is good. We also
# assert the unescaped raw characters DO NOT appear in the file as bytes
# (because `&` raw would make the XML invalid).
#
# Cross-OS: runs on all platforms. The function is called regardless of
# OS (the OS gate lives in the caller `_materialize_boot_service`). We
# inject env vars + a template, no schtasks.exe required.

from __future__ import annotations

import importlib.util
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


# ── Test fixture helpers ────────────────────────────────────────────────


def _load_install_module():
    """Load install.py as a module for direct function access.

    install.py lives at the repo root and is not a package; we load it
    via importlib so the tests don't depend on a pip install.
    """
    repo_root = Path(__file__).resolve().parent.parent
    install_path = repo_root / "install.py"
    spec = importlib.util.spec_from_file_location("install_under_test", install_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # install.py expects to find vco_lib on sys.path; the repo root suffices.
    sys.path.insert(0, str(repo_root))
    try:
        spec.loader.exec_module(mod)
    finally:
        # Don't pop the path — the module remains importable for the test
        # session. Pop only if we inserted and it's still at the front.
        if sys.path and sys.path[0] == str(repo_root):
            sys.path.pop(0)
    return mod


@pytest.fixture(scope="module")
def install_mod():
    return _load_install_module()


@pytest.fixture
def fake_install_root(tmp_path, install_mod, monkeypatch):
    """Set up a minimal install root with the Task XML template + state dir."""
    repo_root = Path(install_mod.__file__).resolve().parent
    src_template = repo_root / "templates" / "windows" / "claude-mcp-containers.task.xml.template"
    if not src_template.exists():
        pytest.skip("Task XML template not present in this checkout")
    # Mirror the template under tmp_path/templates/
    dest_templates = tmp_path / "templates" / "windows"
    dest_templates.mkdir(parents=True, exist_ok=True)
    dest_template = dest_templates / "claude-mcp-containers.task.xml.template"
    dest_template.write_text(src_template.read_text(encoding="utf-8"), encoding="utf-8")

    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "scripts").mkdir(exist_ok=True)
    # Ship a stub launch-claude-mcp-stack.ps1 so the wrapper-discovery
    # branch picks it up.
    (tmp_path / "scripts" / "launch-claude-mcp-stack.ps1").write_text(
        "# stub for tests", encoding="utf-8",
    )

    # Pre-fix code used the module-level `_read_template` which resolves
    # template paths relative to the install.py location. Monkey-patch it
    # to read from our tmp_path mirror instead.
    real_read_template = install_mod._read_template

    def _patched_read_template(rel_path):
        candidate = tmp_path / rel_path
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
        return real_read_template(rel_path)

    monkeypatch.setattr(install_mod, "_read_template", _patched_read_template)
    return tmp_path


# ── Tests ───────────────────────────────────────────────────────────────


def test_userdomain_ampersand_is_escaped(install_mod, fake_install_root, monkeypatch):
    """USERDOMAIN containing `&` must produce parseable XML."""
    monkeypatch.setenv("USERDOMAIN", "ACME&CO")
    monkeypatch.setenv("USERNAME", "alice")
    # USER must NOT short-circuit the fallback (only triggers when both
    # USERDOMAIN + USERNAME are empty).
    monkeypatch.delenv("USER", raising=False)

    install_mod._materialize_boot_service_windows(
        install_path=fake_install_root,
        working_dir=fake_install_root,
    )

    rendered_path = fake_install_root / "state" / "installed_boot_task.xml"
    assert rendered_path.exists(), "Task XML was not materialized"
    rendered = rendered_path.read_text(encoding="utf-8")

    # If the fix is in place, the rendered XML contains `&amp;` (the XML
    # entity) and `xml.etree` can parse it. Pre-fix, the file contained
    # bare `&CO` and ET.fromstring raised ParseError.
    try:
        root = ET.fromstring(rendered)
    except ET.ParseError as exc:
        pytest.fail(f"Rendered XML is malformed (USERDOMAIN escape missing): {exc}\n---\n{rendered}")

    # Find the <UserId> element and assert it contains the LOGICAL value
    # `ACME&CO\alice` (ET decodes `&amp;` back to `&` on access).
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    found = root.find(".//t:UserId", ns)
    if found is None:
        found = root.find(".//UserId")
    assert found is not None, "<UserId> element not found in rendered XML"
    assert found.text == "ACME&CO\\alice", f"unexpected UserId text: {found.text!r}"


def test_username_ltgt_is_escaped(install_mod, fake_install_root, monkeypatch):
    """USERNAME containing `<` / `>` (extremely rare but defensive) must escape."""
    monkeypatch.setenv("USERDOMAIN", "DOMAIN")
    monkeypatch.setenv("USERNAME", "bob<test>")
    monkeypatch.delenv("USER", raising=False)

    install_mod._materialize_boot_service_windows(
        install_path=fake_install_root,
        working_dir=fake_install_root,
    )

    rendered_path = fake_install_root / "state" / "installed_boot_task.xml"
    rendered = rendered_path.read_text(encoding="utf-8")
    # Must contain entity-form `&lt;` / `&gt;` (the XML stores entities).
    assert "&lt;" in rendered, "USERNAME `<` not escaped to `&lt;`"
    assert "&gt;" in rendered, "USERNAME `>` not escaped to `&gt;`"
    # Bare `<test>` must not appear anywhere in the user_id portion.
    # (It WILL appear elsewhere as XML element delimiters; we narrow by
    # finding the UserId element via parser.)
    root = ET.fromstring(rendered)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    found = root.find(".//t:UserId", ns)
    if found is None:
        found = root.find(".//UserId")
    assert found is not None
    assert found.text == "DOMAIN\\bob<test>"


def test_no_userdomain_falls_back_to_user(install_mod, fake_install_root, monkeypatch):
    """When neither USERDOMAIN nor USERNAME is set, USER fallback fires and is also escaped."""
    monkeypatch.delenv("USERDOMAIN", raising=False)
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.setenv("USER", "carol&dave")

    install_mod._materialize_boot_service_windows(
        install_path=fake_install_root,
        working_dir=fake_install_root,
    )

    rendered = (fake_install_root / "state" / "installed_boot_task.xml").read_text(
        encoding="utf-8",
    )
    # Parse must succeed.
    root = ET.fromstring(rendered)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    found = root.find(".//t:UserId", ns)
    if found is None:
        found = root.find(".//UserId")
    assert found is not None
    assert found.text == "carol&dave"


def test_plain_userdomain_unchanged(install_mod, fake_install_root, monkeypatch):
    """Sanity: normal env values render unchanged (no regression for the common path)."""
    monkeypatch.setenv("USERDOMAIN", "WORKGROUP")
    monkeypatch.setenv("USERNAME", "martino")
    monkeypatch.delenv("USER", raising=False)

    install_mod._materialize_boot_service_windows(
        install_path=fake_install_root,
        working_dir=fake_install_root,
    )

    rendered = (fake_install_root / "state" / "installed_boot_task.xml").read_text(
        encoding="utf-8",
    )
    root = ET.fromstring(rendered)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    found = root.find(".//t:UserId", ns)
    if found is None:
        found = root.find(".//UserId")
    assert found is not None
    assert found.text == "WORKGROUP\\martino"
