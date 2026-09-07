# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-14 — the doctor can see a silent update outage.

THE FAILURE THIS CLOSES
-----------------------
``vco doctor`` is positioned as the authoritative post-update health check.
It probed npx, MCP spawnability, the ledger, prereqs, npm pins, disk space and
``vco_lib``'s import origin — and had **no probe for detached HEAD, no
HEAD-vs-upstream distance, and no "when did an update last succeed"**. A field
install therefore sat five weeks behind upstream, in a detached HEAD, while
every surface reported health. ``probe_launcher_binary_fresh`` even returned
``ok``, because it compares the running binary to *the tree's* binary and on a
frozen clone both are the same frozen version: the correct answer to the wrong
question.

THE FIXTURE IS BUILT WITH ``init`` + ``remote add`` + ``fetch``, NEVER ``clone``
------------------------------------------------------------------------------
``git clone`` creates ``refs/remotes/<remote>/HEAD``. Production never does:
``ensure_upstream_remote`` only ever runs ``remote add`` / ``set-url``, and
``git fetch`` does not create a remote HEAD symref. A clone-based fixture makes
``HEAD..vco_upstream/HEAD`` resolve — so it would have PASSED against the very
code that shipped the five-week outage. The double must reproduce the
environment's DEFICIENCY, not the textbook setup.

