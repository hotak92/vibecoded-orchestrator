# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""CI runner + vocabulary-parity leg for the `vct` name-shape / shared-scope guards.

Two gaps in ``tools/vct-secrets/vct`` are covered here:

GAP 1 (security)
    ``validate_name`` rejected empty / over-long / path-traversal /
    out-of-charset names but never asked whether the NAME itself IS a live
    credential. A swapped argument (``vct set --key <the token>``) therefore
    passed every check, and the credential became a FILENAME on disk plus a
    cleartext row in ``audit.log``'s ``secrets`` field. The rejection message
    also echoed the rejected input, so the path that refused a malformed key
    was itself a second exposure.

GAP 2 (availability)
    ``set`` hardcoded ``projects/<project>/<key>``, so ``--project shared``
    created a project literally named "shared". No consumer of the shared
    namespace reads ``projects/shared/`` — not the per-project resolver order,
    not ``templates/scripts/vct_secrets_resolve.sh``, not
    ``vco_lib/agent_secrets.py``, not ``tools/vct-secrets/git-credential-vct``.

The behavioural assertions live in the bash suite
``tools/vct-secrets/tests/test_vct_name_shape_and_shared_scope.sh`` (same
harness as the pre-existing ``test_vct.sh``); this module shells out to it the
way ``tests/test_vct_secrets_cli_suite_runner.py`` does for its sibling, so the
assertions run in CI instead of only when someone remembers to type ``bash``.

It ALSO carries the vocabulary-parity leg, which cannot live in bash: the name
guard's shape list is the union of the two credential vocabularies this repo
already maintains, and drift between them is the failure mode worth pinning.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_VCT = REPO_ROOT / "tools" / "vct-secrets" / "vct"
_SUITE = REPO_ROOT / "tools" / "vct-secrets" / "tests" / "test_vct_name_shape_and_shared_scope.sh"
_CHECK_NO_SECRETS = REPO_ROOT / "scripts" / "check-no-secrets.sh"
_BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    _BASH is None,
    reason="no bash on PATH — POSIX vct name-shape suite skipped (Windows host)",
)


def _hermetic_env(tmp_path) -> dict:
    """Env that makes the vct hub probe soft-fail deterministically.

    Mirrors tests/test_vct_secrets_cli_suite_runner.py: the suite isolates
    VCT_SECRETS_DIR itself but the miss-path probe would otherwise reach
    whatever hub is running on the developer's machine with whatever token
    their shell exported.
    """
    env = dict(os.environ)
    empty_state = tmp_path / "empty-state"
    empty_state.mkdir(exist_ok=True)
    env["VCT_STATE_DIR"] = str(empty_state)
    for key in ("VCT_HUB_TOKEN", "VCT_HUB_PORT", "VCT_HUB_TOKEN_STRICT"):
        env.pop(key, None)
    return env


