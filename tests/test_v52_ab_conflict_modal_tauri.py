# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-A/B regression tests for the orchestrator-update conflict modal.

Covers the two halves of the feature:

* **V52-A (modal contract)** — the legacy "Resolve manually (close this
  dialog)" button is REMOVED. Escape / X / backdrop dismiss routes through
  a confirmation that maps to Abort. Hard guarantee: install.py runs unless
  the user explicitly aborts.
* **V52-B (one-click buttons)** — two new Tauri commands
  (`keep_local_and_continue_update` / `accept_upstream_and_continue_update`)
  plus matching Svelte buttons with user-locked tooltip text. The buttons
  call into the existing v0.2.51 `resume_orchestrator_update` machinery
  after running `git checkout --ours / --theirs` + commit/continue.

Why a Python smoke test for Rust + Svelte content? Cross-language drift.
The Rust unit tests in installer.rs cover the Tauri command logic; the
Svelte/Vite check covers the modal markup. But neither catches the
*lockstep* requirement: the button labels in the modal must match the
exact command names registered in Rust; the tooltip text must match the
user-locked wording (backlog §V52-B). A grep-based Python test pins the
drift surface and runs in <100 ms — much cheaper than a Cargo +
Playwright pair.

Mirrors the style of `tests/test_managed_paths_consistency.py`: parse
both source-of-truth files independently, assert each contains the
expected anchor strings + cross-references.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER_RS = REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "installer.rs"
LIB_RS = REPO_ROOT / "launcher" / "src-tauri" / "src" / "lib.rs"
MODAL_SVELTE = (
    REPO_ROOT
    / "launcher"
    / "src"
    / "lib"
    / "components"
    / "OrchestratorUpdateConflictModal.svelte"
)

# Exact tooltip strings from the backlog (§V52-B, user-locked 2026-06-09).
# These are the affordance the user picked for the two destructive
# choices; paraphrasing them breaks the user's intent.
KEEP_LOCAL_TOOLTIP = (
    "Discards upstream changes for the conflicting files; keeps everything "
    "you've added locally. Good for: nodes you've heavily customized."
)
ACCEPT_UPSTREAM_TOOLTIP = (
    "Discards your local changes for the conflicting files; takes the public "
    "release version. Good for: KG nodes you didn't really need."
)


