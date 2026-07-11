//! Orchestrator copy + conflict-strategy file operations.
//!
//! Verbatim extraction (v0.2.77 Part 7d) of the recursive copy helpers
//! (`copy_recursive_sync`/`_gitignore_aware`/`_blind`/`_preserve_sync`,
//! `copy_orchestrator_to_sync`, `copy_orchestrator_with_count`,
//! `count_files_recursive`, `has_git_root`), the conflict-strategy applier
//! (`apply_conflict_strategy`, `ConflictApplyReport`), and the merge-pending
//! notification-block writers (`update_merge_notification_block`,
//! `build_merge_notification_block`, `replace_or_append_block`,
//! `new_sibling_path`, `new_sibling_display`) that previously lived inline in
//! `installer.rs`. Behaviour is unchanged; the facade re-exports every symbol.
//!
//! The `ConflictStrategy` enum + `MERGE_BLOCK_*` sentinel constants stay in the
//! facade (shared with the conflict-modal command surface); this module pulls
//! them via `super::`.

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

use super::{ConflictStrategy, ORCHESTRATOR_MANAGED_PATHS, MERGE_BLOCK_END, MERGE_BLOCK_START};

/// Synchronous recursive copy. Symlinks are resolved (file content
/// follows). Used by `copy_orchestrator_to_sync` so the caller (which is
/// already an async Tauri command) can `tokio::task::spawn_blocking` it.
///
/// **Gitignore-aware contract** (PR-4, 2026-05-06): when `src` is inside
/// a git repository (any ancestor contains `.git/`), the walker honors
/// `.gitignore` + `.git/info/exclude` + `core.excludesFile` + `.ignore`
/// files via the `ignore` crate (`WalkBuilder::standard_filters(true)`).
/// This prevents `update_orchestrator_at` from propagating machine-local
/// files between clones — `tools/vct-secrets/*.token`,
/// `.claude/agents/`, `.claude/skills/`, `.claude/logs/`,
/// `infrastructure/docker-compose.override.yml`, `state/`, etc. — which
/// the previous blind walker copied verbatim despite being gitignored
/// in the source tree.
///
/// Untracked-but-not-gitignored files ARE still copied (e.g. a file the
/// user just `touch`ed but hasn't committed yet) — `standard_filters`
/// only excludes things gitignore would exclude, not everything `git
/// status --porcelain` lists as `??`.
///
/// **Fallback contract**: if `src` isn't inside a git repo (no `.git/`
/// in any ancestor), we fall back to the old blind walker. This
/// preserves existing behavior for non-git fixtures (test harnesses
/// that mock the source dir) and for shipped non-checkout bundles.
pub(crate) fn copy_recursive_sync(src: &Path, dst: &Path) -> std::io::Result<()> {
    let meta = std::fs::metadata(src)?;
    if !meta.is_dir() {
        // Single-file copy path. The walker variants below are only
        // useful when `src` is a directory; for a plain file we just
        // copy it through.
        if let Some(parent) = dst.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::copy(src, dst)?;
        return Ok(());
    }

    if has_git_root(src) {
        copy_recursive_gitignore_aware(src, dst)
    } else {
        eprintln!(
            "[vct] copy_recursive_sync: source {} has no .git/ ancestor; \
             falling back to blind walker (gitignored files WILL be copied). \
             This is expected for non-checkout bundles and test fixtures.",
            src.display()
        );
        copy_recursive_blind(src, dst)
    }
}

/// True iff `start` or any ancestor contains a `.git` entry (file OR
/// directory — `.git` is a file in worktrees, a directory in the main
/// checkout). Used to gate gitignore-aware copying.
pub(crate) fn has_git_root(start: &Path) -> bool {
    let mut current = Some(start);
    while let Some(dir) = current {
        if dir.join(".git").exists() {
            return true;
        }
        current = dir.parent();
    }
    false
}

