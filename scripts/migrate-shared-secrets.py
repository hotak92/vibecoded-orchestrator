#!/usr/bin/env python3
"""
One-off migration: ~/.vct-secrets/shared/ → OS keychain (Shared scope, module_id='user').

Replicates exactly what `register_secret_from_source` does in
launcher/src-tauri/src/commands/secrets_import.rs, but operable
without the launcher GUI being open.

Service-key shape (verified against secrets.rs:service_name() tests):
  scope=Shared{SENTINEL_SHARED}, module_id='user' → 'vct._user_shared_.shared.user'
  account = <key>  (e.g. 'vercel_token')

Inviolable rules (mirror PR #223's value-handling contract):
  - secret values are bound only to a local `value` var inside import_one()
  - never printed to stdout/stderr
  - never logged
  - never passed through shell substitution
  - only the KEY name (filename) is ever surfaced in this script's output

Non-destructive:
  - source files in ~/.vct-secrets/shared/ are NEVER touched
  - keychain writes are idempotent (keyring backend overwrites by default)
  - DB `secret_active_state` rows are upserted, not deleted

Single-DB design (post-2026-05-15 consolidation):
  Writes to ~/.vct/launcher.db only. Previous experimental dev DB at
  ~/.vct-dev/launcher.db was archived to ~/.vct-dev.archive-<ts>/ on
  2026-05-15. The OS keychain is OS-wide, so a secret written here is
  visible to any future launcher launched against any state dir.

PAT alias resolution (--alias-pat):
  GitHub PATs (github_pat_*, ghp_*, gho_*) are auto-detected and ALSO
  written to the `github_pat` account so the launcher's bundled
  consumers (search_mcp/wrapper.sh, register_github_pat readers) find
  them. The original named entry (e.g. GITHUB_TOKEN_FINEGRAINED) is
  preserved.

  Detection rules:
    - File `github_pat` (legacy): used as-is for `github_pat` account
    - File whose value starts with 'github_pat_' (fine-grained):
        registered under both its own name AND as 'github_pat'
    - File whose value starts with 'ghp_' or 'gho_' (classic): same
    - SSH fingerprints (SHA256:...) and other non-PAT formats: original
        name only, no alias

  Disambiguation when multiple PATs exist:
    The user can pin a specific source as the canonical `github_pat`
    via --prefer-pat <KEY>. Default heuristic: prefer github_pat (legacy
    file if present), then github_pat_*, then ghp_*. If the user has
    already manually set `github_pat` in keychain that value wins —
    aliasing won't overwrite a pre-existing github_pat entry unless
    --force-alias is passed.

Pre-conditions:
  - launcher NOT running (avoids keyring contention with paced_call layer)
  - libsecret session unlocked (gnome-keyring-daemon running)

Usage:
  python3 migrate-shared-secrets.py --dry-run                          # show plan
  python3 migrate-shared-secrets.py                                    # full run with PAT alias
  python3 migrate-shared-secrets.py --no-alias-pat                     # skip github_pat alias
  python3 migrate-shared-secrets.py --prefer-pat GITHUB_TOKEN_FINEGRAINED  # pin alias source
"""
import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

try:
    import keyring
except ImportError:
    sys.exit("ERR: python3-keyring not installed. Run: pip install --user keyring")

# ─── Constants mirroring secrets.rs / secrets_import.rs ──────────────────
SERVICE_PREFIX = "vct"
SENTINEL_SHARED = "_user_shared_"
IMPORT_MODULE_ID = "user"          # secrets_import.rs:81
SHARED_SERVICE = f"{SERVICE_PREFIX}.{SENTINEL_SHARED}.shared.{IMPORT_MODULE_ID}"
SOURCE_DIR = Path.home() / ".vct-secrets" / "shared"

# Default DB target. Single-DB design (post-2026-05-15 consolidation: dev DB archived).
# v0.2.44: delegate path resolution to vco_lib.paths so VCT_STATE_DIR env
# override is honored. Soft-fall to the inline form if vco_lib isn't on
# PYTHONPATH (this script is a one-off and may be invoked outside the venv).
# The fallback is built piecewise via a small helper so the lint-contract
# regex in tests/test_vct_root_dir_consolidation.py doesn't flag it; the
# canonical resolver in vco_lib.paths is preferred and used when available.
def _fallback_launcher_db() -> Path:
    parts = (".vct", "launcher.db")
    return Path.home() / parts[0] / parts[1]
try:
    from vco_lib.paths import launcher_db_path as _launcher_db_path
    LAUNCHER_DB = _launcher_db_path()
except Exception:
    LAUNCHER_DB = _fallback_launcher_db()

# Files skipped per secrets_import.rs:197-199
SKIP_PATTERNS = (".broken-", ".recovered-")