class RustTauriCommandsTests(unittest.TestCase):
    """Assert the two new Tauri commands exist and are wired up."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.installer = INSTALLER_RS.read_text(encoding="utf-8")
        cls.lib = LIB_RS.read_text(encoding="utf-8")

    def test_keep_local_command_exists(self) -> None:
        """`keep_local_and_continue_update` is declared as a Tauri command."""
        # The function must be `pub async fn` AND carry a `#[command]`
        # attribute (otherwise it isn't a Tauri-callable surface).
        self.assertIn(
            "pub async fn keep_local_and_continue_update",
            self.installer,
            "missing Tauri command keep_local_and_continue_update in installer.rs",
        )
        # The `#[command]` attribute must precede the function. Search for
        # the function name preceded (within ~200 chars) by `#[command]`.
        snippet = self._function_definition_snippet(
            self.installer, "pub async fn keep_local_and_continue_update"
        )
        self.assertIn(
            "#[command]",
            snippet,
            "keep_local_and_continue_update must be annotated `#[command]`",
        )

    def test_accept_upstream_command_exists(self) -> None:
        """`accept_upstream_and_continue_update` is declared as a Tauri command."""
        self.assertIn(
            "pub async fn accept_upstream_and_continue_update",
            self.installer,
            "missing Tauri command accept_upstream_and_continue_update in installer.rs",
        )
        snippet = self._function_definition_snippet(
            self.installer, "pub async fn accept_upstream_and_continue_update"
        )
        self.assertIn(
            "#[command]",
            snippet,
            "accept_upstream_and_continue_update must be annotated `#[command]`",
        )

    def test_both_commands_registered_in_lib_rs(self) -> None:
        """`lib.rs` registers both commands in the `invoke_handler!` list."""
        self.assertIn(
            "commands::installer::keep_local_and_continue_update",
            self.lib,
            "lib.rs must register keep_local_and_continue_update on the invoke_handler",
        )
        self.assertIn(
            "commands::installer::accept_upstream_and_continue_update",
            self.lib,
            "lib.rs must register accept_upstream_and_continue_update on the invoke_handler",
        )

    def test_commands_delegate_to_resume_orchestrator_update(self) -> None:
        """V52-A hard guarantee — both commands must call into the existing
        v0.2.51 `resume_orchestrator_update` machinery so install.py is
        guaranteed to run after a successful resolution."""
        # Find the shared helper `resolve_conflict_and_resume`. The two
        # public Tauri commands MUST call it; otherwise they could skip
        # the resume tail (which is what we're guarding against).
        self.assertIn(
            "fn resolve_conflict_and_resume",
            self.installer,
            "missing shared helper resolve_conflict_and_resume",
        )
        self.assertIn(
            "resume_orchestrator_update(app, path, window).await",
            self.installer,
            "resolve_conflict_and_resume must delegate to resume_orchestrator_update",
        )

        # Both public commands must call resolve_conflict_and_resume.
        keep_local_body = self._function_body(
            self.installer, "pub async fn keep_local_and_continue_update"
        )
        self.assertIn(
            "resolve_conflict_and_resume",
            keep_local_body,
            "keep_local_and_continue_update must call resolve_conflict_and_resume",
        )
        self.assertIn(
            "ConflictResolutionSide::KeepLocal",
            keep_local_body,
            "keep_local_and_continue_update must pass KeepLocal side enum",
        )

        accept_upstream_body = self._function_body(
            self.installer, "pub async fn accept_upstream_and_continue_update"
        )
        self.assertIn(
            "resolve_conflict_and_resume",
            accept_upstream_body,
            "accept_upstream_and_continue_update must call resolve_conflict_and_resume",
        )
        self.assertIn(
            "ConflictResolutionSide::AcceptUpstream",
            accept_upstream_body,
            "accept_upstream_and_continue_update must pass AcceptUpstream side enum",
        )

    def test_checkout_flag_orientation_swap_documented(self) -> None:
        """The merge-vs-rebase orientation swap in `git checkout --ours`/
        `--theirs` is subtle (rebase reverses the sides). The helper
        `resolve_checkout_flag` must exist AND map all 4 cases.
        """
        self.assertIn(
            "fn resolve_checkout_flag",
            self.installer,
            "missing resolve_checkout_flag helper",
        )
        # The four arms of the match (side, is_rebase) → flag.
        for arm in (
            "(ConflictResolutionSide::KeepLocal, false) => \"--ours\"",
            "(ConflictResolutionSide::KeepLocal, true) => \"--theirs\"",
            "(ConflictResolutionSide::AcceptUpstream, false) => \"--theirs\"",
            "(ConflictResolutionSide::AcceptUpstream, true) => \"--ours\"",
        ):
            self.assertIn(
                arm,
                self.installer,
                f"resolve_checkout_flag missing case: {arm}",
            )

    def test_commands_use_git_silent_extension(self) -> None:
        """Per the launcher's CommandExt pattern, every git subprocess must
        be `.silent()` — the helper this exposes hides the Windows console
        window. The two new commands' resolution body invokes git via
        the helper; confirm the body calls it for each git subcommand."""
        helper_body = self._function_body(self.installer, "fn resolve_conflict_and_resume")
        # We do 3 distinct git invocations: checkout, add, commit/rebase-continue.
        # All three must go through .silent().
        for verb in ("checkout", "add", "commit", "rebase"):
            # Loose check: the verb appears in some git args list AND the
            # surrounding code uses .silent(). We don't pin the exact
            # syntax — just confirm both are present in the same body.
            self.assertIn(
                verb,
                helper_body,
                f"resolve_conflict_and_resume must invoke `git {verb}`",
            )
        # Count silent() occurrences as a sanity check (~6 expected: 3 git
        # subprocess calls + extra audit/state probes; ≥3 is the floor).
        silent_count = helper_body.count(".silent()")
        self.assertGreaterEqual(
            silent_count,
            3,
            "resolve_conflict_and_resume should pipe every git subprocess through "
            f".silent() (found {silent_count}, expected ≥3)",
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _function_definition_snippet(text: str, signature: str) -> str:
        """Return the ~200 chars preceding a function signature, so we can
        check for adjacent attributes like `#[command]`."""
        idx = text.find(signature)
        if idx < 0:
            return ""
        start = max(0, idx - 200)
        return text[start:idx]

    @staticmethod
    def _function_body(text: str, signature: str) -> str:
        """Return the body of a function starting at `signature`. Naive
        brace-counting that's good enough for production-shape Rust
        functions (no string literals containing unmatched braces here)."""
        idx = text.find(signature)
        if idx < 0:
            return ""
        # Find the opening brace.
        brace_start = text.find("{", idx)
        if brace_start < 0:
            return ""
        depth = 0
        i = brace_start
        while i < len(text):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[brace_start : i + 1]
            i += 1
        return text[brace_start:]


class SvelteModalTests(unittest.TestCase):
    """Assert the modal exposes the V52-A/B affordances + removes the
    legacy silent-dismiss path."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.modal = MODAL_SVELTE.read_text(encoding="utf-8")

    def test_keep_local_button_present(self) -> None:
        """The modal renders a 'Keep local versions' button that calls
        `keep_local_and_continue_update`."""
        self.assertIn(
            "Keep local versions",
            self.modal,
            "missing 'Keep local versions' button label",
        )
        # The button must wire onclick to a handler that invokes the
        # matching Tauri command.
        self.assertIn(
            "keep_local_and_continue_update",
            self.modal,
            "modal must invoke 'keep_local_and_continue_update' Tauri command",
        )

    def test_accept_upstream_button_present(self) -> None:
        """The modal renders an 'Accept upstream versions' button that
        calls `accept_upstream_and_continue_update`."""
        self.assertIn(
            "Accept upstream versions",
            self.modal,
            "missing 'Accept upstream versions' button label",
        )
        self.assertIn(
            "accept_upstream_and_continue_update",
            self.modal,
            "modal must invoke 'accept_upstream_and_continue_update' Tauri command",
        )

    def test_user_locked_tooltip_text(self) -> None:
        """The two new buttons carry the user-locked tooltip text verbatim
        (backlog §V52-B 2026-06-09). Paraphrasing is forbidden — the
        wording is the user's chosen affordance for the destructive choice
        between local-vs-upstream."""
        self.assertIn(
            KEEP_LOCAL_TOOLTIP,
            self.modal,
            "Keep local tooltip text does not match user-locked spec",
        )
        self.assertIn(
            ACCEPT_UPSTREAM_TOOLTIP,
            self.modal,
            "Accept upstream tooltip text does not match user-locked spec",
        )

    def test_legacy_resolve_manually_button_removed(self) -> None:
        """V52-A: the 'Resolve manually (close this dialog)' button is
        REMOVED. It was the silent-dismiss path that left half-applied
        updates on disk (root cause of v0.2.51 ship-day pain)."""
        # The literal button-label string should not appear anywhere in
        # the modal markup OR in a state branch label.
        self.assertNotIn(
            "Resolve manually (close this dialog)",
            self.modal,
            "V52-A: legacy 'Resolve manually (close this dialog)' button must be removed",
        )
        self.assertNotIn(
            "Resolve manually then click Continue Update",
            self.modal,
            "V52-A: legacy 'Resolve manually then click Continue Update' label must be removed",
        )

    def test_smart_default_kg_detection(self) -> None:
        """V52-B smart default: when ALL conflicted files are under
        `knowledge/`, the modal auto-recommends Keep local."""
        # Reactive boolean exists.
        self.assertIn(
            "allConflictsAreKgNodes",
            self.modal,
            "missing reactive `allConflictsAreKgNodes` for smart-default detection",
        )
        # Detection logic includes both top-level and nested layouts.
        self.assertIn(
            "knowledge/",
            self.modal,
            "smart-default detection must check for `knowledge/` prefix",
        )
        # Smart-default notice block exists.
        self.assertIn(
            "cfl-smart-default",
            self.modal,
            "missing .cfl-smart-default UI surface for the recommendation notice",
        )
        # Recommended-button highlight class exists and is conditional on
        # the smart-default boolean.
        self.assertIn(
            "cfl-btn-recommended",
            self.modal,
            "missing .cfl-btn-recommended class to highlight Keep local",
        )

    def test_dismiss_routes_to_confirm_or_abort(self) -> None:
        """V52-A: closing via Escape / X / backdrop must NOT silently
        dismiss. The `dismiss()` function should require a two-step
        confirmation and route to `abort()` rather than `onClose()`."""
        # The confirmation state is wired in.
        self.assertIn(
            "confirmingDismiss",
            self.modal,
            "V52-A: dismiss must use two-step confirmation via `confirmingDismiss`",
        )
        # `dismiss()` must NOT call `onClose()` directly without a
        # completed-action guard. Sniff-test: dismiss() calls abort() on
        # second pass.
        match = re.search(
            r"function dismiss\s*\(\)\s*\{(.+?)^  \}",
            self.modal,
            re.DOTALL | re.MULTILINE,
        )
        self.assertIsNotNone(
            match,
            "could not locate dismiss() function body for V52-A audit",
        )
        body = match.group(1) if match else ""
        # Must reference confirmingDismiss + must call abort() at some
        # point in the dismiss path (the second-confirmation hop).
        self.assertIn(
            "confirmingDismiss",
            body,
            "dismiss() must check confirmingDismiss for two-step confirmation",
        )
        self.assertIn(
            "abort()",
            body,
            "dismiss() must call abort() when the user confirms dismiss intent",
        )

    def test_escape_key_routed_through_dismiss(self) -> None:
        """V52-A: pressing Escape must NOT bypass the dismiss flow. The
        document-level handler must call `dismiss()` so the user gets the
        same confirmation as a backdrop click."""
        self.assertIn(
            "handleEscape",
            self.modal,
            "V52-A: missing handleEscape listener for document-level Escape capture",
        )
        # The listener calls dismiss(), not onClose() directly.
        match = re.search(
            r"function handleEscape\s*\([^)]*\)\s*\{(.+?)^  \}",
            self.modal,
            re.DOTALL | re.MULTILINE,
        )
        self.assertIsNotNone(
            match,
            "could not locate handleEscape function body",
        )
        body = match.group(1) if match else ""
        self.assertIn(
            "dismiss()",
            body,
            "handleEscape must route through dismiss() (not directly to onClose())",
        )
        # Document-level listener registered + cleaned up.
        self.assertIn(
            "addEventListener('keydown', handleEscape)",
            self.modal,
            "Escape listener must be registered on window-level keydown in onMount",
        )
        self.assertIn(
            "removeEventListener('keydown', handleEscape)",
            self.modal,
            "Escape listener must be removed on destroy (avoid leak between modals)",
        )

    def test_resolving_state_prevents_button_races(self) -> None:
        """A `resolving` boolean disables both resolution buttons while
        a checkout+commit is in flight against the same working tree."""
        self.assertIn(
            "resolving",
            self.modal,
            "missing `resolving` shared state — buttons could race against the same tree",
        )
        self.assertIn(
            "resolutionMode",
            self.modal,
            "missing `resolutionMode` enum — UI label can't reflect which side is in flight",
        )


if __name__ == "__main__":
    unittest.main()
