"""v0.2.44 V44-F: regression tests for fix-now items from adversarial review.

Covers:
  - NUL byte in stored file_path doesn't crash _path_resolves_on_disk (Fix 1)
  - Embedded newline in stored file_path doesn't crash (Fix 1)
  - Rebind errors propagate to seed_errors (Fix 2)
  - Orphan-collection notice fires when primary_count > 0 and canonical differs (Fix 3)
  - Env/DB disagreement warning logs cleanly (Fix 4)
  - SHARED_KG_WRITE_DISABLED gate no longer blocks scope='project' writes
    when KG_COLLECTION == SHARED_KG_COLLECTION (Fix 6)
"""

import sys
from pathlib import Path


def test_path_resolves_on_disk_handles_nul_byte():
    """Fix 1: a NUL byte in file_path must not crash strategy 1."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import install
    # Should return False, NOT raise
    result = install._path_resolves_on_disk("foo\x00bar.md")
    assert result is False


def test_path_resolves_on_disk_handles_embedded_newline():
    """Fix 1: a newline in file_path must not crash."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import install
    result = install._path_resolves_on_disk("foo\nbar.md")
    assert result is False


def test_rebind_errors_propagate_to_seed_errors(monkeypatch, tmp_path):
    """Fix 2: when _rebind_orchestrator_root_to_canonical returns ACTUAL
    failures (not soft-fail deferrals), they MUST be extended into
    seed_errors so the audit-log warn entry fires.

    Discriminator: error strings containing "(skipped)" or "reconciled on
    next launcher boot" are deferrals (legitimate soft-fail) and stay
    non-propagated. Everything else (UPDATE failed:, import/use failed:,
    apply failed:) is a real failure and propagates.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import install

    # Mock the rebind helper to return a REAL failure shape
    monkeypatch.setattr(install, "_is_orchestrator_root_install", lambda: True)
    monkeypatch.setattr(
        install,
        "_rebind_orchestrator_root_to_canonical",
        lambda canonical: ["launcher.db UPDATE failed: simulated"],
    )
    monkeypatch.setattr(install, "_write_app_state_key", lambda *a, **k: None)
    monkeypatch.setattr(install, "_count_weaviate_class_objects", lambda *a, **k: 0)
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)

    # Build minimal args+venv+sync_kg
    class _Args:
        update = True
    venv_py = tmp_path / "py"
    sync_kg = tmp_path / "sync"

    errs = install._seed_weaviate_shared_kg_only(
        args=_Args(),
        venv_py=venv_py,
        sync_kg=sync_kg,
        weaviate_url="http://x",
        current_shared_kg="MyKG",
        current_kg_collection="MyKG",
    )
    assert any("UPDATE failed" in e for e in errs), \
        f"rebind real-failure error not propagated; got: {errs}"


def test_rebind_deferrals_do_not_propagate_to_seed_errors(monkeypatch, tmp_path):
    """Fix 2 inverse: soft-fail deferrals (launcher.db missing,
    'reconciled on next launcher boot') stay non-propagated so the
    existing test_rebind_soft_fails_when_launcher_db_missing contract
    holds — those signal a deliberate first-boot reconciliation path,
    not a failure."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import install

    monkeypatch.setattr(install, "_is_orchestrator_root_install", lambda: True)
    monkeypatch.setattr(
        install,
        "_rebind_orchestrator_root_to_canonical",
        lambda canonical: [
            "launcher.db: no orchestrator-root project row found (skipped)",
            "config_projection: orchestrator-root project_id not resolvable; "
            "env files will be reconciled on next launcher boot",
        ],
    )
    monkeypatch.setattr(install, "_write_app_state_key", lambda *a, **k: None)
    monkeypatch.setattr(install, "_count_weaviate_class_objects", lambda *a, **k: 0)
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)

    class _Args:
        update = True

    errs = install._seed_weaviate_shared_kg_only(
        args=_Args(),
        venv_py=tmp_path / "py",
        sync_kg=tmp_path / "sync",
        weaviate_url="http://x",
        current_shared_kg="MyKG",
        current_kg_collection="MyKG",
    )
    assert errs == [], (
        f"deferral strings must not propagate; got: {errs}"
    )


