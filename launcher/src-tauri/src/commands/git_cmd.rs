// SPDX-License-Identifier: AGPL-3.0-or-later
// Part of VibeCoded Orchestrator.
//! The ONE home for the launcher's `git` invocations and for the questions it
//! asks git about a repo's currency.
//!
//! ## Why this module exists (v0.2.92, WP-13)
//!
//! Two independent subsystems answered the question "which branch is this
//! repo on?" and answered it DIFFERENTLY:
//!
//! * `installer.rs` — `check_for_updates`, `update_orchestrator`,
//!   `resolve_pull_branch`, and two more sites — each inlined
//!   `if b.is_empty() || b == "HEAD" { "main" } else { b }`. Five copies of
//!   one rule, correct in all five.
//! * `self_update.rs` — `current_branch` returned `git rev-parse
//!   --abbrev-ref HEAD` verbatim. Zero copies of the rule.
//!
//! In a detached HEAD, `--abbrev-ref` returns the literal string `"HEAD"`.
//! The installer surface normalised it to `main` and worked. The self-update
//! surface passed `"HEAD"` through into `HEAD..vco_upstream/HEAD` — a ref
//! that DOES NOT EXIST, because `ensure_upstream_remote` only ever runs
//! `remote add` / `set-url` and git's fetch never creates a remote HEAD
//! symref. git returned `fatal:`, and `.unwrap_or(0)` turned that into the
//! number zero, and zero meant "up to date".
//!
//! So: two subsystems, one question, opposite answers on the same repo. That
//! is a duplicated SYSTEM even though the two shared no lines of code — and
//! the divergence is what let a real install sit five weeks behind while
//! every surface said it was current. The fix is not "add the normalisation
//! to the sixth place"; it is that there is exactly ONE place.
//!
//! ## What lives here
//!
//! * [`run_git`] — spawn + capture + timeout, the production git runner.
//! * [`resolve_branch`] — the branch question, answered as a
//!   [`BranchState`] that says BOTH which branch to compare against AND
//!   whether HEAD is detached. The detached bit was previously destroyed by
//!   normalisation (`"HEAD"` → `"main"` loses the fact); surfaces need it to
//!   explain themselves and to offer the reattach affordance.
//! * [`commits_behind`] — `rev-list --count`, returning `Result` so callers
//!   must decide what to do about failure instead of receiving a `0`.
//! * [`remote_default_branch`] / [`latest_remote_tag`] — questions asked of
//!   the REMOTE rather than of local HEAD.
//!
//! ## What does NOT live here, deliberately
//!
//! `git remote set-head` is NOT called anywhere, and must not be added as a
//! "second fix" for the missing `vco_upstream/HEAD` ref. The normaliser IS
//! the fix; a second mechanism is a second thing to drift.

use std::ffi::OsStr;
use std::path::Path;
use std::time::Duration;

use tokio::process::Command as TokioCommand;
use vct_launcher_core::process::CommandExt as _;

/// Wall-clock ceiling for a single git invocation. Matches the value
/// `self_update.rs` used before the extraction — a network `ls-remote` on a
/// slow link is the long pole, and 30s is generous for it while still
/// bounded (an unbounded git hangs the daily check forever).
pub(crate) const GIT_TIMEOUT: Duration = Duration::from_secs(30);

