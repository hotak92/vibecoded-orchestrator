# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for PR-28 (Group G) — interactive storage-location prompt at
install time.

The prompt closes the silent-data-loss footgun documented in
`.claude/context/volume-binding-fix-2026-05-16.md`: previously a CLI
install on a machine with legacy ~/podman_volumes/ollama/models data
would silently spin up fresh empty named volumes.

These tests cover:
  - non-interactive flags (--quiet, --yes, piped stdin) → 'deferred' silently
  - no legacy data detected → silent 'named' default (no surprise prompt)
  - interactive choice (1) → 'bind' mode with detected paths
  - interactive choice (2) → 'named' mode without bind paths
  - interactive choice (3) / empty input / EOF → 'deferred'
  - direct ~/.vct/storage.toml fallback writes correct TOML
  - launcher binary resolution returns None when binary absent

The detection logic is tested with synthetic Foo / FooBar paths created
under tempdirs so no real host state is touched.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from install import (
    _LEGACY_VOLUME_PROBES,
    _detect_legacy_volume_paths,
    _dir_size_human,
    _prompt_storage_location,
    _resolve_launcher_binary_for_storage,
    _vct_state_dir,
    _write_storage_toml_direct,
)


def _ns(**overrides) -> argparse.Namespace:
    """Build a minimal argparse Namespace with the flags the prompt reads."""
    defaults = {"quiet": False, "yes": False}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# _prompt_storage_location — non-interactive paths
# ---------------------------------------------------------------------------


def test_prompt_quiet_returns_deferred_without_calling_detect():
    """--quiet MUST skip detection and the prompt entirely."""
    called = {"n": 0}

    def fake_detect():
        called["n"] += 1
        return {"ollama": "/foo"}

    choice = _prompt_storage_location(
        Path("/tmp"),
        _ns(quiet=True),
        stdin_isatty=True,
        detect_fn=fake_detect,
        input_fn=lambda _: pytest.fail("input_fn unexpectedly called"),
    )
    assert choice == {"mode": "deferred", "bind_paths": {}}
    assert called["n"] == 0, "--quiet must short-circuit before detection"


def test_prompt_yes_returns_deferred():
    """--yes (CI / scripted installs) MUST default to deferred silently."""
    choice = _prompt_storage_location(
        Path("/tmp"),
        _ns(yes=True),
        stdin_isatty=True,
        detect_fn=lambda: {"ollama": "/foo"},
        input_fn=lambda _: pytest.fail("input_fn unexpectedly called"),
    )
    assert choice["mode"] == "deferred"


def test_prompt_non_tty_returns_deferred():
    """Piped stdin (no TTY) MUST default to deferred — CI installs hit this."""
    choice = _prompt_storage_location(
        Path("/tmp"),
        _ns(),
        stdin_isatty=False,
        detect_fn=lambda: {"ollama": "/foo"},
        input_fn=lambda _: pytest.fail("input_fn unexpectedly called"),
    )
    assert choice["mode"] == "deferred"


def test_prompt_no_legacy_data_silently_defaults_named():
    """No detected legacy paths → silent 'named' default (no surprise prompt)."""
    choice = _prompt_storage_location(
        Path("/tmp"),
        _ns(),
        stdin_isatty=True,
        detect_fn=lambda: {},
        input_fn=lambda _: pytest.fail("input_fn unexpectedly called"),
    )
    assert choice == {"mode": "named", "bind_paths": {}}


# ---------------------------------------------------------------------------
# _prompt_storage_location — interactive paths
# ---------------------------------------------------------------------------


def test_prompt_choice_1_returns_bind_with_detected_paths(tmp_path: Path):
    """Choice '1' → bind mode carrying the detected paths."""
    foo_root = tmp_path / "podman_volumes" / "ollama" / "models"
    foo_root.mkdir(parents=True)
    (foo_root / "marker.bin").write_text("x")

    detected = {"ollama": str(foo_root)}
    choice = _prompt_storage_location(
        Path("/tmp"),
        _ns(),
        stdin_isatty=True,
        detect_fn=lambda: detected,
        input_fn=lambda _: "1",
    )
    assert choice == {"mode": "bind", "bind_paths": detected}


def test_prompt_choice_2_returns_named_without_bind_paths():
    """Choice '2' → named mode, drops bind paths (user explicitly opted out)."""
    choice = _prompt_storage_location(
        Path("/tmp"),
        _ns(),
        stdin_isatty=True,
        detect_fn=lambda: {"ollama": "/foo/bar"},
        input_fn=lambda _: "2",
    )
    assert choice == {"mode": "named", "bind_paths": {}}


def test_prompt_choice_3_returns_deferred():
    choice = _prompt_storage_location(
        Path("/tmp"),
        _ns(),
        stdin_isatty=True,
        detect_fn=lambda: {"ollama": "/foo/bar"},
        input_fn=lambda _: "3",
    )
    assert choice == {"mode": "deferred", "bind_paths": {}}