def test_name_shape_and_shared_scope_bash_suite_passes(tmp_path):
    assert _SUITE.is_file(), f"missing bash suite: {_SUITE}"
    proc = subprocess.run(
        [_BASH, str(_SUITE)],
        capture_output=True,
        text=True,
        env=_hermetic_env(tmp_path),
        cwd=str(tmp_path),
        timeout=300,
    )
    assert proc.returncode == 0, (
        "vct name-shape/shared-scope suite FAILED\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )


# ---------------------------------------------------------------------------
# Vocabulary parity — the name guard must stay at least as strong as the
# repo's canonical token-shape anchor.
# ---------------------------------------------------------------------------

# One representative synthetic value per shape declared in
# scripts/check-no-secrets.sh::TOKEN_SHAPES. Built from repeats so a miscount
# cannot silently weaken the assertion, and so no literal token-shaped string
# sits in this file for check-no-secrets.sh itself to have to reason about.
_ANCHOR_SAMPLES = {
    "ghp_ classic PAT": "ghp_" + "A" * 36,
    "github_pat_ fine-grained": "github_pat_" + "A" * 22 + "_" + "B" * 59,
    "sk- OpenAI-style": "sk-" + "A" * 30,
    # The `sk-<vendor>-<tail>` family that TOKEN_SHAPES' narrowed tail misses
    # but a NAME guard must not: OpenRouter / Anthropic / OpenAI project keys.
    "sk-or-v1- OpenRouter": "sk-or-v1-" + "deadbeef" * 8,
    "sk-ant-api03- Anthropic": "sk-ant-api03-" + "A" * 30,
    "sk-proj- OpenAI project": "sk-proj-" + "A" * 30,
}

# Names a human plausibly chooses. The guard MUST let every one of these
# through — this is the false-positive budget, asserted rather than assumed.
_LEGITIMATE_NAMES = [
    "API_KEY",
    "github_pat",
    "github_pat.myorg",
    "github_pat_personal",
    "github_pat_ci",
    "openai_api_key",
    "sk_test_key",
    "sk-app",
    "my-service-token",
    "supabase_token",
    "claude_code_oauth_token",
    "telegram_onboarding_chat_id",
    "AKIA_ROTATION_NOTES",
]


def _name_is_rejected(name: str, tmp_path, tag: str) -> bool:
    """Run `vct set` with `name` as the KEY; True when the CLI refuses it."""
    store = tmp_path / f"store-{tag}"
    env = _hermetic_env(tmp_path)
    env["VCT_SECRETS_DIR"] = str(store)
    proc = subprocess.run(
        [str(_VCT), "set", "--project", "probe", "--key", name],
        input="x",
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    if proc.returncode == 0:
        return False
    # A refusal must never quote the input back — that is the secondary-leak
    # fix, so it is asserted on every rejection this helper observes rather
    # than only in the dedicated test.
    assert name not in proc.stderr, "rejection message echoed the rejected name"
    assert name not in proc.stdout, "rejection message echoed the rejected name"
    return True


@pytest.mark.parametrize("label,sample", sorted(_ANCHOR_SAMPLES.items()))
def test_guard_covers_canonical_token_shapes(label, sample, tmp_path):
    """Every shape the repo's canonical anchor knows about is refused as a NAME.

    scripts/check-no-secrets.sh::TOKEN_SHAPES is documented as "one pattern
    home, never a fourth fork". This is a SEMANTIC parity check rather than a
    byte-comparison: it asserts the name guard is at least as strong as the
    anchor, so adding a shape to the anchor without teaching this guard about
    it fails here.
    """
    assert _name_is_rejected(sample, tmp_path, label.split()[0]), (
        f"{label}: a value matching the canonical token-shape anchor was "
        "accepted as a key NAME"
    )


@pytest.mark.parametrize("name", _LEGITIMATE_NAMES)
def test_guard_accepts_legitimate_names(name, tmp_path):
    """The leave-alone side: plausible human key names are untouched."""
    assert not _name_is_rejected(name, tmp_path, name.replace(".", "_")), (
        f"guard rejected a legitimate key name: {name}"
    )


def test_anchor_shape_count_is_pinned():
    """Fail loudly when the shared vocabulary grows without this guard revisited.

    RE-PINNED to the post-consolidation arrangement. Previously this parsed a
    literal ``TOKEN_SHAPES=( ... )`` block out of scripts/check-no-secrets.sh
    and asserted it held exactly 4 entries. That block no longer exists: the
    anchor and the ``vct`` name guard now both derive from the ONE
    credential-shape vocabulary (vco_lib/credential_shapes.py), so a textual
    parse finds nothing.

    The ASSERTION IS NOT RELAXED — it is strengthened. The old count pin was a
    proxy for "someone added a shape to the anchor but forgot to teach the name
    guard about it". That drift is now expressible directly, so this asserts the
    real invariant instead of the proxy: every shape in the file-scanning
    ``repo_scan`` context must ALSO be covered by ``argv_name``, unless it is
    structurally impossible as a key name. The counts are still pinned so that
    ADDING a shape anywhere forces a deliberate revisit here.
    """
    from vco_lib.credential_shapes import SHAPES, patterns_for_context

    # 1) The anchor must no longer carry a private literal list; it must read
    #    the shared vocabulary. (If a literal block ever returns, this trips.)
    assert _CHECK_NO_SECRETS.is_file(), f"missing anchor: {_CHECK_NO_SECRETS}"
    anchor_text = _CHECK_NO_SECRETS.read_text(encoding="utf-8")
    assert "credshapes_for_context repo_scan" in anchor_text, (
        "scripts/check-no-secrets.sh must derive TOKEN_SHAPES from the shared "
        "credential-shape vocabulary (repo_scan context)"
    )

    repo_ids = [i for i, _l, _p in patterns_for_context("repo_scan")]
    name_ids = [i for i, _l, _p in patterns_for_context("argv_name")]

    # 2) Size pins — bump these DELIBERATELY when adding a shape, after
    #    checking the coverage assertion below still holds.
    assert len(repo_ids) == 6, (
        f"repo_scan context changed size ({len(repo_ids)} shapes, expected 6). "
        "Adding a file-scanning shape means deciding whether the vct name guard "
        "must also refuse it — see the coverage assertion below, then bump this."
    )
    assert len(name_ids) == 8, (
        f"argv_name context changed size ({len(name_ids)} shapes, expected 8). "
        "Update this count once the new shape is intentional."
    )

    # 3) The invariant the old count pin was a proxy for: the NAME guard is at
    #    least as strong as the file-scanning anchor.
    #
    #    `pem_private_key` is the one anchor shape a NAME can never carry: it
    #    starts with '-' and contains spaces, both already rejected by
    #    validate_name's structural arms. Named here so the gap stays deliberate.
    NAME_IMPOSSIBLE = {"pem_private_key"}
    uncovered = [i for i in repo_ids if i not in name_ids and i not in NAME_IMPOSSIBLE]
    assert not uncovered, (
        f"shape(s) {uncovered} are scanned for in files but NOT refused as a key "
        "name. Add them to the argv_name context in vco_lib/credential_shapes.py "
        "(and its mirrors), or justify them in NAME_IMPOSSIBLE."
    )
    assert "pem_private_key" in repo_ids, (
        "the PEM anchor shape disappeared; re-check which anchor shapes are "
        "structurally impossible as a key name"
    )
    assert {s.id for s in SHAPES} >= set(repo_ids) | set(name_ids)