/// Run `git <args>` in `repo`, returning trimmed stdout.
///
/// `Err` on: spawn failure, timeout, OR a non-zero exit (with trimmed stderr
/// in the message). A caller that wants to inspect a non-zero exit rather
/// than treat it as an error should not use this helper.
///
/// Output is captured, never inherited — `.silent()` also suppresses the
/// console window Windows would otherwise flash for every invocation.
pub(crate) async fn run_git(repo: &Path, args: &[&str]) -> Result<String, String> {
    let fut = TokioCommand::new("git")
        .silent()
        .args(args)
        .current_dir(repo)
        .output();
    let output = tokio::time::timeout(GIT_TIMEOUT, fut)
        .await
        .map_err(|_| format!("git {} timed out", args.join(" ")))?
        .map_err(|e| format!("git {} failed: {}", args.join(" "), e))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(format!("git {}: {}", args.join(" "), stderr.trim()));
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

/// Like [`run_git`] but on FAILURE returns the COMBINED stdout+stderr (and
/// forces `LC_ALL=C` so git emits C-locale English wording).
///
/// v0.2.71 (BLOCKER-1): git writes `CONFLICT (...)` lines to STDOUT, not
/// stderr, so the plain stderr-only error made `is_merge_conflict` silently
/// miss a real merge conflict — the pull error looked generic and dead-ended
/// at a raw toast while leaving `.git/MERGE_HEAD` on disk. This helper feeds
/// the shared `is_pull_conflict` classifier BOTH streams. The `LC_ALL=C` pin
/// matches the classifier's English-substring assumption (LOW-4).
///
/// Success return is identical to [`run_git`] (trimmed stdout).
pub(crate) async fn run_git_combined(repo: &Path, args: &[&str]) -> Result<String, String> {
    let fut = TokioCommand::new("git")
        .silent()
        .args(args)
        .env("LC_ALL", "C")
        .current_dir(repo)
        .output();
    let output = tokio::time::timeout(GIT_TIMEOUT, fut)
        .await
        .map_err(|_| format!("git {} timed out", args.join(" ")))?
        .map_err(|e| format!("git {} failed: {}", args.join(" "), e))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        let stdout = String::from_utf8_lossy(&output.stdout);
        return Err(format!(
            "git {}: {}\n{}",
            args.join(" "),
            stderr.trim(),
            stdout.trim()
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

/// Spawn git and hand back the RAW [`std::process::Output`].
///
/// Contract deliberately differs from [`run_git`]: `Err` is a SPAWN failure
/// only — a non-zero git exit comes back as `Ok(output)` for the caller to
/// judge. Callers that need to branch on a specific exit code, or that read
/// stdout on failure, use this; everything else uses [`run_git`].
///
/// Deliberately UNTIMED, unlike [`run_git`]: the callers that need raw
/// `Output` include `git pull` / `git rebase` on a full clone, and a 30s
/// ceiling on those would abort a legitimately slow update mid-write. A
/// caller that wants the ceiling uses [`run_git`].
///
/// v0.2.92 (MAJOR-7): generic over `AsRef<OsStr>` rather than fixed to
/// `&[&str]`, so a call site holding a `PathBuf`/`OsStr` argument (staging a
/// merged file by path) can route through the runner instead of keeping a
/// raw `Command::new("git")` next to it. Lossy `to_string_lossy` conversion
/// at such a site would be a real behaviour change on a non-UTF-8 path; a
/// second raw spawn is a worse one.
pub(crate) async fn run_git_raw<S: AsRef<OsStr>>(
    repo: &Path,
    args: &[S],
) -> Result<std::process::Output, String> {
    run_git_raw_env(repo, args, &[]).await
}

/// [`run_git_raw`] plus explicit environment overrides.
///
/// Exists for the two `GIT_EDITOR=true` sites (`git commit --no-edit` /
/// `git rebase --continue` after a one-click conflict resolution): a Tauri
/// subprocess has no controlling tty, so git's editor must be short-circuited
/// or the command hangs forever. Expressing that as a runner parameter is what
/// lets those sites stop constructing their own `Command`.
pub(crate) async fn run_git_raw_env<S: AsRef<OsStr>>(
    repo: &Path,
    args: &[S],
    envs: &[(&str, &str)],
) -> Result<std::process::Output, String> {
    let mut cmd = TokioCommand::new("git").silent();
    cmd.args(args).current_dir(repo);
    for (key, value) in envs {
        cmd.env(key, value);
    }
    cmd.output()
        .await
        .map_err(|e| format!("git {} spawn failed: {}", first_arg(args), e))
}

/// The first argument, lossily, for error messages only. Matches the
/// pre-generic `args.first().unwrap_or(&"")` shape.
fn first_arg<S: AsRef<OsStr>>(args: &[S]) -> String {
    args.first()
        .map(|a| a.as_ref().to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// The answer to "which branch, and is HEAD attached to it?".
///
/// `name` is always usable as the right-hand side of a
/// `<remote>/<name>` ref — never the literal `"HEAD"`, never empty. When
/// HEAD is detached (or the branch name is unreadable) `name` falls back to
/// [`FALLBACK_BRANCH`] and `detached` records that the fallback happened for
/// the detached reason.
///
/// Keeping both fields is the point. The old five inline normalisations were
/// each correct AND each threw away the detached bit, so no surface could
/// say "you are on a detached HEAD" — it could only say `Branch: main`,
/// which is a true-ish statement about a repo the user cannot push, pull
/// with tracking, or reason about.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct BranchState {
    /// Branch to compare/pull against. Never `"HEAD"`, never empty.
    pub name: String,
    /// `true` when HEAD points at a commit rather than a branch.
    pub detached: bool,
}

/// The branch every surface falls back to when HEAD names none.
///
/// `main` and not the remote's actual default: resolving the remote default
/// costs a network round-trip on a path (`check_for_updates` at startup)
/// that must stay fast and must work offline. [`remote_default_branch`]
/// exists for callers that can afford to ask; this constant is what the
/// offline/fast path uses, and it matches what every one of the five deleted
/// inline copies used.
pub(crate) const FALLBACK_BRANCH: &str = "main";

/// Resolve [`BranchState`] for `repo`.
///
/// `Err` only when git itself could not be run (spawn failure/timeout) — a
/// repo whose HEAD is unreadable for any other reason yields
/// `Ok(BranchState { name: "main", detached: false })`, matching the
/// pre-extraction behaviour of all five installer.rs sites (which returned
/// `"main"` on a non-zero exit).
///
/// Note the asymmetry, which is intentional: `detached` is only ever `true`
/// when git POSITIVELY reported the detached shape (`HEAD` or empty from
/// `--abbrev-ref`). A git we could not run tells us nothing about
/// attachment, so we do not claim it.
pub(crate) async fn resolve_branch(repo: &Path) -> Result<BranchState, String> {
    let raw = match run_git(repo, &["rev-parse", "--abbrev-ref", "HEAD"]).await {
        Ok(s) => s,
        Err(e) => {
            // Distinguish "git is broken/absent" (propagate) from "this repo's
            // HEAD is unreadable" (fall back, as the five inline copies did).
            // A spawn failure or timeout is the former; a non-zero exit —
            // e.g. an unborn HEAD in a freshly `git init`ed repo — is the
            // latter.
            if e.contains("timed out") || e.contains("failed:") {
                return Err(e);
            }
            return Ok(BranchState {
                name: FALLBACK_BRANCH.to_string(),
                detached: false,
            });
        }
    };
    let b = raw.trim();
    // `--abbrev-ref` prints the literal `HEAD` for a detached HEAD. That is
    // git's documented behaviour, not an error, which is exactly why an
    // `unwrap_or_else(|_| "main")` on the Result never fired for it.
    if b.is_empty() || b == "HEAD" {
        return Ok(BranchState {
            name: FALLBACK_BRANCH.to_string(),
            detached: true,
        });
    }
    Ok(BranchState {
        name: b.to_string(),
        detached: false,
    })
}

/// Count how many commits `HEAD` is BEHIND `<remote>/<branch>` —
/// `git rev-list --count HEAD..<remote>/<branch>`. Requires a prior fetch.
///
/// Returns `Result`, and every caller must handle the `Err` arm explicitly.
/// This signature is load-bearing: the incident this module exists to
/// prevent was produced by `.unwrap_or(0)` on exactly this call, which made
/// "git refused to answer" and "you are current" the same u32.
pub(crate) async fn commits_behind(
    repo: &Path,
    remote: &str,
    branch: &str,
) -> Result<u32, String> {
    let raw = run_git(
        repo,
        &["rev-list", "--count", &format!("HEAD..{remote}/{branch}")],
    )
    .await?;
    raw.parse::<u32>()
        .map_err(|e| format!("count parse failed (raw={raw:?}): {e}"))
}

/// Ask the REMOTE which branch its `HEAD` points at, via
/// `git ls-remote --symref <remote> HEAD`.
///
/// Output shape (verified empirically):
///
/// ```text
/// ref: refs/heads/main	HEAD
/// 08897d00…	HEAD
/// ```
///
/// Returns the short branch name (`main`). `Ok(None)` when the remote
/// answered but advertised no symref (an old server, or a remote whose HEAD
/// is unborn); `Err` when the remote could not be reached at all — the
/// caller decides which of those two is acceptable for its surface.
#[allow(dead_code)] // consumed by the reattach guard chain + future callers
pub(crate) async fn remote_default_branch(
    repo: &Path,
    remote: &str,
) -> Result<Option<String>, String> {
    let out = run_git(repo, &["ls-remote", "--symref", remote, "HEAD"]).await?;
    for line in out.lines() {
        let line = line.trim();
        if let Some(rest) = line.strip_prefix("ref: ") {
            // `refs/heads/main\tHEAD`
            let refname = rest.split_whitespace().next().unwrap_or("");
            if let Some(short) = refname.strip_prefix("refs/heads/") {
                if !short.is_empty() {
                    return Ok(Some(short.to_string()));
                }
            }
        }
    }
    Ok(None)
}

/// The newest release tag ON THE REMOTE, by version order.
///
/// **This is not `git describe`.** `git describe --tags --abbrev=0` answers
/// "what is the closest tag reachable FROM HEAD", which is a different
/// question with a dangerously plausible answer: an install detached on
/// `v0.2.88` gets told the latest source release is `v0.2.88`. Truthful, and
/// the single line that most directly produced "everything reported healthy"
/// during the five-week outage.
///
/// Implementation: `git ls-remote --tags --refs --sort=-v:refname <remote>`
/// and take the first row. `--refs` drops the `^{}` peeled duplicates;
/// `-v:refname` is git's own version sort (verified: `v0.0.10` sorts above
/// `v0.0.2`, which a lexicographic sort gets backwards).
///
/// `Ok(None)` when the remote has no tags. `Err` when the remote could not
/// be reached — the caller renders "couldn't check", never its own tag.
pub(crate) async fn latest_remote_tag(
    repo: &Path,
    remote: &str,
) -> Result<Option<String>, String> {
    let out = run_git(
        repo,
        &["ls-remote", "--tags", "--refs", "--sort=-v:refname", remote],
    )
    .await?;
    for line in out.lines() {
        // `<sha>\trefs/tags/<name>`
        if let Some(refname) = line.split('\t').nth(1) {
            if let Some(tag) = refname.trim().strip_prefix("refs/tags/") {
                if !tag.is_empty() {
                    return Ok(Some(tag.to_string()));
                }
            }
        }
    }
    Ok(None)
}

/// `true` when the working tree has no modified/staged/untracked entries.
///
/// Used as a guard in front of the launcher's only path-less
/// `git checkout <branch>` (the reattach affordance). Untracked files count:
/// a checkout that would overwrite one aborts, and a guard that lets the
/// user reach an abort has not guarded anything.
pub(crate) async fn tree_is_clean(repo: &Path) -> Result<bool, String> {
    let out = run_git(repo, &["status", "--porcelain"]).await?;
    Ok(out.trim().is_empty())
}

/// `true` when `commit` is an ancestor of `other` —
/// `git merge-base --is-ancestor <commit> <other>` (exit 0 = yes, 1 = no).
///
/// A non-zero-that-is-not-1 (bad ref, broken repo) comes back as `Err`, so
/// "I could not tell" is never silently the same as "no".
pub(crate) async fn is_ancestor(
    repo: &Path,
    commit: &str,
    other: &str,
) -> Result<bool, String> {
    let output = tokio::time::timeout(
        GIT_TIMEOUT,
        TokioCommand::new("git")
            .silent()
            .args(["merge-base", "--is-ancestor", commit, other])
            .current_dir(repo)
            .output(),
    )
    .await
    .map_err(|_| "git merge-base --is-ancestor timed out".to_string())?
    .map_err(|e| format!("git merge-base --is-ancestor failed: {e}"))?;

    match output.status.code() {
        Some(0) => Ok(true),
        Some(1) => Ok(false),
        _ => Err(format!(
            "git merge-base --is-ancestor {commit} {other}: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::process::{Command as StdCommand, Stdio};

    macro_rules! skip_if_no_git {
        () => {
            if StdCommand::new("git")
                .arg("--version")
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .map(|s| !s.success())
                .unwrap_or(true)
            {
                eprintln!("skipping: git not on PATH");
                return;
            }
        };
    }

    fn git(cwd: &Path, args: &[&str]) {
        let st = StdCommand::new("git")
            .args(args)
            .current_dir(cwd)
            // Keep the developer's ~/.gitconfig (hooks, signing, templates,
            // init.defaultBranch) out of the fixture entirely.
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_CONFIG_SYSTEM", "/dev/null")
            .env("GIT_AUTHOR_NAME", "T")
            .env("GIT_AUTHOR_EMAIL", "t@example.com")
            .env("GIT_COMMITTER_NAME", "T")
            .env("GIT_COMMITTER_EMAIL", "t@example.com")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .unwrap_or_else(|e| panic!("git {args:?}: {e}"));
        assert!(st.success(), "git {args:?} failed in {}", cwd.display());
    }

    /// Build the fixture that reproduces the FIELD shape, not a convenient
    /// one. Two properties are load-bearing and easy to get wrong:
    ///
    /// 1. the local repo is created with `init` + `remote add` + `fetch`,
    ///    NEVER `clone` — because `clone` creates
    ///    `refs/remotes/<remote>/HEAD` and the production
    ///    `ensure_upstream_remote` (which only ever runs `remote add` /
    ///    `set-url`) does not. A `clone`-based fixture makes
    ///    `HEAD..vco_upstream/HEAD` resolve, and would have passed against
    ///    the very code that shipped the outage;
    /// 2. HEAD is left DETACHED on the first tag with the remote two commits
    ///    ahead — the exact state the field install sat in.
    ///
    /// Returns (tempdir, local repo path, remote name).
    pub(super) fn detached_fixture() -> (tempfile::TempDir, PathBuf, &'static str) {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path().to_path_buf();
        let remote = root.join("remote.git");
        let seed = root.join("seed");
        let local = root.join("local");
        std::fs::create_dir_all(&seed).unwrap();
        std::fs::create_dir_all(&local).unwrap();

        git(&root, &["init", "--bare", "--initial-branch=main", "-q", "remote.git"]);

        git(&seed, &["init", "--initial-branch=main", "-q"]);
        std::fs::write(seed.join("a.txt"), "a\n").unwrap();
        git(&seed, &["add", "-A"]);
        git(&seed, &["commit", "-qm", "c1"]);
        git(&seed, &["tag", "v0.0.1"]);
        git(&seed, &["remote", "add", "vco_upstream", remote.to_str().unwrap()]);
        git(&seed, &["push", "-q", "vco_upstream", "main", "--tags"]);

        // Local: init + remote add + fetch. NOT clone. (See doc comment.)
        git(&local, &["init", "--initial-branch=main", "-q"]);
        git(&local, &["remote", "add", "vco_upstream", remote.to_str().unwrap()]);
        git(&local, &["fetch", "-q", "vco_upstream"]);
        git(&local, &["checkout", "-q", "-B", "main", "vco_upstream/main"]);

        // Upstream moves two commits ahead, adding a Rust file and a
        // frontend file so rebuild-gating tests have real inputs, plus a
        // newer tag so "latest release" has something to be wrong about.
        std::fs::create_dir_all(seed.join("launcher/src-tauri/src")).unwrap();
        std::fs::write(seed.join("launcher/src-tauri/src/main.rs"), "fn main() {}\n").unwrap();
        git(&seed, &["add", "-A"]);
        git(&seed, &["commit", "-qm", "c2"]);
        std::fs::create_dir_all(seed.join("launcher/src")).unwrap();
        std::fs::write(seed.join("launcher/src/app.ts"), "export {};\n").unwrap();
        git(&seed, &["add", "-A"]);
        git(&seed, &["commit", "-qm", "c3"]);
        git(&seed, &["tag", "v0.0.2"]);
        git(&seed, &["push", "-q", "vco_upstream", "main", "--tags"]);

        git(&local, &["fetch", "-q", "vco_upstream", "--tags"]);
        git(&local, &["checkout", "-q", "--detach", "v0.0.1"]);

        (tmp, local, "vco_upstream")
    }

    /// THE regression test for the field incident's root cause. Against the
    /// pre-fix `current_branch` this asserts the opposite of what shipped:
    /// that surface returned the literal `"HEAD"`, which then produced a ref
    /// that does not exist.
    #[tokio::test]
    async fn resolve_branch_detached_head_normalises_and_flags() {
        skip_if_no_git!();
        let (_tmp, repo, _remote) = detached_fixture();

        // Precondition: the fixture really is in the broken shape. If this
        // ever fails the test below is proving nothing.
        let raw = run_git(&repo, &["rev-parse", "--abbrev-ref", "HEAD"])
            .await
            .expect("rev-parse");
        assert_eq!(raw, "HEAD", "fixture is not in a detached HEAD");

        let st = resolve_branch(&repo).await.expect("resolve_branch");
        assert_eq!(st.name, "main", "detached HEAD must normalise to main");
        assert!(st.detached, "detached HEAD must be FLAGGED, not just normalised");
    }

    #[tokio::test]
    async fn resolve_branch_attached_reports_the_real_branch_not_detached() {
        skip_if_no_git!();
        let (_tmp, repo, _remote) = detached_fixture();
        git(&repo, &["checkout", "-q", "main"]);

        let st = resolve_branch(&repo).await.expect("resolve_branch");
        assert_eq!(st.name, "main");
        assert!(!st.detached, "an attached HEAD must not be flagged detached");
    }

    #[tokio::test]
    async fn resolve_branch_reports_a_non_main_branch_verbatim() {
        skip_if_no_git!();
        let (_tmp, repo, _remote) = detached_fixture();
        git(&repo, &["checkout", "-q", "-b", "feature/x"]);

        let st = resolve_branch(&repo).await.expect("resolve_branch");
        assert_eq!(
            st.name, "feature/x",
            "a real branch must survive the normaliser untouched"
        );
        assert!(!st.detached);
    }

    /// The missing-ref failure has to reach the caller as `Err`. This is the
    /// exact call whose `.unwrap_or(0)` produced the outage.
    #[tokio::test]
    async fn commits_behind_errors_on_a_ref_that_does_not_exist() {
        skip_if_no_git!();
        let (_tmp, repo, remote) = detached_fixture();

        // `<remote>/HEAD` does NOT exist under `remote add` + `fetch` — this
        // is the production shape, and asking for it is what self_update.rs
        // did in a detached HEAD.
        let err = commits_behind(&repo, remote, "HEAD")
            .await
            .expect_err("HEAD..<remote>/HEAD must be an error, not a count");
        assert!(
            err.contains("rev-list"),
            "the error must name the failing git command, got: {err}"
        );
    }

    #[tokio::test]
    async fn commits_behind_counts_correctly_against_the_resolved_branch() {
        skip_if_no_git!();
        let (_tmp, repo, remote) = detached_fixture();
        let st = resolve_branch(&repo).await.expect("resolve_branch");

        let n = commits_behind(&repo, remote, &st.name)
            .await
            .expect("count via the RESOLVED branch must succeed even when detached");
        assert_eq!(n, 2, "fixture puts upstream exactly two commits ahead");
    }

    #[tokio::test]
    async fn latest_remote_tag_reads_the_remote_not_head() {
        skip_if_no_git!();
        let (_tmp, repo, remote) = detached_fixture();

        // What the OLD implementation would have said: HEAD is detached on
        // v0.0.1, so `describe` returns the install's own tag.
        let describe = run_git(&repo, &["describe", "--tags", "--abbrev=0"])
            .await
            .expect("describe");
        assert_eq!(describe, "v0.0.1", "fixture precondition");

        let tag = latest_remote_tag(&repo, remote)
            .await
            .expect("ls-remote")
            .expect("remote has tags");
        assert_eq!(
            tag, "v0.0.2",
            "the newest REMOTE tag, not the newest tag reachable from HEAD"
        );
    }

    #[tokio::test]
    async fn latest_remote_tag_sorts_by_version_not_lexicographically() {
        skip_if_no_git!();
        let (_tmp, repo, remote) = detached_fixture();
        // v0.0.10 sorts BELOW v0.0.2 lexicographically and ABOVE it by
        // version. Push it and require the version answer.
        let seed = repo.parent().unwrap().join("seed");
        git(&seed, &["tag", "v0.0.10"]);
        git(&seed, &["push", "-q", "vco_upstream", "--tags"]);

        let tag = latest_remote_tag(&repo, remote)
            .await
            .expect("ls-remote")
            .expect("tags");
        assert_eq!(tag, "v0.0.10", "must use git's version sort");
    }

    #[tokio::test]
    async fn latest_remote_tag_is_none_when_the_remote_has_no_tags() {
        skip_if_no_git!();
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path();
        let bare = root.join("bare.git");
        let local = root.join("local");
        std::fs::create_dir_all(&local).unwrap();
        git(root, &["init", "--bare", "--initial-branch=main", "-q", "bare.git"]);
        git(&local, &["init", "--initial-branch=main", "-q"]);
        git(&local, &["remote", "add", "up", bare.to_str().unwrap()]);

        assert_eq!(
            latest_remote_tag(&local, "up").await.expect("ls-remote"),
            None,
            "a tagless remote is Ok(None), not an error and not a fabricated tag"
        );
    }

    #[tokio::test]
    async fn remote_default_branch_reads_the_symref() {
        skip_if_no_git!();
        let (_tmp, repo, remote) = detached_fixture();
        assert_eq!(
            remote_default_branch(&repo, remote).await.expect("symref"),
            Some("main".to_string())
        );
    }

    #[tokio::test]
    async fn is_ancestor_distinguishes_yes_no_and_broken() {
        skip_if_no_git!();
        let (_tmp, repo, remote) = detached_fixture();

        assert!(
            is_ancestor(&repo, "HEAD", &format!("{remote}/main"))
                .await
                .expect("ancestor probe"),
            "the detached tag IS an ancestor of the upstream tip"
        );

        // An orphan commit shares no history with upstream.
        git(&repo, &["checkout", "-q", "--orphan", "orphanwork"]);
        std::fs::write(repo.join("z.txt"), "z\n").unwrap();
        git(&repo, &["add", "-A"]);
        git(&repo, &["commit", "-qm", "orphan"]);
        assert!(
            !is_ancestor(&repo, "HEAD", &format!("{remote}/main"))
                .await
                .expect("ancestor probe"),
            "an orphan commit is NOT an ancestor"
        );

        // A ref that does not resolve is neither yes nor no.
        assert!(
            is_ancestor(&repo, "HEAD", "no_such_remote/main")
                .await
                .is_err(),
            "an unresolvable ref must be Err, never a silent `false`"
        );
    }

    #[tokio::test]
    async fn tree_is_clean_reflects_tracked_and_untracked_state() {
        skip_if_no_git!();
        let (_tmp, repo, _remote) = detached_fixture();
        assert!(tree_is_clean(&repo).await.expect("status"));

        std::fs::write(repo.join("a.txt"), "modified\n").unwrap();
        assert!(!tree_is_clean(&repo).await.expect("status"));

        std::fs::write(repo.join("a.txt"), "a\n").unwrap();
        assert!(tree_is_clean(&repo).await.expect("status"));

        std::fs::write(repo.join("brand-new.txt"), "x\n").unwrap();
        assert!(
            !tree_is_clean(&repo).await.expect("status"),
            "untracked files count — a checkout that would clobber one aborts"
        );
    }

    #[tokio::test]
    async fn run_git_surfaces_a_non_zero_exit_as_err_with_stderr() {
        skip_if_no_git!();
        let (_tmp, repo, _remote) = detached_fixture();
        let err = run_git(&repo, &["rev-parse", "definitely-not-a-ref"])
            .await
            .expect_err("bad ref must be Err");
        assert!(err.starts_with("git rev-parse"), "got: {err}");
    }
}