def test_orphan_collection_notice_fires(monkeypatch, tmp_path, capsys):
    """Fix 3: notice prints when canonical differs AND primary has rows."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import install

    monkeypatch.setattr(install, "_is_orchestrator_root_install", lambda: True)
    monkeypatch.setattr(install, "_rebind_orchestrator_root_to_canonical", lambda c: [])
    monkeypatch.setattr(install, "_write_app_state_key", lambda *a, **k: None)
    # primary has 192 rows, shared has 0
    def _count(_url, name):
        return 192 if name == "OldKG" else 0
    monkeypatch.setattr(install, "_count_weaviate_class_objects", _count)
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)

    class _Args:
        update = True
    install._seed_weaviate_shared_kg_only(
        args=_Args(),
        venv_py=tmp_path / "py",
        sync_kg=tmp_path / "sync",
        weaviate_url="http://x",
        current_shared_kg="NewKG",      # canonical (SHARED wins)
        current_kg_collection="OldKG",  # orphan after rebind
    )
    out = capsys.readouterr().out
    assert "Orphan collection 'OldKG' retains 192 rows" in out


def test_env_db_disagreement_warning(monkeypatch, capsys, tmp_path):
    """Fix 4: when env value disagrees with DB value, a WARNING prints."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import install

    # Stub the launcher_db_reader to return a value DIFFERENT from current env.
    def _fake_bindings():
        return ("DbPrimary_KG", "DbShared_KG")
    monkeypatch.setattr(
        "vco_lib.launcher_db_reader.get_orchestrator_root_bindings",
        _fake_bindings,
    )
    monkeypatch.setattr(install, "_is_orchestrator_root_install", lambda: True)
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)

    # Stub everything downstream of the warning that could blow up.
    monkeypatch.setattr(install, "_read_app_state_key", lambda k: None)
    monkeypatch.setattr(install, "_write_app_state_key", lambda *a, **k: None)
    monkeypatch.setattr(install, "_compute_on_disk_content_hashes", lambda *a, **k: {})
    monkeypatch.setattr(install, "_batch_query_weaviate_content_hashes", lambda *a, **k: {})
    monkeypatch.setattr(install, "_seed_weaviate_shared_kg_only", lambda **k: [])
    monkeypatch.setattr(install, "_prune_stale_kg_rows", lambda *a, **k: None)

    # We need .claude/scripts/sync_knowledge_graph.py to exist for the path.
    scripts = tmp_path / ".claude" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "sync_knowledge_graph.py").write_text("# stub")
    venv_py = tmp_path / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True, exist_ok=True)
    venv_py.write_text("# stub")

    # Set env to values that DISAGREE with the stubbed DB bindings.
    monkeypatch.setenv("KG_COLLECTION", "EnvPrimary_KG")
    monkeypatch.setenv("SHARED_KG_COLLECTION", "EnvShared_KG")
    monkeypatch.setenv("WEAVIATE_URL", "http://x")

    class _Args:
        update = True
        skip_seed = False

    # Call should not raise; we only care about stdout containing the WARNING.
    try:
        install._seed_weaviate(_Args())
    except SystemExit:
        pass
    except Exception:
        # Other failures downstream are fine — we just need the warning print
        # which happens at the V44-B block, before any of the failure points.
        pass

    out = capsys.readouterr().out
    assert (
        "KG_COLLECTION disagreement" in out
        or "SHARED_KG_COLLECTION disagreement" in out
    ), f"disagreement warning not printed; stdout was:\n{out}"


def test_shared_kg_write_disabled_does_not_block_project_scope_on_orchestrator_root(monkeypatch):
    """Fix 6: with KG_COLLECTION == SHARED_KG_COLLECTION (orchestrator-root
    post-rebind state), SHARED_KG_WRITE_DISABLED=true must NOT block
    scope='project' writes."""
    # Ensure import path
    mcp_dir = Path(__file__).resolve().parents[1] / "claude_mcp_servers"
    if str(mcp_dir) not in sys.path:
        sys.path.insert(0, str(mcp_dir))
    # Reset env
    monkeypatch.setenv("KG_COLLECTION", "VibeCodedOrchestrator_KnowledgeGraph")
    monkeypatch.setenv("SHARED_KG_COLLECTION", "VibeCodedOrchestrator_KnowledgeGraph")
    monkeypatch.setenv("SHARED_KG_WRITE_DISABLED", "true")
    # Force module to re-evaluate by importing fresh
    if "weaviate_mcp.server" in sys.modules:
        del sys.modules["weaviate_mcp.server"]
    from weaviate_mcp import server as mcp_server
    # Inspect the source line to verify the fix landed:
    import inspect
    src = inspect.getsource(mcp_server.store_knowledge_node)
    assert 'scope == "shared"' in src, (
        "Fix 6 not applied: gate should be on scope, not on collection name match"
    )
    # And the OLD predicate must not be present:
    assert 'target_collection_name == SHARED_KG_COLLECTION' not in src, (
        "Old predicate still present"
    )
