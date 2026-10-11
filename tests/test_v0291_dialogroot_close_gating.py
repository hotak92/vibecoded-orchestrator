# SPDX-License-Identifier: AGPL-3.0-or-later
"""v0.2.91 P2-B6 — `DialogRoot.onClose` is a notification, not a gate.

`DialogRoot.onDialogClick` (DialogRoot.svelte:148-153) calls `dialogEl.close()`
whenever `closeOnBackdrop` is true, and `onCancel` (:155-159) only blocks
Escape when `closeOnEscape` is false. **Neither consults `onClose`.** So a
mount that writes

    onClose={allDone ? onClose : undefined}

does not prevent an early dismissal — it only prevents the PARENT being told
about one. `EnrichmentProgressModal` did exactly that, and the parent
(`KgCodegraphTab`) therefore never nulled `enrichmentTarget`: the component
stayed mounted with a literal `open={true}` that nothing could re-trigger,
so the dialog could never be shown again and the run went silent for good.

A repo-wide audit at the time of the finding put the whole family at three
sites out of 33 `<DialogRoot` mounts, so the rule is cheap to hold: **no
mount may pass a conditional `onClose`.** Either dismissal is allowed (pass
the handler unconditionally) or it is blocked (`closeOnBackdrop` /
`closeOnEscape`) — gating the notification is neither, and is wrong for both
intents.

Deliberately NOT asserted here: that every mount passes an `onClose` at all.
`UpdateAllProjectsModal` omits it, which is a different defect (P2-B5) with a
different fix (single-flight + close-gating) landing separately.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SVELTE_ROOT = REPO_ROOT / "launcher" / "src"

# A conditional expression in the attribute value: a ternary, or a short-
# circuit that can evaluate to undefined.
_CONDITIONAL = re.compile(r"\?[^?:]*:|&&|\|\|")


def _iter_dialogroot_mounts(src: str):
    """Yield the attribute text of each `<DialogRoot ...>` opening tag."""
    for m in re.finditer(r"<DialogRoot\b", src):
        i = m.end()
        depth = 0
        while i < len(src):
            ch = src[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            elif ch == ">" and depth == 0:
                break
            i += 1
        yield src[m.end() : i]


def _attr_expression(attrs: str, name: str) -> str | None:
    """Return the `{...}` expression bound to `name`, or None if absent."""
    m = re.search(rf"\b{name}=\{{", attrs)
    if not m:
        return None
    i = m.end()
    depth = 1
    start = i
    while i < len(attrs) and depth:
        if attrs[i] == "{":
            depth += 1
        elif attrs[i] == "}":
            depth -= 1
        i += 1
    return attrs[start : i - 1]


class DialogRootOnCloseIsNeverUsedAsAGate(unittest.TestCase):
    def test_no_mount_passes_a_conditional_on_close(self) -> None:
        offenders: list[str] = []
        mounts = 0
        for path in sorted(SVELTE_ROOT.rglob("*.svelte")):
            src = path.read_text(encoding="utf-8")
            if "<DialogRoot" not in src:
                continue
            for attrs in _iter_dialogroot_mounts(src):
                mounts += 1
                expr = _attr_expression(attrs, "onClose")
                if expr is None:
                    continue
                if _CONDITIONAL.search(expr):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: onClose={{{expr}}}")
        self.assertGreater(mounts, 10, "the mount scanner found suspiciously few sites")
        self.assertEqual(
            offenders,
            [],
            "onClose does not gate dismissal — it only decides whether the "
            "parent is told. Pass it unconditionally, or block dismissal with "
            "closeOnBackdrop/closeOnEscape:\n  " + "\n  ".join(offenders),
        )


# The site the rule came from, `EnrichmentProgressModal`, was retired in
# v0.2.102 W6-J with its only mount (the per-project KG model picker on
# `KgCodegraphTab`); the repo-wide rule above keeps holding every other mount.


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