EVERY PROBE MUST REACH THREE OUTCOMES
-------------------------------------
Each question below is asserted in all three: the problem (act), the healthy
case (leave alone), and **the undetermined case** — a probe that cannot say
"I could not determine this" is not a probe, and every laundering in the
incident (``unwrap_or(0)``, ``unwrap_or_default()``, ``describe`` against HEAD)
was a missing third state.
"""
from __future__ import annotations

import json
import ntpath
import os
import posixpath
import re
import shutil
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import doctor  # noqa: E402

GIT = shutil.which("git")


# ---------------------------------------------------------------------------
# Fixture — a real git world, hermetic and offline
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    """Run git in ``cwd`` with the developer's own config fully neutralised.

    ``GIT_CONFIG_GLOBAL``/``SYSTEM`` point at a path that does not exist rather
    than at ``/dev/null`` — the latter is a POSIX-only device name and this
    suite must behave identically on the Windows runner.
    """
    env = dict(os.environ)
    env.update(
        {
            "GIT_CONFIG_GLOBAL": str(cwd / "_no_such_gitconfig"),
            "GIT_CONFIG_SYSTEM": str(cwd / "_no_such_gitconfig"),
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    proc = subprocess.run(
        [GIT or "git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args} failed in {cwd}: {proc.stderr}")
    return (proc.stdout or "").strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    # Pin the branch name without relying on `git init -b` (not in older git)
    # or on the developer's `init.defaultBranch`.
    _git(path, "symbolic-ref", "HEAD", "refs/heads/main")


def _make_orchestrator_shape(root: Path) -> None:
    """The two markers ``looks_like_orchestrator_root`` requires."""
    (root / "vco_lib").mkdir(parents=True, exist_ok=True)
    (root / ".claude").mkdir(parents=True, exist_ok=True)
    (root / "vco_lib" / "__init__.py").write_text("", encoding="utf-8")


class GitWorld:
    """An upstream repo plus a local checkout wired to it as ``vco_upstream``."""

    def __init__(self, base: Path):
        self.upstream = base / "upstream.git"
        self.local = base / "clone"
        _init_repo(self.upstream)
        _make_orchestrator_shape(self.upstream)
        self.tip = _commit(self.upstream, "one")

        # NEVER `git clone` — see the module docstring.
        _init_repo(self.local)
        _make_orchestrator_shape(self.local)
        _git(self.local, "remote", "add", doctor.UPSTREAM_REMOTE, str(self.upstream))
        _git(self.local, "fetch", "-q", doctor.UPSTREAM_REMOTE, "main")
        _git(self.local, "reset", "-q", "--hard", f"{doctor.UPSTREAM_REMOTE}/main")
        _git(self.local, "branch", "--set-upstream-to",
             f"{doctor.UPSTREAM_REMOTE}/main", "main", check=False)

    def advance_upstream(self, n: int = 1) -> str:
        for i in range(n):
            self.tip = _commit(self.upstream, f"up-{time.time_ns()}-{i}")
        return self.tip

    def fetch(self) -> None:
        _git(self.local, "fetch", "-q", doctor.UPSTREAM_REMOTE, "main")

    def detach(self) -> None:
        sha = _git(self.local, "rev-parse", "HEAD")
        _git(self.local, "checkout", "-q", "--detach", sha)


@unittest.skipIf(GIT is None, "git is not on PATH")
class RealGitFixtureTests(unittest.TestCase):
    """Red-proof against a REAL repo — the shape production actually has."""

    def test_the_fixture_has_no_remote_HEAD_symref(self):
        """The property that makes this fixture honest.

        `git clone` would create `refs/remotes/vco_upstream/HEAD`, which
        production's `ensure_upstream_remote` never creates. With it present,
        `HEAD..vco_upstream/HEAD` RESOLVES — and a suite built that way passes
        against the exact code that shipped the outage.
        """
        with TemporaryDirectory() as td:
            world = GitWorld(Path(td))
            refs = _git(world.local, "for-each-ref", "--format=%(refname)")
            self.assertIn("refs/remotes/vco_upstream/main", refs)
            self.assertNotIn("refs/remotes/vco_upstream/HEAD", refs)
            rc = subprocess.run(
                [GIT or "git", "-C", str(world.local), "rev-list", "--count",
                 "HEAD..vco_upstream/HEAD"],
                capture_output=True, text=True, check=False,
            ).returncode
            self.assertNotEqual(
                rc, 0,
                "the deficient ref must be unresolvable, as it is in the field",
            )

    # ── act: the incident's own shape ───────────────────────────────────

    def test_detached_and_behind_reports_both_facts(self):
        with TemporaryDirectory() as td:
            world = GitWorld(Path(td))
            world.advance_upstream(2)
            world.fetch()          # the launcher fetches; the user never merges
            world.detach()
            report = doctor.run_doctor(world.local)
            head = self._one(report, "head_attached")
            currency = self._one(report, "source_currency")
        self.assertEqual(head.status, doctor.STATUS_PROBLEM)
        self.assertIn("DETACHED", head.summary)
        self.assertEqual(currency.status, doctor.STATUS_PROBLEM)
        self.assertIn("2 commit(s) behind", currency.summary)

    def test_a_stale_checkout_is_told_how_long_it_has_been_stale(self):
        """The incident's own sentence needs BOTH halves.

        "You are 59 behind" invites "so I will update later"; "and your last
        completed update was 38 days ago" is what says the updates are not
        arriving. Neither finding carries it alone, so the age rides the
        verdict that makes it actionable.
        """
        with TemporaryDirectory() as td:
            world = GitWorld(Path(td))
            world.advance_upstream(2)
            world.fetch()
            logs = world.local / "state" / "logs"
            logs.mkdir(parents=True)
            old = (datetime.now(timezone.utc) - timedelta(days=38)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            (logs / "install.jsonl").write_text(
                json.dumps({"ts": old, "step": "session", "phase": "ok",
                            "data": {"mode": "update"}}) + "\n",
                encoding="utf-8")
            report = doctor.run_doctor(world.local)
            currency = self._one(report, "source_currency")
        self.assertEqual(currency.status, doctor.STATUS_PROBLEM)
        self.assertIn("2 commit(s) behind", currency.summary)
        self.assertIn("38 day(s) ago", currency.summary)
        self.assertAlmostEqual(currency.detail["last_install_age_days"], 38, delta=1)

    def test_a_healthy_checkout_is_not_told_its_install_age(self):
        """Not decision-relevant when there is nothing to install, and every
        line the report does not need is a line that makes it less read."""
        with TemporaryDirectory() as td:
            world = GitWorld(Path(td))
            logs = world.local / "state" / "logs"
            logs.mkdir(parents=True)
            old = (datetime.now(timezone.utc) - timedelta(days=400)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            (logs / "install.jsonl").write_text(
                json.dumps({"ts": old, "step": "session", "phase": "ok"}) + "\n",
                encoding="utf-8")
            report = doctor.run_doctor(world.local)
            currency = self._one(report, "source_currency")
        self.assertEqual(currency.status, doctor.STATUS_OK)
        self.assertNotIn("day(s) ago", currency.summary)

    def test_behind_without_the_objects_is_still_a_positive_problem(self):
        """The genuinely-stale shape: upstream moved and we never fetched.

        `merge-base --is-ancestor` cannot run — the object is not here. Its
        ABSENCE is the evidence: a commit we do not have is not one we
        contain. Anything less would report `unknown` for the commonest form
        of the failure.
        """
        with TemporaryDirectory() as td:
            world = GitWorld(Path(td))
            world.advance_upstream(3)   # NO fetch
            report = doctor.run_doctor(world.local)
            currency = self._one(report, "source_currency")
        self.assertEqual(currency.status, doctor.STATUS_PROBLEM)
        self.assertIn("does not contain", currency.summary)
        self.assertFalse(currency.detail["contains_remote_tip"])

    # ── leave alone: a healthy checkout ─────────────────────────────────

    def test_current_and_attached_is_ok(self):
        with TemporaryDirectory() as td:
            world = GitWorld(Path(td))
            report = doctor.run_doctor(world.local)
            head = self._one(report, "head_attached")
            currency = self._one(report, "source_currency")
        self.assertEqual(head.status, doctor.STATUS_OK)
        self.assertIn("main", head.summary)
        self.assertEqual(currency.status, doctor.STATUS_OK)
        self.assertIn("current with", currency.summary)

    def test_local_commits_on_top_are_not_staleness(self):
        """A developer ahead of upstream is CURRENT, not behind.

        Without this arm the honest-looking rule "HEAD sha != remote sha ⇒
        stale" would fire on every maintainer machine, and a report that cries
        wolf is one nobody reads.
        """
        with TemporaryDirectory() as td:
            world = GitWorld(Path(td))
            _commit(world.local, "mine")
            report = doctor.run_doctor(world.local)
            currency = self._one(report, "source_currency")
        self.assertEqual(currency.status, doctor.STATUS_OK)
        self.assertIn("local commits on top", currency.summary)

    # ── unknown: the third state, reached three different ways ──────────

    def test_no_upstream_remote_is_unknown_not_ok(self):
        with TemporaryDirectory() as td:
            world = GitWorld(Path(td))
            _git(world.local, "remote", "remove", doctor.UPSTREAM_REMOTE)
            report = doctor.run_doctor(world.local)
            currency = self._one(report, "source_currency")
        self.assertEqual(currency.status, doctor.STATUS_UNKNOWN)
        self.assertIn("no `vco_upstream` remote", currency.summary)

    def test_unreachable_remote_is_unknown_not_ok(self):
        with TemporaryDirectory() as td:
            world = GitWorld(Path(td))
            _git(world.local, "remote", "set-url", doctor.UPSTREAM_REMOTE,
                 str(Path(td) / "gone.git"))
            report = doctor.run_doctor(world.local)
            currency = self._one(report, "source_currency")
        self.assertEqual(currency.status, doctor.STATUS_UNKNOWN)
        self.assertIn("could not be determined", currency.summary)

    def test_a_branch_absent_upstream_is_named_not_blamed(self):
        """A local feature branch has no upstream tip to compare against.

        `ls-remote --exit-code` returns 2 for "no matching refs" — a POSITIVE
        answer, and a different one from "the remote could not be reached".
        Collapsing the two would tell a user on a working branch that their
        network is broken.
        """
        with TemporaryDirectory() as td:
            world = GitWorld(Path(td))
            _git(world.local, "checkout", "-q", "-b", "my-feature")
            report = doctor.run_doctor(world.local)
            currency = self._one(report, "source_currency")
        self.assertEqual(currency.status, doctor.STATUS_UNKNOWN)
        self.assertIn("does not exist on vco_upstream", currency.summary)

    def test_a_stale_local_ref_may_convict_even_when_the_remote_is_gone(self):
        """Being behind a ref we ALREADY HAVE is not in doubt.

        The remote is unreachable, so `ok` is unavailable — but the local
        remote-tracking ref is positive evidence of staleness, and downgrading
        that to `unknown` would hide a fact we hold.
        """
        with TemporaryDirectory() as td:
            world = GitWorld(Path(td))
            world.advance_upstream(2)
            world.fetch()
            _git(world.local, "reset", "-q", "--hard", "HEAD~0")
            _git(world.local, "checkout", "-q", "-B", "main",
                 f"{doctor.UPSTREAM_REMOTE}/main~2")
            _git(world.local, "remote", "set-url", doctor.UPSTREAM_REMOTE,
                 str(Path(td) / "gone.git"))
            report = doctor.run_doctor(world.local)
            currency = self._one(report, "source_currency")
        self.assertEqual(currency.status, doctor.STATUS_PROBLEM)
        self.assertIn("at least 2 commit(s) behind", currency.summary)
        self.assertIn("could not be reached", currency.summary)

    # ── not applicable is a FOURTH state, distinct from unknown ─────────

    def test_a_folder_inside_another_repo_is_not_graded_as_that_repo(self):
        """`rev-parse --is-inside-work-tree` would say yes for the PARENT.

        Answering the currency question about an enclosing repository is a
        true statement about the wrong tree — the exact shape of every defect
        this release is closing.
        """
        with TemporaryDirectory() as td:
            world = GitWorld(Path(td))
            nested = world.local / "sub"
            _make_orchestrator_shape(nested)
            report = doctor.run_doctor(nested)
            currency = self._one(report, "source_currency")
        self.assertEqual(currency.status, doctor.STATUS_UNKNOWN)
        self.assertIn("not the root", str(currency.detail))

    def test_a_non_orchestrator_folder_gets_no_finding_at_all(self):
        with TemporaryDirectory() as td:
            plain = Path(td) / "userproject"
            (plain / ".claude").mkdir(parents=True)
            report = doctor.run_doctor(plain)
        self.assertEqual(
            [f for f in report.findings
             if f.probe in ("head_attached", "source_currency")],
            [],
            "a user project has no orchestrator checkout to grade",
        )

    def _one(self, report, probe):
        matches = [f for f in report.findings if f.probe == probe]
        self.assertEqual(len(matches), 1, f"expected exactly one {probe} finding")
        return matches[0]


@unittest.skipIf(GIT is None, "git is not on PATH")
class ReadOnlyContractTests(unittest.TestCase):
    def test_the_probe_never_mutates_the_repository(self):
        """`vco doctor` is a REPORT.

        The plan's sketch had the probe run `git fetch`, which writes refs and
        objects into the user's repo and can block on a credential prompt.
        `ls-remote` answers the same question read-only, so a health check can
        be run twice with confidence and can never inflate a repo it was asked
        to inspect.
        """
        with TemporaryDirectory() as td:
            world = GitWorld(Path(td))
            world.advance_upstream(2)
            before = self._git_dir_state(world.local)
            doctor.run_doctor(world.local)
            after = self._git_dir_state(world.local)
        self.assertEqual(before, after, "the doctor wrote into .git/")

    def test_no_fetch_verb_is_ever_issued(self):
        seen: list[list[str]] = []

        def _fake_run(argv, **kw):
            seen.append(list(argv))
            return subprocess.CompletedProcess(argv, 1, "", "")

        with mock.patch.object(subprocess, "run", side_effect=_fake_run):
            doctor.collect_source_facts(Path("/tmp/whatever"))
        verbs = {a[3] for a in seen if len(a) > 3}
        self.assertNotIn("fetch", verbs)
        self.assertNotIn("pull", verbs)

    def _git_dir_state(self, repo: Path):
        git_dir = repo / ".git"
        return sorted(
            (str(p.relative_to(git_dir)), p.stat().st_size)
            for p in git_dir.rglob("*")
            if p.is_file()
        )


class GitUnrunnableTests(unittest.TestCase):
    """The `unknown` arm that matters most: no git at all.

    R14 — this is also the tri-OS shape test for "git is absent". The decision
    is made from the spawn result, not from a platform check, so it is the same
    decision on all three.
    """

    def test_git_absent_yields_unknown_never_ok(self):
        def _boom(*a, **kw):
            raise FileNotFoundError("git")

        with mock.patch.object(subprocess, "run", side_effect=_boom):
            facts = doctor.collect_source_facts(Path("/tmp/x"))
        self.assertFalse(facts.is_git_toplevel)
        self.assertIn("could not run", facts.errors["git"])

        finding = doctor._currency_finding(Path("/tmp/x"), facts)
        self.assertEqual(finding.status, doctor.STATUS_UNKNOWN)

    def test_a_timed_out_git_is_unknown_never_ok(self):
        def _timeout(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="git", timeout=10)

        with mock.patch.object(subprocess, "run", side_effect=_timeout):
            facts = doctor.collect_source_facts(Path("/tmp/x"))
        self.assertIn("timed out", facts.errors["git"])

    def test_probe_reports_unknown_when_the_toplevel_call_fails(self):
        res = doctor.DoctorResolvers(
            source_facts=lambda folder, ask: doctor.SourceFacts(
                errors={"git": "git rev-parse could not run: [Errno 2]"}
            ),
        )
        with TemporaryDirectory() as td:
            root = Path(td)
            _make_orchestrator_shape(root)
            findings = doctor.probe_source_currency(root, res, {})
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].status, doctor.STATUS_UNKNOWN)
        self.assertIn("Errno 2", findings[0].summary)

    def test_undetermined_detachment_is_unknown_not_attached(self):
        facts = doctor.SourceFacts(
            is_git_toplevel=True, detached=None,
            errors={"detached": "git symbolic-ref exited 129"},
        )
        finding = doctor._head_attached_finding(Path("/x"), facts)
        self.assertEqual(finding.status, doctor.STATUS_UNKNOWN)
        self.assertIn("129", finding.summary)

    def test_terminal_prompts_are_disabled_for_every_call(self):
        """A health check that hangs on a password prompt is worse than one
        that says "I could not check"."""
        captured = {}

        def _fake_run(argv, **kw):
            captured.update(kw.get("env") or {})
            return subprocess.CompletedProcess(argv, 1, "", "")

        with mock.patch.object(subprocess, "run", side_effect=_fake_run):
            doctor._git(Path("/tmp/x"), ["status"])
        self.assertEqual(captured.get("GIT_TERMINAL_PROMPT"), "0")


class VerdictLatticeTests(unittest.TestCase):
    """`ok` requires evidence; it is never the absence of evidence."""

    def _facts(self, **over):
        base = dict(is_git_toplevel=True, detached=False, branch="main",
                    head_sha="a" * 40, remote_configured=True, remote_asked=True)
        base.update(over)
        return doctor.SourceFacts(**base)

    def test_a_known_problem_outranks_an_undetermined_leg(self):
        finding = doctor._currency_finding(
            Path("/x"),
            self._facts(contains_remote_tip=False, behind=7,
                        errors={"ancestry": "boom"}),
        )
        self.assertEqual(finding.status, doctor.STATUS_PROBLEM)

    def test_an_undetermined_leg_outranks_ok(self):
        finding = doctor._currency_finding(
            Path("/x"), self._facts(contains_remote_tip=None, behind_local_ref=0),
        )
        self.assertEqual(finding.status, doctor.STATUS_UNKNOWN)

    def test_no_network_pass_can_never_report_ok(self):
        """The `--no-write-fetch-head` consequence, pinned.

        VCO's own fetch passes `--no-write-fetch-head`, so neither the
        remote-tracking ref nor FETCH_HEAD's mtime records WHEN the ref was
        last updated. "Level with a ref of unknown age" is therefore not
        evidence of currency, and calling it `ok` would be a fresh instance of
        the defect being closed.
        """
        finding = doctor._currency_finding(
            Path("/x"),
            self._facts(remote_asked=False, behind_local_ref=0),
        )
        self.assertEqual(finding.status, doctor.STATUS_UNKNOWN)
        self.assertIn("--no-write-fetch-head", finding.summary)

    def test_ask_remote_false_performs_no_network_call(self):
        seen: list[list[str]] = []

        def _fake_run(argv, **kw):
            seen.append(list(argv))
            out = "/tmp/x" if "--show-toplevel" in argv else ""
            return subprocess.CompletedProcess(argv, 0, out, "")

        with mock.patch.object(subprocess, "run", side_effect=_fake_run), \
             mock.patch.object(doctor, "same_location", return_value=True):
            facts = doctor.collect_source_facts(Path("/tmp/x"), ask_remote=False)
        self.assertFalse(facts.remote_asked)
        self.assertFalse(any("ls-remote" in a for argv in seen for a in argv))

    def test_boot_scope_would_not_ask_the_remote(self):
        self.assertFalse(doctor._ask_remote_for({"scope": doctor.SCOPE_BOOT}))
        self.assertTrue(doctor._ask_remote_for({"scope": doctor.SCOPE_FULL}))
        self.assertTrue(doctor._ask_remote_for({}), "default is the full scope")


class RemediationTests(unittest.TestCase):
    """Every printed command is shipped code (R16 category 3)."""

    def _detached(self, **over):
        base = dict(is_git_toplevel=True, detached=True, branch="main",
                    head_sha="deadbeefcafe0000", local_branch_exists=True)
        base.update(over)
        return doctor.SourceFacts(**base)

    def test_no_destructive_verb_appears_in_any_remediation(self):
        blocks = [
            doctor._reattach_remediation(Path("/tmp/root"), self._detached()),
            doctor._reattach_remediation(
                Path("/tmp/root"), self._detached(local_branch_exists=False)),
            doctor._currency_remediation(
                Path("/tmp/root"), doctor.SourceFacts(is_git_toplevel=True)),
            doctor._stale_vct_remediation(
                Path("/tmp/root/tools/vct-secrets/vct"), "/usr/bin/vct"),
        ]
        forbidden = ("reset --hard", "clean -", "rm -rf", "checkout -f",
                     "branch -D", "push --force")
        for block in blocks:
            for verb in forbidden:
                self.assertNotIn(verb, block, f"destructive verb in:\n{block}")

    def test_the_checkout_command_is_omitted_when_the_branch_does_not_exist(self):
        """A printed command that cannot work is a promise, not help."""
        without = doctor._reattach_remediation(
            Path("/tmp/root"), self._detached(local_branch_exists=False))
        self.assertNotIn("checkout main", without)
        self.assertIn("branch -a", without)
        with_branch = doctor._reattach_remediation(Path("/tmp/root"), self._detached())
        self.assertIn("checkout main", with_branch)

    def test_every_path_printed_is_absolute(self):
        """Found by RUNNING the probe, not by reading it.

        A live `vco doctor` from a relative cwd printed
        `ln -sfn tools/vct-secrets/vct ~/.local/bin/vct` — a command that
        creates a BROKEN symlink when pasted from anywhere else. Callers may
        pass any folder shape (`install.py` passes an absolute root, the CLI
        defaults to `Path.cwd()`, a test passes `.`), so the absolutisation
        belongs in the formatter, and this pins it there.
        """
        relative = Path("tools/vct-secrets/vct")
        blocks = [
            doctor._reattach_remediation(Path("."), self._detached()),
            doctor._currency_remediation(
                Path("."), doctor.SourceFacts(is_git_toplevel=True)),
            doctor._stale_vct_remediation(relative, "/x/vct"),
            doctor._vco_lib_shadow_remediation(Path("."), "/venv/lib/vco_lib"),
        ]
        cwd = str(Path.cwd())
        for block in blocks:
            self.assertNotIn(
                " tools/vct-secrets/vct", block,
                f"relative path survived into a printed command:\n{block}")
            self.assertNotIn(
                "cd . &&", block, f"relative cwd in a printed command:\n{block}")
            self.assertIn(cwd, block, f"no absolute root in:\n{block}")

    def test_the_reattach_advice_names_a_gui_affordance_that_exists(self):
        """R16: the launcher command it points at must be real."""
        block = doctor._reattach_remediation(Path("/tmp/root"), self._detached())
        self.assertIn("Preferences -> Launcher updates", block)
        rust = (REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands"
                / "self_update.rs").read_text(encoding="utf-8")
        self.assertIn("pub async fn reattach_orchestrator_branch", rust)
        lib_rs = (REPO_ROOT / "launcher" / "src-tauri" / "src"
                  / "lib.rs").read_text(encoding="utf-8")
        self.assertIn("self_update::reattach_orchestrator_branch", lib_rs)

    def test_the_shadow_remediation_venv_path_is_resolved_not_assumed(self):
        """R23, pre-existing: the verify line hard-coded `.venv/bin/python`,
        which does not exist on Windows — so the command that PROVES the
        repair could not be run by the users on the OS that reported the
        class."""
        with TemporaryDirectory() as td:
            root = Path(td)
            block = doctor._vco_lib_shadow_remediation(root, "/venv/lib/vco_lib")
            self.assertNotIn(".venv/bin/python", block)
            self.assertIn("import vco_lib", block)

            win = root / ".venv" / "Scripts"
            win.mkdir(parents=True)
            (win / "python.exe").write_text("", encoding="utf-8")
            with mock.patch(
                "vco_lib.install_companions.platform.system", return_value="Windows"
            ):
                block_win = doctor._vco_lib_shadow_remediation(root, "x")
        self.assertIn("python.exe", block_win)


class CrossPlatformShapeTests(unittest.TestCase):
    """R14 — the OS-dependent decision is unit-tested for all three shapes."""

    def test_windows_folds_case_and_separators_posix_does_not(self):
        identity = lambda p: p  # noqa: E731 — a stand-in realpath
        self.assertTrue(
            doctor.same_location(
                "C:/Users/x/vco", r"C:\Users\X\VCO",
                normcase=ntpath.normcase, realpath=identity,
            ),
            "git prints C:/… while the filesystem says C:\\…; Windows folds both",
        )
        self.assertFalse(
            doctor.same_location(
                "/home/x/vco", "/home/X/VCO",
                normcase=posixpath.normcase, realpath=identity,
            ),
            "case is significant on Linux; macOS's default FS is insensitive but "
            "realpath (not normcase) is what resolves that, and it is injected",
        )

    def test_an_unresolvable_path_is_not_the_same_place(self):
        def _boom(_p):
            raise OSError("nope")

        self.assertFalse(doctor.same_location("/a", "/b", realpath=_boom))

    def test_the_fallback_branch_matches_the_rust_resolver(self):
        """Cross-language mirror (rule C), pinned to its counterpart.

        The Python doctor cannot shell the launcher binary, so this two-token
        rule exists twice. The parity test is what keeps the copies honest —
        the divergence of exactly this rule across two subsystems is what
        produced the incident.
        """
        rust = (REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands"
                / "git_cmd.rs").read_text(encoding="utf-8")
        match = re.search(r'FALLBACK_BRANCH:\s*&str\s*=\s*"([^"]+)"', rust)
        self.assertIsNotNone(match, "git_cmd.rs no longer declares FALLBACK_BRANCH")
        assert match is not None
        self.assertEqual(doctor.SOURCE_FALLBACK_BRANCH, match.group(1))

    def test_the_upstream_remote_name_matches_the_rust_constant(self):
        rust = (REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands"
                / "self_update.rs").read_text(encoding="utf-8")
        match = re.search(
            r'VCO_UPSTREAM_REMOTE:\s*&str\s*=\s*"([^"]+)"', rust)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(doctor.UPSTREAM_REMOTE, match.group(1))
        self.assertNotEqual(
            doctor.UPSTREAM_REMOTE, "origin",
            "origin may be a private fork; comparing against it answers a "
            "different question with a plausible number",
        )


class LastUpdateRunTests(unittest.TestCase):
    """"When did an update last succeed" — the third leg."""

    def _root(self, base: Path, rows=()) -> Path:
        root = base / "root"
        _make_orchestrator_shape(root)
        if rows:
            logs = root / "state" / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            (logs / "install.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
            )
        return root

    def _row(self, days_ago: float, phase="ok", step="session", mode="update"):
        ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        return {"ts": ts, "actor": "install.py", "step": step, "phase": phase,
                "detail": f"{mode} finished cleanly", "data": {"mode": mode}}

    def test_reports_the_newest_completed_session(self):
        with TemporaryDirectory() as td:
            root = self._root(Path(td), rows=[
                self._row(40), self._row(9, phase="error"), self._row(3),
            ])
            findings = doctor.probe_last_update_run(root, doctor.DoctorResolvers(), {})
        self.assertEqual(findings[0].status, doctor.STATUS_OK)
        self.assertIn("3 day(s) ago", findings[0].summary)
        self.assertEqual(findings[0].detail["mode"], "update")

    def test_absent_log_is_unknown_never_ok(self):
        with TemporaryDirectory() as td:
            root = self._root(Path(td))
            findings = doctor.probe_last_update_run(root, doctor.DoctorResolvers(), {})
        self.assertEqual(findings[0].status, doctor.STATUS_UNKNOWN)
        self.assertIn("does not exist", findings[0].summary)

    def test_a_log_with_no_completed_session_is_unknown(self):
        with TemporaryDirectory() as td:
            root = self._root(Path(td), rows=[
                self._row(2, phase="start"), self._row(1, step="doctor"),
            ])
            findings = doctor.probe_last_update_run(root, doctor.DoctorResolvers(), {})
        self.assertEqual(findings[0].status, doctor.STATUS_UNKNOWN)
        self.assertIn("no `session ok` row", findings[0].summary)

    def test_corrupt_rows_are_skipped_not_fatal(self):
        with TemporaryDirectory() as td:
            root = self._root(Path(td), rows=[self._row(5)])
            log = root / "state" / "logs" / "install.jsonl"
            log.write_text("not json\n[]\n" + log.read_text(encoding="utf-8"),
                           encoding="utf-8")
            findings = doctor.probe_last_update_run(root, doctor.DoctorResolvers(), {})
        self.assertEqual(findings[0].status, doctor.STATUS_OK)

    def test_an_unparseable_timestamp_still_reports_the_row(self):
        with TemporaryDirectory() as td:
            root = self._root(Path(td), rows=[
                {"ts": "yesterday", "step": "session", "phase": "ok"},
            ])
            findings = doctor.probe_last_update_run(root, doctor.DoctorResolvers(), {})
        self.assertEqual(findings[0].status, doctor.STATUS_OK)
        self.assertIsNone(findings[0].detail["age_days"])

    def test_age_alone_is_never_graded_a_problem(self):
        """Deliberate deviation from the plan's sketch, stated where it lives.

        An install root that has run nothing for a year is either deliberately
        pinned or silently not updating, and the AGE cannot tell those apart —
        `probe_source_currency` answers the question age was a proxy for, with
        evidence. Grading age alone would cry wolf on every user who is simply
        content with their version, and the doctor's own `npm_pins` probe
        already settled that policy for absent pins.
        """
        with TemporaryDirectory() as td:
            root = self._root(Path(td), rows=[self._row(900)])
            findings = doctor.probe_last_update_run(root, doctor.DoctorResolvers(), {})
        self.assertEqual(findings[0].status, doctor.STATUS_OK)
        self.assertIn("900 day(s) ago", findings[0].summary)

    def test_a_user_project_gets_no_finding(self):
        with TemporaryDirectory() as td:
            plain = Path(td) / "p"
            (plain / ".claude").mkdir(parents=True)
            self.assertEqual(
                doctor.probe_last_update_run(plain, doctor.DoctorResolvers(), {}), [])


class ProbeRegistrationTests(unittest.TestCase):
    def test_the_new_probes_are_registered_full_scope_only(self):
        """Not a cost decision — a promise one.

        `deferral_ledger.rs::run_boot_doctor_and_retries` counts every boot
        `problem` finding and logs "N problem(s) recorded in the deferral
        ledger — see the launcher's Updates page". These probes have no
        registered condition of their own (that is a row in
        deferral_conditions.toml, another lane's file), so a boot-scope
        problem here would make the launcher point users at a panel that
        cannot show them: `update.log`'s defect, one surface over.
        """
        for probe in ("source_currency", "last_update_run", "diagnostic_files",
                      "stale_vct_deploy"):
            _fn, scopes = doctor.PROBES[probe]
            self.assertEqual(scopes, (doctor.SCOPE_FULL,), probe)

    def test_boot_scope_runs_none_of_them(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            _make_orchestrator_shape(root)
            report = doctor.run_doctor(root, scope=doctor.SCOPE_BOOT)
        probes = {f.probe for f in report.findings}
        self.assertFalse(
            probes & {"source_currency", "head_attached", "last_update_run",
                      "diagnostic_files", "stale_vct_deploy"})

    def test_the_launcher_boot_counter_still_only_sees_ledger_backed_problems(self):
        """Guards the promise above against a later scope change."""
        rust = (REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands"
                / "deferral_ledger.rs").read_text(encoding="utf-8")
        # Normalised: the assertion is about the SENTENCE the launcher logs,
        # not about where rustfmt happened to wrap it.
        flat = re.sub(r"\\\s*\n\s*", "", rust)
        self.assertIn("problem(s) recorded in the deferral ledger", flat)
        boot = {pid for pid, (_fn, scopes) in doctor.PROBES.items()
                if doctor.SCOPE_BOOT in scopes}
        self.assertEqual(
            boot,
            {"mcp_commands_spawnable", "deferral_ledger", "disk_space"},
            "a probe added to the boot scope must either carry a doctor-owned "
            "condition_id or the Rust log line must be reworded first",
        )

    def test_the_whole_pass_spawns_nothing_when_the_seams_are_injected(self):
        """The module's hermeticity claim, enforced.

        v0.2.92 made one probe do real network I/O, so the old blanket "nothing
        here opens a socket" had to become a precise contract — and a precise
        contract that nothing checks is just a longer promise. With
        `source_facts` injected, a full `run_doctor` pass must spawn NO
        subprocess at all.
        """
        calls: list = []

        def _tripwire(*a, **kw):
            calls.append(a)
            raise AssertionError(f"probe spawned a subprocess: {a}")

        res = doctor.DoctorResolvers(
            source_facts=lambda folder, ask: doctor.SourceFacts(),
            npx_probe=lambda names: {"npx_present": True, "npx_path": "/b/npx",
                                     "npm_present": True, "commands": {}},
            mcp_entries=lambda: {},
            pin_rows=lambda: [],
            vco_lib_origin=lambda root: None,
            path_command=lambda name: None,
        )
        with TemporaryDirectory() as td:
            root = Path(td)
            _make_orchestrator_shape(root)
            with mock.patch.object(subprocess, "run", side_effect=_tripwire), \
                 mock.patch.object(subprocess, "Popen", side_effect=_tripwire):
                report = doctor.run_doctor(root, resolvers=res)
        self.assertEqual(calls, [])
        self.assertTrue(report.findings)

    def test_the_binary_freshness_ok_line_says_what_it_does_not_mean(self):
        """WFT cross-cutting item 9, in the text a user reads.

        `launcher_binary_fresh` compares the delivered binary with the one the
        TREE builds; on a frozen clone both are the same frozen version and it
        answers `ok`. That is the correct answer to the wrong question, and it
        is the single line that most made a stale install look healthy — so the
        line now names its own limit and points at the probe that answers the
        question the user actually has.
        """
        res = doctor.DoctorResolvers()
        with mock.patch("vco_lib.deferral_probes.run_probe", return_value=False):
            findings = doctor.probe_launcher_binary_fresh(
                Path("/tmp/x"), res, {"launcher_probe_extras": {"any": "thing"}})
        self.assertEqual(findings[0].status, doctor.STATUS_OK)
        self.assertIn("source_currency", findings[0].summary)
        self.assertIn("nothing about whether the TREE is current",
                      findings[0].summary)

    def test_a_raising_new_probe_degrades_to_unknown(self):
        def _boom(folder, res, ctx):
            raise RuntimeError("probe exploded")

        with mock.patch.dict(
            doctor.PROBES, {"source_currency": (_boom, (doctor.SCOPE_FULL,))}
        ):
            report = doctor.run_doctor(Path("/tmp/x"))
        finding = next(f for f in report.findings if f.probe == "source_currency")
        self.assertEqual(finding.status, doctor.STATUS_UNKNOWN)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class ShadowRemediationIsolatedVerifyTests(unittest.TestCase):
    """v0.2.92 R42 follow-up: the verify line prefixed ``cd /tmp &&`` to a
    (Windows) venv interpreter — POSIX-only path in front of the exact OS that
    reported the shadow class, so the one command that PROVES the repair was
    un-paste-able there.

    The fix drops the ``cd`` entirely and adds ``-I`` (isolated mode), which
    removes the cwd from ``sys.path`` — the entire reason the ``cd`` existed —
    and ignores ``PYTHONPATH``. These tests EXECUTE the printed command (the
    tokens after the interpreter, run with the real interpreter) to prove the
    behaviour, not the text: from inside a decoy checkout, the ``-I`` line
    must resolve vco_lib to the interpreter's own installation, while the
    same tokens WITHOUT ``-I`` fall into the decoy.
    """

    DECOY_INIT = (
        "# decoy vco_lib — if this wins, cwd leaked onto sys.path\n"
        "raise SystemExit('DECOY-WON')\n"
    )

    def _printed_verify_line(self) -> str:
        with TemporaryDirectory() as td:
            block = doctor._vco_lib_shadow_remediation(
                Path(td), "/venv/lib/vco_lib",
            )
        verify = [
            ln for ln in block.splitlines()
            if "import vco_lib" in ln and ln.startswith("#")
        ]
        self.assertEqual(1, len(verify), f"one verify line expected:\n{block}")
        return verify[0].lstrip("#").strip()

    def test_no_posix_only_cd_and_isolated_flag_present(self):
        line = self._printed_verify_line()
        self.assertNotIn("/tmp", line)
        self.assertNotIn("cd ", line)
        self.assertIn(" -I -c ", line)

    def test_the_printed_command_actually_runs_isolated(self):
        """Execute the printed tokens from inside a decoy vco_lib checkout:
        with ``-I`` the decoy must NOT win; without it (control) it must."""
        import shlex

        line = self._printed_verify_line()
        tokens = shlex.split(line)
        # tokens[0] is the interpreter ("python" in the no-venv fallback);
        # run the SAME flags/args under the real interpreter so the test is
        # about the printed flags, not about which `python` is on PATH.
        argv = [sys.executable] + tokens[1:]
        self.assertIn("-I", argv)

        with TemporaryDirectory() as td:
            decoy = Path(td) / "vco_lib"
            decoy.mkdir()
            (decoy / "__init__.py").write_text(self.DECOY_INIT, encoding="utf-8")
            env = {k: v for k, v in os.environ.items()
                   if k.upper() != "PYTHONPATH"}

            isolated = subprocess.run(
                argv, cwd=td, env=env, capture_output=True, text=True,
                timeout=60,
            )
            self.assertEqual(0, isolated.returncode, isolated.stderr)
            self.assertNotIn("DECOY", (isolated.stdout + isolated.stderr))
            self.assertIn("vco_lib", isolated.stdout)

            control = subprocess.run(
                [sys.executable] + [t for t in tokens[1:] if t != "-I"],
                cwd=td, env=env, capture_output=True, text=True, timeout=60,
            )
            self.assertNotEqual(0, control.returncode, (
                "without -I the decoy cwd must shadow the install — if this "
                "control stops failing, the -I flag is no longer load-bearing"
            ))

    def test_windows_venv_shape_gets_the_same_isolated_line(self):
        """The R23 fixture shape (Windows venv under .venv/Scripts) must carry
        the same -I verify line — no `cd /tmp &&` in front of python.exe."""
        with TemporaryDirectory() as td:
            root = Path(td)
            win = root / ".venv" / "Scripts"
            win.mkdir(parents=True)
            (win / "python.exe").write_text("", encoding="utf-8")
            with mock.patch(
                "vco_lib.install_companions.platform.system",
                return_value="Windows",
            ):
                block = doctor._vco_lib_shadow_remediation(root, "x")
        verify = [
            ln for ln in block.splitlines()
            if "import vco_lib" in ln and ln.startswith("#")
        ]
        self.assertEqual(1, len(verify), f"one verify line expected:\n{block}")
        line = verify[0]
        self.assertIn("python.exe", line)
        self.assertIn(" -I -c ", line)
        self.assertNotIn("cd ", line)  # no POSIX-only cwd hop in front of it
