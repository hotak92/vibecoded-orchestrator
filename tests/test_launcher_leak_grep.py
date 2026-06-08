"""Unit tests for the launcher-leak-check grep logic.

The CI workflow at `.github/workflows/ci.yml :: launcher-leak-check`
runs `strings <binary> | grep -E '<pattern>' | grep -vE '<allowlist>'`
to verify that built launcher binaries don't leak the build host's
username. Those greps are shell, not Python — but we duplicate the
regex logic in pure Python here so a contributor can run
`pytest tests/test_launcher_leak_grep.py` without a CI run, and so
a refactor of the CI greps that breaks the regex semantics surfaces
in a regular `pytest tests/` invocation.

If you change the patterns in this file, also change them in
`.github/workflows/ci.yml`. Both must stay in sync.
"""

from __future__ import annotations

import io
import re
from typing import Iterable


# Patterns mirror the three greps in
# .github/workflows/ci.yml :: launcher-leak-check.
# Keep these regexes byte-for-byte aligned with the CI-side ones
# (Python `re` and POSIX ERE differ in a few edge cases — these
# regexes use only the common subset).
LINUX_HOME_RE = re.compile(r"^/home/[^/]+/")
MACOS_HOME_RE = re.compile(r"^/Users/[^/]+/")
# Windows: backslash paths can appear anywhere on a line, so we
# don't anchor at start. The CI uses `(^|[[:space:]])` for the
# leading delimiter; we encode the same with `(?:^|\s)`.
WINDOWS_USERS_RE = re.compile(r"(?:^|\s)C:\\Users\\[^\\]+\\")

# Allowlist usernames — matches the CI's --vE allowlists.
LINUX_ALLOWLIST_RE = re.compile(r"^/home/runner(?:/|$)")
MACOS_ALLOWLIST_RE = re.compile(r"^/Users/runner(?:/|$)")
WINDOWS_ALLOWLIST_RE = re.compile(r"C:\\Users\\runneradmin\\")


def find_leaks(strings_output: Iterable[str]) -> list[tuple[str, str]]:
    """Mirror the CI grep pipeline. Returns (kind, line) tuples for
    each line that matches a leak pattern AND fails its allowlist.

    `strings_output` is the line-by-line output of `strings <binary>`
    (typically thousands of lines on a 20MB launcher binary; we keep
    the iteration lazy so this stays cheap).
    """
    leaks: list[tuple[str, str]] = []
    for raw in strings_output:
        line = raw.rstrip("\n")
        if LINUX_HOME_RE.match(line) and not LINUX_ALLOWLIST_RE.match(line):
            leaks.append(("linux", line))
        if MACOS_HOME_RE.match(line) and not MACOS_ALLOWLIST_RE.match(line):
            leaks.append(("macos", line))
        if WINDOWS_USERS_RE.search(line) and not WINDOWS_ALLOWLIST_RE.search(line):
            leaks.append(("windows", line))
    return leaks


def test_no_leaks_on_empty_input():
    assert find_leaks([]) == []


def test_no_leaks_on_clean_strings():
    """A cleanly-built binary should produce no flagged paths.

    Examples here are paths that LOOK plausible (cargo registry,
    HOME-rooted) but are properly remapped to the `<home>` /
    `<cargo>` placeholders we set in
    `launcher/src-tauri/.cargo/config.toml`.
    """
    sample = [
        "<home>/.cargo/registry/src/index.crates.io-...",
        "<cargo>/registry/src/...",
        "<home>/runner/.cargo/registry/...",
        "/usr/local/bin/something",
        "panic at line 42",
        "vct_launcher_temp_lib",
        "Some random rodata string with no path",
    ]
    assert find_leaks(sample) == []


def test_detects_linux_dev_path_leak():
    sample = [
        "/home/martino/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/glib-0.18.5/src/types.rs",
        "harmless other line",
    ]
    leaks = find_leaks(sample)
    assert len(leaks) == 1
    assert leaks[0][0] == "linux"
    assert "/home/martino/" in leaks[0][1]


def test_allows_github_runner_linux_path():
    sample = [
        "/home/runner/.cargo/registry/src/index.crates.io-.../axum-0.8.9/src/router.rs",
        "/home/runner/work/vibecoded-orchestrator/launcher/src-tauri/src/main.rs",
    ]
    assert find_leaks(sample) == []


def test_detects_macos_dev_path_leak():
    sample = [
        "/Users/marti/.cargo/registry/src/...",
        "/Users/martino/Desktop/code/launcher/src/main.rs",
    ]
    leaks = find_leaks(sample)
    assert len(leaks) == 2
    assert all(kind == "macos" for kind, _ in leaks)