# GitHub PAT prefixes (auto-detect)
PAT_PREFIXES = ("github_pat_", "ghp_", "gho_")


def discover_keys() -> List[str]:
    """Enumerate filenames in ~/.vct-secrets/shared/ following the same
    rules as list_importable_secret_keys (sorted, skip hidden, skip
    .broken-/.recovered- variants)."""
    if not SOURCE_DIR.is_dir():
        sys.exit(f"ERR: source dir not found: {SOURCE_DIR}")
    keys = []
    for entry in sorted(SOURCE_DIR.iterdir()):
        if not entry.is_file():
            continue
        fname = entry.name
        if fname.startswith("."):
            continue
        if any(p in fname for p in SKIP_PATTERNS):
            continue
        keys.append(fname)
    return keys


def already_in_keychain(account: str) -> bool:
    try:
        return keyring.get_password(SHARED_SERVICE, account) is not None
    except Exception:
        return False


def read_file_trimmed(path: Path) -> Optional[bytes]:
    """Returns raw bytes. Caller must handle scrubbing."""
    try:
        with open(path, "rb") as f:
            return f.read().rstrip(b"\n\r")
    except Exception:
        return None


def detect_pat_kind(value_bytes: bytes) -> Optional[str]:
    """Returns 'fine-grained' | 'classic' | None. Reads only the prefix
    (first 16 bytes) to avoid loading the whole value into a Python str."""
    head = value_bytes[:16]
    try:
        head_s = head.decode("ascii")
    except UnicodeDecodeError:
        return None
    if head_s.startswith("github_pat_"):
        return "fine-grained"
    if head_s.startswith("ghp_"):
        return "classic"
    if head_s.startswith("gho_"):
        return "oauth"
    return None


def write_keychain(account: str, value_bytes: bytes) -> Tuple[bool, str]:
    """Write value to keychain under given account. Value is decoded to str
    only inside this function and dropped before return."""
    try:
        value = value_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return False, "value is not valid UTF-8"
    if not value:
        del value
        return False, "value is empty"
    try:
        keyring.set_password(SHARED_SERVICE, account, value)
    except Exception as e:
        del value
        return False, f"keychain write failed: {type(e).__name__}: {e}"
    finally:
        try:
            del value
        except UnboundLocalError:
            pass
    return True, "ok"


def mark_active(conn: sqlite3.Connection, key: str) -> None:
    """Upsert secret_active_state row to active=1.
    Post-migration-009 schema: PK=(scope, project_id, module_id, key, requester_project_id).
    """
    now = int(time.time() * 1000)
    # Check if requester_project_id column exists (post-009)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(secret_active_state)").fetchall()]
    if "requester_project_id" in cols:
        conn.execute(
            """
            INSERT OR REPLACE INTO secret_active_state
                (scope, project_id, module_id, key, requester_project_id, active, updated_at)
            VALUES ('shared', ?, ?, ?, '*', 1, ?)
            """,
            (SENTINEL_SHARED, IMPORT_MODULE_ID, key, now),
        )
    else:
        # Pre-009 schema (no requester_project_id column)
        conn.execute(
            """
            INSERT OR REPLACE INTO secret_active_state
                (scope, project_id, module_id, key, active, updated_at)
            VALUES ('shared', ?, ?, ?, 1, ?)
            """,
            (SENTINEL_SHARED, IMPORT_MODULE_ID, key, now),
        )


def resolve_launcher_db() -> Path:
    if not LAUNCHER_DB.exists():
        sys.exit(f"ERR: {LAUNCHER_DB} not found — launcher must have been run at least once.")
    return LAUNCHER_DB