/// Gitignore-honoring recursive copy. Walks `src` via
/// `ignore::WalkBuilder` so `.gitignore`, `.git/info/exclude`,
/// `core.excludesFile`, and `.ignore` entries are respected. Each
/// visited entry is copied to `dst` at the same relative path.
pub(crate) fn copy_recursive_gitignore_aware(src: &Path, dst: &Path) -> std::io::Result<()> {
    use ignore::WalkBuilder;

    std::fs::create_dir_all(dst)?;

    let walker = WalkBuilder::new(src)
        // Honor .gitignore, .git/info/exclude, core.excludesFile, .ignore.
        // This is the SAME default ripgrep uses; chosen for behavioral
        // parity with what a developer would expect from `git ls-files
        // --cached --others --exclude-standard`.
        .standard_filters(true)
        // We DO want to descend into hidden dirs that aren't gitignored
        // (`.claude/` is a hidden dir but partially tracked). Default
        // `standard_filters(true)` already enables this for non-ignored
        // hidden entries.
        .hidden(false)
        // Don't follow symlinks — matches the old `std::fs::metadata`
        // behavior. (`metadata` follows; `symlink_metadata` doesn't.
        // The old walker used `metadata` so it followed; for a copy
        // operation that's fine — we WANT the file content, not the
        // dangling link.) The `ignore` crate defaults to NOT following
        // symlinks; we leave that default alone.
        .build();

    for result in walker {
        let entry = match result {
            Ok(e) => e,
            Err(e) => {
                // Permission error on a single subtree shouldn't abort
                // the whole copy. Mirror walkdir convention: log and
                // continue. (Catches the rare case where a `.git/`
                // sub-object is unreadable on shared developer boxes.)
                eprintln!("[vct] walker error: {}", e);
                continue;
            }
        };
        let path = entry.path();
        let rel = match path.strip_prefix(src) {
            Ok(r) => r,
            Err(_) => continue, // Defensive — shouldn't happen with WalkBuilder.
        };
        // The walker emits the root itself first; rel is empty for it.
        if rel.as_os_str().is_empty() {
            continue;
        }
        let dst_path = dst.join(rel);
        let file_type = entry.file_type();
        match file_type {
            Some(ft) if ft.is_dir() => {
                std::fs::create_dir_all(&dst_path)?;
            }
            Some(ft) if ft.is_file() => {
                if let Some(parent) = dst_path.parent() {
                    std::fs::create_dir_all(parent)?;
                }
                std::fs::copy(path, &dst_path)?;
            }
            // Symlinks / sockets / other: skip silently. Matches the
            // intent of an orchestrator-state copy operation.
            _ => continue,
        }
    }
    Ok(())
}

/// Blind recursive copy — the pre-PR-4 behavior. Used as a fallback
/// when `src` isn't inside a git repo. Preserved verbatim so non-git
/// callers (test fixtures, shipped non-checkout bundles) keep working.
pub(crate) fn copy_recursive_blind(src: &Path, dst: &Path) -> std::io::Result<()> {
    let meta = std::fs::metadata(src)?;
    if meta.is_dir() {
        std::fs::create_dir_all(dst)?;
        for entry in std::fs::read_dir(src)? {
            let entry = entry?;
            let s = entry.path();
            let d = dst.join(entry.file_name());
            copy_recursive_blind(&s, &d)?;
        }
    } else {
        if let Some(parent) = dst.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::copy(src, dst)?;
    }
    Ok(())
}

/// Copy every entry in `ORCHESTRATOR_MANAGED_PATHS` from `source` to
/// `target`. Missing source entries are silently skipped (some allowlist
/// entries are optional). Returns the source path that was used so the
/// caller can show it in the UI.
pub fn copy_orchestrator_to_sync(source: &Path, target: &Path) -> Result<(), String> {
    if !source.join("vct-module.json").exists() {
        return Err(format!(
            "source {} is not an orchestrator repo (no vct-module.json)",
            source.display()
        ));
    }
    std::fs::create_dir_all(target)
        .map_err(|e| format!("cannot create target {}: {}", target.display(), e))?;

    for managed in ORCHESTRATOR_MANAGED_PATHS.iter() {
        let src = source.join(managed);
        let dst = target.join(managed);
        if !src.exists() {
            continue;
        }
        copy_recursive_sync(&src, &dst).map_err(|e| {
            format!("copy {} -> {}: {}", src.display(), dst.display(), e)
        })?;
    }
    Ok(())
}

/// Result of running a `ConflictStrategy` against an install target.
/// Used by the install-log emitter and the FE success toast.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ConflictApplyReport {
    pub strategy: String,
    /// Number of preserve-list paths that already existed at the target
    /// and were left untouched (only meaningful for OverwritePreserve).
    pub preserved_count: usize,
    /// Number of `<file>.new.<ext>` siblings written next to preserved
    /// files (only meaningful for OverwritePreserve).
    pub new_md_count: usize,
    /// Whether the merge-notification block was written / refreshed in
    /// `.claude/CONTEXT_STATE.md`.
    pub notification_written: bool,
    /// Number of files copied from source to target on top of existing
    /// content. 0 for AdoptAsIs.
    pub copied_count: usize,
}