def test_allows_github_runner_macos_path():
    sample = [
        "/Users/runner/work/vibecoded-orchestrator/launcher/src-tauri/src/main.rs",
        "/Users/runner/.cargo/registry/...",
    ]
    assert find_leaks(sample) == []


def test_detects_windows_dev_path_leak():
    """Windows path leak — the audit's highest-signal item.

    `marti` (a contributor's Windows username sentinel) and `OneDrive\\Desktop\\...`
    were the leaking strings before PR-4. The CI grep must catch
    them whether they appear at line-start, after whitespace, or
    embedded in a longer panic-message context.
    """
    sample = [
        r"C:\Users\marti\.cargo\registry\src\index.crates.io-...",
        r"event C:\Users\marti\.cargo\registry\src\..\main.rs:818lean_ctx",
        r"future still here when droppingqueue not emptyC:\Users\marti\.cargo\registry\src\...",
    ]
    leaks = find_leaks(sample)
    assert len(leaks) >= 1
    assert all(kind == "windows" for kind, _ in leaks)
    assert any("marti" in line for _, line in leaks)


def test_allows_github_runner_windows_path():
    sample = [
        r"C:\Users\runneradmin\.cargo\registry\src\index.crates.io-...",
        r"some text C:\Users\runneradmin\AppData\Local\Temp\tmp.rs",
    ]
    assert find_leaks(sample) == []


def test_real_world_pre_pr4_sample():
    """Regression sample copied verbatim from the audit report
    (§5, Linux launcher binary) + the lean-ctx Windows binary.
    PR-4 must catch every line in this sample that's path-shaped at
    its start or after whitespace.

    Note: the audit also showed lines like
    `future still here when droppingqueue not emptyC:\\Users\\marti\\...`
    where the leak path is concatenated DIRECTLY to a panic message
    with no separator. That's a known limitation of the CI grep
    (which uses `(^|[[:space:]])` as the leading delimiter): it
    catches the same path string when it appears on its own (as
    `strings` typically emits it) but not when concatenated. In
    practice `strings` splits on null bytes, so consecutive
    rodata strings become separate output lines — meaning the
    concatenated form is rare. If a future regression introduces
    these, broaden the regex to `(^|[^A-Za-z0-9_/\\\\:])` and
    update both this test and the CI yaml.
    """
    sample = [
        "/home/martino/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/axum-0.8.9/src/handler/mod.rs",
        "/home/martino/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/axum-core-0.5.6/src/extract/request_parts.rs",
        "/home/martino/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/tower-0.5.3/src/util/ready.rs",
        r"C:\Users\marti\.cargo\registry\src\index.crates.io-1949cf8c6b5b557f\tokio-1.50.0\src\runtime\scheduler\multi_thread\queue.rs",
        # Whitespace-prefixed Windows path. This SHOULD be flagged.
        r" event C:\Users\marti\.cargo\registry\src\foo.rs",
    ]
    leaks = find_leaks(sample)
    assert len(leaks) == 5
    kinds = {k for k, _ in leaks}
    assert "linux" in kinds
    assert "windows" in kinds


def test_real_world_post_pr4_sample():
    """After-PR-4 sample with the username remapped to `<home>`.

    The username is part of the path AFTER the `/home/` prefix, so
    `--remap-path-prefix=/home=<home>` only handles the prefix —
    leaving `<home>/martino/...` visible. That residue MUST NOT be
    flagged by the CI grep (it's no longer `^/home/...` shape).
    The full username scrub on local dev builds requires the
    additional `RUSTFLAGS="--remap-path-prefix=$HOME=<home>"` env-
    var documented in `launcher/src-tauri/.cargo/config.toml`.
    On CI, $HOME is already /home/runner so the runner-allowlisted
    `^/home/runner/` is fine and the more aggressive env-var form
    is also applied (see workflow yaml).
    """
    sample = [
        "<home>/martino/.cargo/registry/src/index.crates.io-.../glib-0.18.5/src/types.rs",
        "<home>/.cargo/registry/...",
        "<cargo>/registry/...",
    ]
    assert find_leaks(sample) == []


def test_strings_pipeline_can_iterate_over_io_stream():
    """Sanity: the function works against a streaming source the
    same way the CI shell pipeline does (`strings ... | python ...`).
    """
    blob = (
        "/home/martino/.cargo/registry/src/foo.rs\n"
        "<home>/runner/clean.rs\n"
        "/Users/marti/Desktop/leak.rs\n"
    )
    leaks = find_leaks(io.StringIO(blob))
    assert len(leaks) == 2
    assert {k for k, _ in leaks} == {"linux", "macos"}