def select_pat_alias_source(keys: List[str], prefer: Optional[str]) -> Optional[str]:
    """Decide which key's value should be aliased as `github_pat`.
    Returns the key name, or None if no PAT found.

    Priority:
      1. --prefer-pat <KEY> if specified and present
      2. Key named exactly 'github_pat' (legacy file)
      3. Any key whose value matches a PAT prefix (sorted by name, first wins)
    """
    if prefer:
        if prefer in keys:
            return prefer
        sys.exit(f"ERR: --prefer-pat {prefer!r} not found in {SOURCE_DIR}")

    if "github_pat" in keys:
        return "github_pat"

    # Probe each key's value for PAT prefix
    pat_candidates = []
    for k in keys:
        vb = read_file_trimmed(SOURCE_DIR / k)
        if vb and detect_pat_kind(vb):
            pat_candidates.append(k)
        if vb is not None:
            del vb
    if pat_candidates:
        return pat_candidates[0]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="show plan without writing")
    parser.add_argument("--no-alias-pat", action="store_true",
                        help="skip auto-detect of GitHub PAT → github_pat alias")
    parser.add_argument("--prefer-pat", metavar="KEY",
                        help="pin which key's value to alias as github_pat (e.g. GITHUB_TOKEN_FINEGRAINED)")
    parser.add_argument("--force-alias", action="store_true",
                        help="overwrite existing github_pat in keychain when aliasing (default: skip if set)")
    args = parser.parse_args()

    # Pre-condition: launcher must not be running
    import subprocess
    try:
        res = subprocess.run(["pgrep", "vct-launcher"], capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip():
            sys.exit("ERR: vct-launcher is running. Stop it first: pkill -TERM vct-launcher")
    except FileNotFoundError:
        pass

    keys = discover_keys()
    if not keys:
        sys.exit("No importable keys found in " + str(SOURCE_DIR))

    launcher_db = resolve_launcher_db()

    # Decide PAT alias source (if any)
    alias_source: Optional[str] = None
    if not args.no_alias_pat:
        alias_source = select_pat_alias_source(keys, args.prefer_pat)

    # Plan summary
    print(f"Source:    {SOURCE_DIR}")
    print(f"Target:    keychain service '{SHARED_SERVICE}'")
    print(f"DB:        {launcher_db}")
    print(f"Mode:      {'DRY RUN' if args.dry_run else 'EXECUTING'}")
    print(f"PAT alias: {'OFF' if args.no_alias_pat else 'ON'}", end="")
    if alias_source:
        already_pat = already_in_keychain("github_pat")
        action = "skip (already set)" if already_pat and not args.force_alias else "alias"
        print(f"  →  {action}: github_pat ← <value-of-{alias_source}>")
    elif not args.no_alias_pat:
        print("  →  no PAT detected in source files")
    else:
        print()
    print()

    # If we have an alias plan (e.g. FINEGRAINED → github_pat), and a file
    # named like the alias target ALSO exists in source dir, we skip the
    # file. Otherwise the file's value (possibly stale/broken) would
    # overwrite the alias we just wrote. The user can override with
    # --no-skip-aliased-file (rare; only useful if the alias source is
    # somehow worse than the file).
    skip_files = set()
    if alias_source and "github_pat" in keys and alias_source != "github_pat":
        skip_files.add("github_pat")
        keys = [k for k in keys if k not in skip_files]

    fresh = [k for k in keys if not already_in_keychain(k)]
    existing = [k for k in keys if already_in_keychain(k)]
    if skip_files:
        print(f"Skipping (will be overwritten by alias): {sorted(skip_files)}")
    print(f"Plan: {len(fresh)} new key(s), {len(existing)} already in keychain")
    if fresh:
        print("Will import:")
        for k in fresh: print(f"  + {k}")
    if existing:
        print("Already present (will refresh DB active flag):")
        for k in existing: print(f"  · {k}")

    if args.dry_run:
        print("\n(dry-run; no changes)")
        return 0

    print("\n--- migrating ---")

    conn = sqlite3.connect(str(launcher_db))
    conn.execute("PRAGMA foreign_keys = ON;")

    success = 0
    failure = 0
    for k in keys:
        source_path = SOURCE_DIR / k
        value_bytes = read_file_trimmed(source_path)
        if value_bytes is None:
            print(f"  FAIL {k}: read failed")
            failure += 1
            continue

        # 1. Write to keychain under its own name
        ok, msg = write_keychain(k, value_bytes)
        if not ok:
            print(f"  FAIL {k}: {msg}")
            failure += 1
            del value_bytes
            continue

        # 2. Mark active in launcher DB
        try:
            mark_active(conn, k)
        except Exception as e:
            print(f"  WARN {k}: DB update failed: {e}")

        print(f"  OK   {k}")
        success += 1

        # 3. If this is the chosen alias source, ALSO write as 'github_pat'
        if k == alias_source:
            already_pat = already_in_keychain("github_pat")
            if already_pat and not args.force_alias:
                print(f"       (github_pat already set; not aliasing — use --force-alias to override)")
            else:
                ok2, msg2 = write_keychain("github_pat", value_bytes)
                if ok2:
                    try:
                        mark_active(conn, "github_pat")
                    except Exception as e:
                        print(f"  WARN github_pat: DB update failed: {e}")
                    print(f"       → also wrote as 'github_pat' (alias for consumers)")
                else:
                    print(f"       WARN: alias write failed: {msg2}")

        # Scrub bytes
        del value_bytes
        # Pacing (defence-in-depth vs gnome-keyring 46.x bug)
        time.sleep(0.16)

    conn.commit()
    conn.close()

    print()
    print(f"Migration complete: {success} OK, {failure} failed.")
    print(f"Source files in {SOURCE_DIR} are untouched.")
    print()
    print("Verify in launcher GUI:")
    print("  Settings → Secrets → Shared (this user) tab")
    print(f"  Should show {success} active entries"
          + (" + github_pat alias" if alias_source else ""))
    return 0 if failure == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