/// Insert `.new` before the file's extension. Examples:
///   - `CLAUDE.md` -> `CLAUDE.new.md`
///   - `.env` -> `.env.new` (no extension to split, append at end)
///   - `archive.tar.gz` -> `archive.tar.new.gz` (split on LAST `.`)
pub(crate) fn new_sibling_path(path: &Path) -> PathBuf {
    let parent = path.parent().unwrap_or_else(|| Path::new(""));
    let file_name = match path.file_name().and_then(|n| n.to_str()) {
        Some(n) => n,
        None => return path.with_extension("new"),
    };
    // Filename starting with `.` and no other `.` (e.g. `.env`) → treat
    // the leading dot as part of the stem so we don't write `.new.env`.
    let dot_idx = file_name.rfind('.').filter(|&i| i > 0);
    let new_name = match dot_idx {
        Some(i) => format!("{}.new{}", &file_name[..i], &file_name[i..]),
        None => format!("{}.new", file_name),
    };
    parent.join(new_name)
}

/// Append (or refresh) the merge-notification block in
/// `.claude/CONTEXT_STATE.md`. Idempotent: if a previous block exists, it
/// is REPLACED in-place rather than duplicated. The block is bounded by
/// the marker comments `MERGE_BLOCK_START` / `MERGE_BLOCK_END`.
///
/// Returns `true` iff the file was written (i.e. block needed adding or
/// updating). Returns `false` if the block was already present and
/// identical to what we'd write.
pub fn update_merge_notification_block(
    context_state_path: &Path,
    preserved_files: &[String],
) -> std::io::Result<bool> {
    let block = build_merge_notification_block(preserved_files);

    // CONTEXT_STATE.md ought to exist by the time we get here (we only
    // call this after a copy step that populates `.claude/`), but guard
    // anyway: if missing, create with just the block.
    if !context_state_path.exists() {
        if let Some(parent) = context_state_path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(context_state_path, &block)?;
        return Ok(true);
    }

    let existing = std::fs::read_to_string(context_state_path)?;
    let updated = replace_or_append_block(&existing, &block);
    if updated == existing {
        return Ok(false);
    }
    std::fs::write(context_state_path, updated)?;
    Ok(true)
}

pub(crate) fn build_merge_notification_block(preserved_files: &[String]) -> String {
    let list = if preserved_files.is_empty() {
        "_(none — strategy ran with an empty preserve list)_".to_string()
    } else {
        preserved_files
            .iter()
            .map(|p| format!("- `{}` (upstream-new at `{}`)", p, new_sibling_display(p)))
            .collect::<Vec<_>>()
            .join("\n")
    };

    // Important: the prose inside this block must NOT contain the
    // literal `MERGE_BLOCK_START` / `MERGE_BLOCK_END` strings, otherwise
    // the idempotency check (which counts marker occurrences) breaks.
    // We reference them obliquely as "the HTML-comment markers".
    format!(
        "{start}\n\
## Pending merge — read this on session start\n\
\n\
The orchestrator was just upgraded. Several user-curated files have an\n\
upstream-new version sitting next to them (`*.new.md` / `*.new.<ext>`).\n\
For each pair:\n\
\n\
1. Read both the existing file AND the upstream-new sibling.\n\
2. Reconcile: keep the user's project-specific content, but adopt new\n\
   structure / guidance / sections from the upstream version. Use your\n\
   judgment for ambiguous merges; ask the user if a conflict is\n\
   irreconcilable.\n\
3. After successfully merging a file, **delete its upstream-new\n\
   sibling**.\n\
4. When ALL `.new.*` siblings under the install path are gone, you'll\n\
   know the merge is complete — at that point, **delete this entire\n\
   notification block** (the HTML-comment markers wrapping this section\n\
   plus all text between them) from this CONTEXT_STATE.md. That removes\n\
   the prompt for the next session.\n\
\n\
Files awaiting merge:\n\
{list}\n\
\n\
Note: `MEMORY.md` lives at `~/.claude/projects/<id>/memory/MEMORY.md`,\n\
not in the install dir, so v1.0 of the conflict resolver does NOT write\n\
an upstream-new sibling for it. If you suspect your MEMORY.md is\n\
divergent from the upstream template, run a manual diff and merge by\n\
hand.\n\
\n\
(Do NOT delete user content. Preserve any session-specific state in\n\
CONTEXT_STATE.md, your existing CLAUDE.md customisations, etc. The\n\
upstream version is a reference for new structure, not a wholesale\n\
replacement.)\n\
{end}\n",
        start = MERGE_BLOCK_START,
        end = MERGE_BLOCK_END,
        list = list,
    )
}