def test_prompt_empty_input_defaults_deferred():
    """Hitting Enter at the prompt → default = 3 = deferred."""
    choice = _prompt_storage_location(
        Path("/tmp"),
        _ns(),
        stdin_isatty=True,
        detect_fn=lambda: {"ollama": "/foo/bar"},
        input_fn=lambda _: "",
    )
    assert choice["mode"] == "deferred"


def test_prompt_garbage_input_defaults_deferred():
    """Unrecognised input falls through to the default branch (deferred)."""
    choice = _prompt_storage_location(
        Path("/tmp"),
        _ns(),
        stdin_isatty=True,
        detect_fn=lambda: {"ollama": "/foo/bar"},
        input_fn=lambda _: "xyzzy",
    )
    assert choice["mode"] == "deferred"


def test_prompt_eof_returns_deferred_not_exception():
    """EOF mid-prompt MUST be caught — don't abort install over a missed prompt."""

    def raise_eof(_):
        raise EOFError

    choice = _prompt_storage_location(
        Path("/tmp"),
        _ns(),
        stdin_isatty=True,
        detect_fn=lambda: {"ollama": "/foo/bar"},
        input_fn=raise_eof,
    )
    assert choice["mode"] == "deferred"


def test_prompt_keyboard_interrupt_returns_deferred():
    """Ctrl+C mid-prompt MUST be caught — don't abort install."""

    def raise_kbi(_):
        raise KeyboardInterrupt

    choice = _prompt_storage_location(
        Path("/tmp"),
        _ns(),
        stdin_isatty=True,
        detect_fn=lambda: {"ollama": "/foo/bar"},
        input_fn=raise_kbi,
    )
    assert choice["mode"] == "deferred"


# ---------------------------------------------------------------------------
# _detect_legacy_volume_paths — integration with the probe table
# ---------------------------------------------------------------------------


def test_detect_uses_user_relative_paths_not_hardcoded():
    """The probe table MUST use `~` paths, never hardcoded absolute paths.

    This is a regression guard: an earlier draft hardcoded /home/martino.
    Any contributor adding a new probe must use Path.expanduser()-style
    paths so the detection works for every user.
    """
    for service, candidates in _LEGACY_VOLUME_PROBES.items():
        for raw in candidates:
            assert raw.startswith("~/"), (
                f"probe {service}={raw!r} is not user-relative; "
                "use ~/... so the install works on every host"
            )


def _isolate_home(monkeypatch, fake_home: Path) -> None:
    """Point `~` resolution at `fake_home` for the duration of one test.

    `Path("~/x").expanduser()` consults `os.path.expanduser`, which on
    POSIX reads $HOME and on Windows reads %USERPROFILE%. Patching
    `Path.home` is NOT enough; we override both env vars so detection
    sees a synthetic home with controlled contents.
    """
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setattr(Path, "home", lambda: fake_home)


def test_detect_skips_empty_directories(tmp_path: Path, monkeypatch):
    """Empty leftover dirs from a prior install MUST NOT trigger the prompt."""
    home = tmp_path / "fakehome"
    empty_dir = home / "podman_volumes" / "ollama" / "models"
    empty_dir.mkdir(parents=True)
    _isolate_home(monkeypatch, home)
    # No files inside → detection should NOT include ollama.
    detected = _detect_legacy_volume_paths()
    assert "ollama" not in detected, (
        "empty directory falsely triggered legacy-volume detection"
    )


def test_detect_finds_populated_legacy_path(tmp_path: Path, monkeypatch):
    """A populated ~/podman_volumes/ollama/models MUST be detected."""
    home = tmp_path / "fakehome"
    models_dir = home / "podman_volumes" / "ollama" / "models"
    models_dir.mkdir(parents=True)
    (models_dir / "qwen3.bin").write_text("fake-model-data")
    _isolate_home(monkeypatch, home)
    detected = _detect_legacy_volume_paths()
    assert detected.get("ollama") == str(models_dir)


def test_detect_returns_empty_when_no_legacy_paths(tmp_path: Path, monkeypatch):
    """A clean host (no legacy volumes anywhere) returns {} — the silent default."""
    home = tmp_path / "cleanhome"
    home.mkdir()
    _isolate_home(monkeypatch, home)
    detected = _detect_legacy_volume_paths()
    assert detected == {}


# ---------------------------------------------------------------------------
# _dir_size_human
# ---------------------------------------------------------------------------


def test_dir_size_human_returns_human_label(tmp_path: Path):
    target = tmp_path / "foo"
    target.mkdir()
    (target / "a.bin").write_bytes(b"x" * 2048)
    label = _dir_size_human(str(target))
    # Should be a "X Y" form, with KB/MB/GB suffix.
    assert any(label.endswith(suffix) for suffix in (" B", " KB", " MB", " GB", " TB"))


def test_dir_size_human_handles_missing_directory():
    label = _dir_size_human("/nonexistent/foobar/acme")
    # Soft-fail path: returns a string, never raises.
    assert isinstance(label, str)


def test_dir_size_human_handles_empty_directory(tmp_path: Path):
    label = _dir_size_human(str(tmp_path))
    assert label == "(empty)"


# ---------------------------------------------------------------------------
# _write_storage_toml_direct (Python fallback)
# ---------------------------------------------------------------------------


def test_write_storage_toml_named_mode(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct-state"))
    target = _write_storage_toml_direct(
        {"mode": "named", "bind_paths": {}}
    )
    assert target.exists()
    body = target.read_text(encoding="utf-8")
    assert 'mode = "named"' in body
    assert "[per_service_paths]" in body
    assert "[external_aliases]" in body


def test_write_storage_toml_bind_mode_persists_paths(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct-state"))
    target = _write_storage_toml_direct(
        {
            "mode": "bind",
            "bind_paths": {
                "ollama": "/foo/bar/ollama",
                "weaviate": "/foo/bar/weaviate",
            },
        }
    )
    body = target.read_text(encoding="utf-8")
    assert 'mode = "bind"' in body
    assert 'ollama = "/foo/bar/ollama"' in body
    assert 'weaviate = "/foo/bar/weaviate"' in body
    # Keys must be sorted (stable output matches the Rust BTreeMap renderer).
    ollama_idx = body.index('ollama =')
    weaviate_idx = body.index('weaviate =')
    assert ollama_idx < weaviate_idx, "keys not alphabetically sorted"


def test_write_storage_toml_atomic_rename(tmp_path: Path, monkeypatch):
    """Writes via .tmp + rename — no .tmp file left behind on success."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct-state"))
    target = _write_storage_toml_direct(
        {"mode": "named", "bind_paths": {}}
    )
    tmp_artifact = target.with_suffix(".toml.tmp")
    assert target.exists()
    assert not tmp_artifact.exists(), "atomic write left a .tmp file behind"


def test_write_storage_toml_windows_path_normalized(tmp_path: Path, monkeypatch):
    """Backslashes in bind paths get normalized to forward slashes."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct-state"))
    target = _write_storage_toml_direct(
        {
            "mode": "bind",
            "bind_paths": {"ollama": r"C:\foo\bar\ollama"},
        }
    )
    body = target.read_text(encoding="utf-8")
    assert 'ollama = "C:/foo/bar/ollama"' in body
    assert "\\" not in body


# ---------------------------------------------------------------------------
# _vct_state_dir
# ---------------------------------------------------------------------------


def test_vct_state_dir_respects_env(monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", "/tmp/foo-vct")
    assert _vct_state_dir() == Path("/tmp/foo-vct")


def test_vct_state_dir_empty_env_falls_back_to_home(monkeypatch):
    """Empty env var must fall through to ~/.vct (mirrors the Rust resolver)."""
    monkeypatch.setenv("VCT_STATE_DIR", "")
    resolved = _vct_state_dir()
    assert resolved.name == ".vct"


def test_vct_state_dir_unset_env_defaults_to_dot_vct(monkeypatch):
    monkeypatch.delenv("VCT_STATE_DIR", raising=False)
    resolved = _vct_state_dir()
    assert resolved.name == ".vct"


# ---------------------------------------------------------------------------
# _resolve_launcher_binary_for_storage
# ---------------------------------------------------------------------------


def test_resolve_launcher_binary_returns_none_when_absent(tmp_path: Path):
    """Fresh source tree without launcher/dist/ → None (fallback path used)."""
    assert _resolve_launcher_binary_for_storage(tmp_path) is None


def test_resolve_launcher_binary_finds_existing_binary(tmp_path: Path, monkeypatch):
    """When the bundled binary exists on the host arch, it's returned."""
    import platform as _plat

    os_name = _plat.system()
    if os_name == "Windows":
        arch_dir = "windows-x64"
        bin_name = "vct-launcher.exe"
    elif os_name == "Darwin":
        arch_dir = "experimental_macOS"
        bin_name = "vct-launcher"
    elif os_name == "Linux":
        arch_dir = "linux-x64"
        bin_name = "vct-launcher"
    else:
        pytest.skip(f"unsupported OS for this test: {os_name}")

    target = tmp_path / "launcher" / "dist" / arch_dir / bin_name
    target.parent.mkdir(parents=True)
    target.write_text("not really a binary")
    if os_name != "Windows":
        target.chmod(0o755)

    resolved = _resolve_launcher_binary_for_storage(tmp_path)
    assert resolved == target


# ---------------------------------------------------------------------------
# End-to-end: a full no-data install run never prompts the user
# ---------------------------------------------------------------------------


def test_e2e_clean_host_silent_named_default(tmp_path: Path, monkeypatch):
    """Smoke test the entire prompt flow on a clean host (no legacy data).

    Expectation: no prompt rendered, no input_fn call, result == 'named'.
    """
    home = tmp_path / "cleanhome"
    home.mkdir()
    _isolate_home(monkeypatch, home)

    choice = _prompt_storage_location(
        Path("/tmp"),
        _ns(),
        stdin_isatty=True,
        input_fn=lambda _: pytest.fail(
            "input_fn called on a clean host — prompt should be silent"
        ),
    )
    assert choice == {"mode": "named", "bind_paths": {}}