/// Return a display-friendly `<file>.new.<ext>` rendering for the given
/// install-relative path. Used inside the notification block.
pub(crate) fn new_sibling_display(rel_path: &str) -> String {
    let p = PathBuf::from(rel_path);
    new_sibling_path(&p).to_string_lossy().to_string()
}

/// If `existing` already contains a `<!-- vct-merge-pending -->` ...
/// `<!-- /vct-merge-pending -->` block, replace it with `block`.
/// Otherwise, append `block` (separated by a blank line) to the end.
pub(crate) fn replace_or_append_block(existing: &str, block: &str) -> String {
    if let (Some(start), Some(end_rel)) = (
        existing.find(MERGE_BLOCK_START),
        existing[existing.find(MERGE_BLOCK_START).unwrap_or(0)..].find(MERGE_BLOCK_END),
    ) {
        let end = start + end_rel + MERGE_BLOCK_END.len();
        // Trim a single trailing newline after the existing block so we
        // don't accumulate blank lines on every refresh.
        let after = &existing[end..];
        let after_trimmed = after.strip_prefix('\n').unwrap_or(after);
        let mut out = String::with_capacity(existing.len() + block.len());
        out.push_str(&existing[..start]);
        out.push_str(block);
        out.push_str(after_trimmed);
        return out;
    }
    let sep = if existing.ends_with('\n') || existing.is_empty() {
        ""
    } else {
        "\n"
    };
    format!("{}{}\n{}", existing, sep, block)
}

/// Apply a `ConflictStrategy` at `target`, copying from `source`.
///
/// Defense: for `DeleteClaudeAndReinstall` we hard-assert the path we're
/// about to remove is exactly `<target>/.claude` (no symlink games, no
/// path traversal) before calling `remove_dir_all`. The launcher is
/// running with the user's full UID so any rmtree we issue is real.
pub fn apply_conflict_strategy(
    source: &Path,
    target: &Path,
    strategy: ConflictStrategy,
    preserve_paths: &[String],
) -> Result<ConflictApplyReport, String> {
    let mut report = ConflictApplyReport::default();
    report.strategy = format!("{:?}", strategy);

    if !source.join("vct-module.json").exists() {
        return Err(format!(
            "source {} is not an orchestrator repo (no vct-module.json)",
            source.display()
        ));
    }
    std::fs::create_dir_all(target)
        .map_err(|e| format!("cannot create target {}: {}", target.display(), e))?;

    match strategy {
        ConflictStrategy::AdoptAsIs => {
            // No-op on disk.
        }
        ConflictStrategy::DeleteClaudeAndReinstall => {
            let claude_dir = target.join(".claude");
            // Defense in depth: never rm anything other than the literal
            // `<target>/.claude` directory. Refuse symlinks and refuse
            // anything that resolves outside `target`.
            if claude_dir.exists() {
                let canon_target = target.canonicalize().map_err(|e| {
                    format!("canonicalize target {}: {}", target.display(), e)
                })?;
                let canon_claude = claude_dir.canonicalize().map_err(|e| {
                    format!("canonicalize {}: {}", claude_dir.display(), e)
                })?;
                let expected = canon_target.join(".claude");
                if canon_claude != expected {
                    return Err(format!(
                        "refusing to delete: {} resolves to {} (expected {})",
                        claude_dir.display(),
                        canon_claude.display(),
                        expected.display(),
                    ));
                }
                std::fs::remove_dir_all(&claude_dir).map_err(|e| {
                    format!("rm -rf {}: {}", claude_dir.display(), e)
                })?;
            }
            // Now do a fresh copy.
            let copied = copy_orchestrator_with_count(source, target)?;
            report.copied_count = copied;
        }
        ConflictStrategy::OverwriteAll => {
            let copied = copy_orchestrator_with_count(source, target)?;
            report.copied_count = copied;
        }
        ConflictStrategy::OverwritePreserve => {
            // Build a Set-like vector of preserve paths (relative to
            // install root). Dedup to avoid double-handling.
            let mut preserve: Vec<String> = preserve_paths.to_vec();
            preserve.sort();
            preserve.dedup();

            let mut copied = 0usize;
            let mut preserved_present: Vec<String> = Vec::new();
            let mut new_files_written = 0usize;

            // Iterate the orchestrator-managed allowlist. For each entry:
            //  - if it's a directory, recurse and apply preserve-aware copy.
            //  - if it's a file, apply preserve-aware copy directly.
            for managed in ORCHESTRATOR_MANAGED_PATHS.iter() {
                let src = source.join(managed);
                let dst = target.join(managed);
                if !src.exists() {
                    continue;
                }
                copied += copy_recursive_preserve_sync(
                    &src,
                    &dst,
                    target,
                    &preserve,
                    &mut preserved_present,
                    &mut new_files_written,
                )
                .map_err(|e| {
                    format!("copy {} -> {}: {}", src.display(), dst.display(), e)
                })?;
            }

            report.copied_count = copied;
            report.preserved_count = preserved_present.len();
            report.new_md_count = new_files_written;

            // Append/refresh notification block. CONTEXT_STATE.md is in
            // the preserve list so it is guaranteed to either already
            // exist OR have just been freshly copied (if the user didn't
            // have one) — either way it's safe to append.
            let context_state = target.join(".claude").join("CONTEXT_STATE.md");
            let notification_written = update_merge_notification_block(
                &context_state,
                &preserved_present,
            )
            .map_err(|e| {
                format!(
                    "writing notification block to {}: {}",
                    context_state.display(),
                    e
                )
            })?;
            report.notification_written = notification_written;
        }
    }

    Ok(report)
}

/// Convenience wrapper that returns a copy count alongside the
/// existing `copy_orchestrator_to_sync` semantics. Used by strategies
/// that overwrite-all so the report can show how many files moved.
pub(crate) fn copy_orchestrator_with_count(source: &Path, target: &Path) -> Result<usize, String> {
    let mut count = 0usize;
    for managed in ORCHESTRATOR_MANAGED_PATHS.iter() {
        let src = source.join(managed);
        let dst = target.join(managed);
        if !src.exists() {
            continue;
        }
        count += count_files_recursive(&src);
        copy_recursive_sync(&src, &dst).map_err(|e| {
            format!("copy {} -> {}: {}", src.display(), dst.display(), e)
        })?;
    }
    Ok(count)
}

pub(crate) fn count_files_recursive(p: &Path) -> usize {
    if p.is_file() {
        return 1;
    }
    if !p.is_dir() {
        return 0;
    }
    let mut total = 0usize;
    if let Ok(rd) = std::fs::read_dir(p) {
        for e in rd.flatten() {
            total += count_files_recursive(&e.path());
        }
    }
    total
}

/// Preserve-aware recursive copy used by `OverwritePreserve`.
///
/// For each FILE encountered:
///   - Compute the install-relative path (`dst` minus `install_root`).
///   - If that path is in `preserve`, AND a file already exists at
///     `dst`, write to `<dst>.new.<ext>` instead of overwriting and
///     record the original path in `preserved_present`.
///   - Otherwise, plain overwrite copy.
///
/// Symlinks are resolved (we copy file content) — same behaviour as the
/// non-preserve path. Returns the number of source files visited
/// (whether copied as-is or to a `.new.*` sibling).
pub(crate) fn copy_recursive_preserve_sync(
    src: &Path,
    dst: &Path,
    install_root: &Path,
    preserve: &[String],
    preserved_present: &mut Vec<String>,
    new_files_written: &mut usize,
) -> std::io::Result<usize> {
    let meta = std::fs::metadata(src)?;
    if meta.is_dir() {
        std::fs::create_dir_all(dst)?;
        let mut total = 0usize;
        for entry in std::fs::read_dir(src)? {
            let entry = entry?;
            let s = entry.path();
            let d = dst.join(entry.file_name());
            total += copy_recursive_preserve_sync(
                &s,
                &d,
                install_root,
                preserve,
                preserved_present,
                new_files_written,
            )?;
        }
        return Ok(total);
    }

    // It's a file. Compute install-relative path.
    let rel = match dst.strip_prefix(install_root) {
        Ok(r) => r.to_string_lossy().to_string(),
        Err(_) => {
            // Should never happen — dst is always rooted at install_root
            // by construction. Fall back to plain copy.
            if let Some(parent) = dst.parent() {
                std::fs::create_dir_all(parent)?;
            }
            std::fs::copy(src, dst)?;
            return Ok(1);
        }
    };

    let is_preserved = preserve.iter().any(|p| p == &rel);
    if is_preserved && dst.exists() {
        // Write to <dst>.new.<ext>; leave existing file untouched.
        let sibling = new_sibling_path(dst);
        if let Some(parent) = sibling.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::copy(src, &sibling)?;
        preserved_present.push(rel);
        *new_files_written += 1;
        return Ok(1);
    }

    // Plain copy.
    if let Some(parent) = dst.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::copy(src, dst)?;
    Ok(1)
}

