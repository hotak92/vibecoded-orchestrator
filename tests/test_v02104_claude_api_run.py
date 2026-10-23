# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 — ``vco_lib.claude_api_run``: a headless ``claude -p`` on the
Console API key, with a cost summary and a ledger line.

A fake ``claude`` on PATH records its argv, its stdin, the env keys that
matter and the ``--mcp-config`` file, and prints a canned stream: the
``system``/``init`` event of a REAL ``--output-format stream-json --verbose``
review run and a REAL result object (both scrubbed copies, 2026-10-20).
Pinned:

* the child env drops ``CLAUDE_CODE_ENTRYPOINT`` and VCO's gateway routing
  and carries the key ONLY there (never argv, never the parent env);
* a blocker or an unresolvable key refuses with exit 3 before any spawn
  (and a blocker before the secret store is touched);
* ``--max-budget-usd`` is always passed; the tool policy flags;
* the cost summary and the ledger line parse the real shape and an error
  result (the 400 "credit balance is too low" refusal);
* the key never appears in stdout, stderr, the report or the ledger;
* (R27, Fable FINAL2) ``review`` runs ``--restricted`` with the guard hook
  in ``--settings``, no Bash allow rule, the KG write tool denied and only
  the ``weaviate-kg`` MCP server; a credential other than the env key, or no
  init event, fails the run; ``--report`` is written only on success;
  ``CLAUDE_CODE_OAUTH_TOKEN`` never reaches the child.

No network, no real secret: the key is an obvious placeholder.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.common.wrapper_staging import stage_scripts
from vco_lib import claude_api_run as car

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "claude_api_run" / \
    "result_success_live_2026-10-20.json"
STREAM_FIXTURE = REPO / "tests" / "fixtures" / "claude_api_run" / \
    "stream_init_result_live_2026-10-20.jsonl"
INIT_EVENT = json.loads(
    STREAM_FIXTURE.read_text(encoding="utf-8").splitlines()[0])
FAKE_KEY = "test-not-a-real-console-key-7f3a"  # vco-secret-scan: test-placeholder
GATEWAY_URL = "http://127.0.0.1:11460"

_FAKE_CLAUDE = """#!{python}
import json, os, sys
rec = os.environ["FAKE_CLAUDE_RECORD"]
keys = ["ANTHROPIC_API_KEY", "CLAUDE_CODE_ENTRYPOINT", "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"]
mcp = None
if "--mcp-config" in sys.argv:
    mcp = json.load(open(sys.argv[sys.argv.index("--mcp-config") + 1]))
settings = None
if "--settings" in sys.argv:
    settings = json.load(open(sys.argv[sys.argv.index("--settings") + 1]))
with open(rec, "w") as fh:
    json.dump({{"argv": sys.argv[1:], "stdin": sys.stdin.read(),
               "cwd": os.getcwd(), "mcp": mcp, "settings": settings,
               "env": {{k: os.environ.get(k) for k in keys}}}}, fh)
out = open(os.environ["FAKE_CLAUDE_OUT"]).read()
if os.environ.get("FAKE_CLAUDE_LEAK"):
    out = out.replace("LEAK", os.environ.get("ANTHROPIC_API_KEY", ""))
    sys.stderr.write("debug key=" + os.environ.get("ANTHROPIC_API_KEY", "") + "\\n")
sys.stdout.write(out)
sys.exit(int(os.environ.get("FAKE_CLAUDE_RC", "0")))
"""


def _stream(result: "dict | None" = None, init: "dict | None" = None) -> str:
    """A stream-json stdout: the init event, then the result event."""
    if result is None:
        result = json.loads(FIXTURE.read_text(encoding="utf-8"))
    lines = [json.dumps(init if init is not None else INIT_EVENT),
             json.dumps({"type": "assistant", "message": {"content": []}}),
             json.dumps(result)]
    return "\n".join(lines) + "\n"


KG_ENTRY = {"type": "stdio", "command": "/opt/vco/.venv/bin/python",
            "args": ["server.py"], "env": {"WEAVIATE_URL": "http://x:1"}}


def _error_result() -> dict:
    """The live success shape turned into the measured 400 refusal."""
    d = json.loads(FIXTURE.read_text(encoding="utf-8"))
    d.update({
        "is_error": True, "api_error_status": 400, "num_turns": 1,
        "result": "API Error: 400 Your credit balance is too low to access "
                  "the Anthropic API.",
        "total_cost_usd": 0, "modelUsage": {},
    })
    return d


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Fake claude on PATH, redirected state dir, a fake secret resolver."""
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    claude = fakebin / "claude"
    claude.write_text(_FAKE_CLAUDE.format(python=sys.executable),
                      encoding="utf-8")
    claude.chmod(0o755)
    record = tmp_path / "record.json"
    out = tmp_path / "out.json"
    out.write_text(_stream(), encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()

    monkeypatch.setenv("PATH", f"{fakebin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    monkeypatch.setenv("FAKE_CLAUDE_OUT", str(out))
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("VCT_MODEL_GATEWAY_PORT", "11460")
    # The realistic parent: a VS Code panel routed through VCO's gateway.
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "claude-vscode")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", GATEWAY_URL)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "loopback-gateway-bearer")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "subscription-token-placeholder")
    monkeypatch.setattr(car, "_user_mcp_servers", lambda: {
        "weaviate-kg": KG_ENTRY, "playwright": {"command": "npx"}})
    for v in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
              "CLAUDE_CODE_USE_FOUNDRY", "FAKE_CLAUDE_LEAK", "FAKE_CLAUDE_RC"):
        monkeypatch.delenv(v, raising=False)

    calls: list = []

    def fake_get(key, **kwargs):
        calls.append((key, kwargs))
        return FAKE_KEY

    monkeypatch.setattr("vco_lib.agent_secrets.get", fake_get)

    def run(*extra: str) -> int:
        return car.main(["--prompt", "Review the diff please",
                         "--cwd", str(work), *extra])

    return SimpleNamespace(
        tmp=tmp_path, record=record, out=out, work=work, secret_calls=calls,
        ledger=tmp_path / "state" / "metrics" / car.LEDGER_FILENAME,
        run=run,
        rec=lambda: json.loads(record.read_text(encoding="utf-8")),
    )


def _ledger_lines(h) -> list:
    return [json.loads(x) for x in
            h.ledger.read_text(encoding="utf-8").splitlines() if x.strip()]


class TestChildEnvAndArgv:

    def test_the_child_env_carries_the_key_and_drops_the_panel_routing(
            self, harness, capsys):
        assert harness.run() == 0
        env = harness.rec()["env"]
        assert env["ANTHROPIC_API_KEY"] == FAKE_KEY
        assert env["CLAUDE_CODE_ENTRYPOINT"] is None
        assert env["ANTHROPIC_BASE_URL"] is None
        assert env["ANTHROPIC_AUTH_TOKEN"] is None
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] is None
        assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
        # The parent environment is untouched.
        assert "ANTHROPIC_API_KEY" not in os.environ
        assert os.environ["CLAUDE_CODE_ENTRYPOINT"] == "claude-vscode"
        assert os.environ["ANTHROPIC_BASE_URL"] == GATEWAY_URL

    def test_argv_defaults_budget_always_and_prompt_on_stdin(self, harness):
        assert harness.run() == 0
        rec = harness.rec()
        argv = rec["argv"]
        assert argv[:4] == ["-p", "--output-format", "stream-json",
                            "--verbose"]
        assert argv[argv.index("--model") + 1] == "claude-fable-5-1"
        assert argv[argv.index("--effort") + 1] == "high"
        assert argv[argv.index("--max-budget-usd") + 1] == "25"
        assert "--max-turns" not in argv
        assert rec["stdin"] == "Review the diff please"
        assert not any("Review the diff" in a for a in argv)
        assert not any(FAKE_KEY in a for a in argv)
        assert rec["cwd"] == str(harness.work.resolve())

    def test_budget_and_turns_are_passed_through(self, harness):
        assert harness.run("--max-budget-usd", "0.2", "--max-turns", "3",
                           "--model", "claude-haiku-4-5",
                           "--effort", "low") == 0
        argv = harness.rec()["argv"]
        assert argv[argv.index("--max-budget-usd") + 1] == "0.2"
        assert argv[argv.index("--max-turns") + 1] == "3"
        assert argv[argv.index("--model") + 1] == "claude-haiku-4-5"

    def test_review_policy_is_read_only(self, harness):
        assert harness.run() == 0
        rec = harness.rec()
        argv = rec["argv"]
        assert "--restricted" in argv
        assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob,Bash"
        assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
        allowed = argv[argv.index("--allowedTools") + 1:
                       argv.index("--disallowedTools")]
        denied = argv[argv.index("--disallowedTools") + 1:
                      argv.index("--mcp-config")]
        # No Bash rule at all: only the guard hook approves a Bash call.
        assert not any(r.startswith("Bash") for r in allowed), allowed
        assert "mcp__weaviate-kg__hybrid_search" in allowed
        for rule in ("Edit", "Write", "Bash(git push *)",
                     "Bash(git commit *)", "Bash(rm *)",
                     "mcp__weaviate-kg__store_knowledge_node"):
            assert rule in denied, rule
        # The guard hook rides in --settings, on every tool — as a FILE
        # (FINAL3 finding 1: no argv element may carry the hook's ``||``).
        assert not any("|" in a for a in argv), argv
        settings = rec["settings"]
        (group,) = settings["hooks"]["PreToolUse"]
        assert group["matcher"] == "*"
        (hook,) = group["hooks"]
        assert hook["type"] == "command"
        assert "-m vco_lib.claude_api_review_guard" in hook["command"]
        assert hook["command"].endswith(" || exit 2")
        # Only the KG server, strictly.
        assert "--strict-mcp-config" in argv
        assert rec["mcp"] == {"mcpServers": {"weaviate-kg": KG_ENTRY}}

    def test_review_without_a_kg_registration_says_so(
            self, harness, monkeypatch, capsys):
        monkeypatch.setattr(car, "_user_mcp_servers", lambda: {})
        assert harness.run() == 0
        argv = harness.rec()["argv"]
        assert "--mcp-config" not in argv
        assert "weaviate-kg MCP server is not registered" in \
            capsys.readouterr().err

    def test_the_mcp_config_file_is_removed_after_the_run(self, harness):
        assert harness.run() == 0
        argv = harness.rec()["argv"]
        assert not Path(argv[argv.index("--mcp-config") + 1]).exists()

    def test_edit_policy_allows_edits_but_not_git_mutation(self, harness):
        assert harness.run("--policy", "edit") == 0
        argv = harness.rec()["argv"]
        assert "--tools" not in argv
        assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
        assert "--restricted" not in argv and "--settings" not in argv
        assert "--mcp-config" not in argv
        denied = argv[argv.index("--disallowedTools") + 1:]
        assert "Edit" not in denied and "Bash(git push *)" in denied
        allowed = argv[argv.index("--allowedTools") + 1:
                       argv.index("--disallowedTools")]
        assert "Bash(git diff *)" in allowed

    def test_the_key_is_requested_from_the_shared_slot_for_the_project(
            self, harness):
        assert harness.run("--project", str(harness.tmp)) == 0
        (key, kwargs), = harness.secret_calls
        assert key == "ANTHROPIC_API_KEY_GATEWAY"
        assert kwargs["shared_only"] is True
        assert kwargs["allow_dotenv"] is False
        assert kwargs["project"] == str(harness.tmp.resolve())

    def test_without_project_the_requester_is_the_cwd(self, harness):
        assert harness.run("--key-name", "MY_OTHER_KEY") == 0
        (key, kwargs), = harness.secret_calls
        assert key == "MY_OTHER_KEY"
        assert kwargs["project"] == str(harness.work.resolve())


class TestRefusals:

    @pytest.mark.parametrize("var,value", [
        ("CLAUDE_CODE_USE_BEDROCK", "1"),
        ("ANTHROPIC_BASE_URL", "https://proxy.example.com/user:pw@x"),
    ])
    def test_a_blocker_refuses_without_spawning_or_resolving(
            self, harness, monkeypatch, capsys, var, value):
        monkeypatch.setenv(var, value)
        assert harness.run() == car.EXIT_REFUSED
        assert not harness.record.exists()
        assert harness.secret_calls == []
        err = capsys.readouterr().err
        assert "refused" in err and var in err
        assert "proxy.example.com" not in err
        assert not harness.ledger.exists()

    def test_a_users_own_auth_token_refuses(self, harness, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_BASE_URL")
        assert harness.run() == car.EXIT_REFUSED
        assert not harness.record.exists()

    def test_an_unresolvable_key_refuses_naming_the_key(
            self, harness, monkeypatch, capsys):
        from vco_lib.agent_secrets import SecretNotFound

        def miss(key, **kwargs):
            raise SecretNotFound(key)

        monkeypatch.setattr("vco_lib.agent_secrets.get", miss)
        assert harness.run() == car.EXIT_REFUSED
        assert not harness.record.exists()
        assert "ANTHROPIC_API_KEY_GATEWAY" in capsys.readouterr().err

    def test_no_claude_on_path_refuses(self, harness, monkeypatch, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        assert harness.run() == car.EXIT_REFUSED

    def test_a_non_positive_budget_is_a_usage_error(self, harness):
        assert harness.run("--max-budget-usd", "0") == 2
        assert not harness.record.exists()

    @pytest.mark.parametrize("value", ["nan", "inf", "-1"])
    def test_a_non_finite_budget_is_a_usage_error(self, harness, value):
        """R26 NIT: ``nan`` slipped past ``<= 0``; the CLI must never get it."""
        assert harness.run("--max-budget-usd", value) == 2
        assert not harness.record.exists()

    def test_a_project_that_is_not_a_directory_is_a_usage_error(
            self, harness):
        """R26 NIT: a bare name was resolved against the cwd and looked up as
        a path that is not the project."""
        assert harness.run("--project", "NoSuchProjectName") == 2
        assert not harness.record.exists()

    def test_a_cwd_that_is_not_a_directory_is_the_same_usage_error(
            self, harness):
        """R27 NIT: --cwd and --project not being directories map to ONE
        code (2), the one the docstring lists."""
        assert car.main(["--prompt", "x", "--cwd",
                         str(harness.tmp / "nope")]) == car.EXIT_USAGE == 2
        assert not harness.record.exists()
        assert car.__doc__ is not None
        assert "--cwd`` or ``--project`` not a directory" in car.__doc__


class TestSummaryAndLedger:

    def test_the_real_shape_parses_into_summary_and_ledger(
            self, harness, capsys):
        report = harness.tmp / "out" / "review.md"
        assert harness.run("--report", str(report)) == 0
        captured = capsys.readouterr()
        assert report.read_text(encoding="utf-8") == "ok\n"
        assert "cost $0.0035" in captured.err
        assert "claude-haiku-4-5: in 10 · out 60 · cache read 22160" in \
            captured.err
        assert "turns 1" in captured.err and "duration 1.2s" in captured.err
        (line,) = _ledger_lines(harness)
        assert line["total_cost_usd"] == pytest.approx(0.003494)
        assert line["num_turns"] == 1 and line["duration_ms"] == 1250
        hk = line["model_usage"]["claude-haiku-4-5"]
        assert hk == {"input_tokens": 10, "output_tokens": 60,
                      "cache_read_tokens": 22160, "cache_write_tokens": 0,
                      "cost_usd": pytest.approx(0.002526)}
        assert line["model_cost_sum_usd"] == pytest.approx(0.003494)
        assert line["is_error"] is False and line["error"] is None
        assert line["report"] == str(report.resolve())
        text = harness.ledger.read_text(encoding="utf-8")
        assert "Review the diff" not in text and '"ok"' not in text
        assert "result" not in line

    def test_json_mode_prints_one_object_with_the_result(
            self, harness, capsys):
        assert harness.run("--json") == 0
        obj = json.loads(capsys.readouterr().out)
        assert obj["result"] == "ok"
        assert obj["ledger"] == str(harness.ledger)
        assert obj["model"] == "claude-fable-5-1"

    def test_an_api_error_result_is_reported_and_exits_1(
            self, harness, capsys):
        harness.out.write_text(_stream(_error_result()), encoding="utf-8")
        report = harness.tmp / "r.md"
        assert harness.run("--report", str(report)) == car.EXIT_RUN_FAILED
        err = capsys.readouterr().err
        assert "ERROR: API status 400; API Error: 400 Your credit balance" \
            in err
        (line,) = _ledger_lines(harness)
        assert line["is_error"] is True and line["api_error_status"] == 400
        assert line["total_cost_usd"] == 0
        assert "credit balance" in line["error"]
        # R27 LOW: the error text is not written as the "report".
        assert not report.exists()
        assert line["report"] is None
        assert f"no report written to {report.resolve()} (the run failed)" \
            in err

    def test_unparsable_output_is_a_failure(self, harness, capsys):
        harness.out.write_text("not json at all", encoding="utf-8")
        assert harness.run() == car.EXIT_RUN_FAILED
        assert "carries no JSON result object" in capsys.readouterr().err
        assert _ledger_lines(harness)[0]["error"]

    def test_a_non_zero_cli_exit_is_a_failure(self, harness, monkeypatch):
        monkeypatch.setenv("FAKE_CLAUDE_RC", "1")
        assert harness.run() == car.EXIT_RUN_FAILED

    def test_the_key_never_reaches_stdout_stderr_report_or_ledger(
            self, harness, monkeypatch, capsys):
        # A CLI that echoes the key (stderr AND the result) is scrubbed.
        d = json.loads(FIXTURE.read_text(encoding="utf-8"))
        d["result"] = "here is LEAK"
        harness.out.write_text(_stream(d), encoding="utf-8")
        monkeypatch.setenv("FAKE_CLAUDE_LEAK", "1")
        report = harness.tmp / "r.md"
        harness.run("--report", str(report))
        monkeypatch.setenv("FAKE_CLAUDE_RC", "1")
        harness.run("--json")
        captured = capsys.readouterr()
        assert FAKE_KEY not in captured.out + captured.err
        assert "here is ***" in report.read_text(encoding="utf-8")
        assert FAKE_KEY not in report.read_text(encoding="utf-8")
        assert FAKE_KEY not in harness.ledger.read_text(encoding="utf-8")
        assert harness.rec()["env"]["ANTHROPIC_API_KEY"] == FAKE_KEY


    def test_the_key_in_cli_stderr_is_scrubbed_on_a_parse_failure(
            self, harness, monkeypatch, capsys):
        # Unparsable stdout shows the CLI's stderr tail — scrubbed.
        harness.out.write_text("garbage", encoding="utf-8")
        monkeypatch.setenv("FAKE_CLAUDE_LEAK", "1")
        assert harness.run() == car.EXIT_RUN_FAILED
        captured = capsys.readouterr()
        assert "debug key=***" in captured.err
        assert FAKE_KEY not in captured.out + captured.err
        assert FAKE_KEY not in harness.ledger.read_text(encoding="utf-8")


class TestKeySource:
    """R27 LOW (Fable FINAL2): the init event's ``apiKeySource`` proves the
    env key served. Measured live 2026-10-20 (CLI 2.1.285): a run on the
    key reports ``ANTHROPIC_API_KEY`` — the fixture's init event is that run's."""

    def test_the_live_fixture_carries_the_expected_source(self):
        assert INIT_EVENT["apiKeySource"] == car.EXPECTED_KEY_SOURCE

    def test_the_source_is_shown_and_recorded(self, harness, capsys):
        assert harness.run() == 0
        assert "credential: ANTHROPIC_API_KEY" in capsys.readouterr().err
        (line,) = _ledger_lines(harness)
        assert line["api_key_source"] == "ANTHROPIC_API_KEY"
        assert line["error"] is None

    @pytest.mark.parametrize("source", ["/login managed key", "apiKeyHelper",
                                        "none", None])
    def test_another_credential_fails_the_run(self, harness, capsys, source):
        init = dict(INIT_EVENT, apiKeySource=source)
        if source is None:
            del init["apiKeySource"]
        harness.out.write_text(_stream(init=init), encoding="utf-8")
        report = harness.tmp / "r.md"
        assert harness.run("--report", str(report)) == car.EXIT_RUN_FAILED
        err = capsys.readouterr().err
        assert f"the run did not use the API key: {source}" in err
        assert not report.exists()
        (line,) = _ledger_lines(harness)
        assert line["error"].startswith("the run did not use the API key")
        # The cost summary still comes from the result event.
        assert line["total_cost_usd"] == pytest.approx(0.003494)

    def test_no_init_event_fails_the_run(self, harness, capsys):
        result = json.loads(FIXTURE.read_text(encoding="utf-8"))
        harness.out.write_text(json.dumps(result) + "\n", encoding="utf-8")
        assert harness.run() == car.EXIT_RUN_FAILED
        assert "could not confirm which credential" in capsys.readouterr().err

    def test_the_stream_fixture_is_scrubbed(self):
        text = STREAM_FIXTURE.read_text(encoding="utf-8")
        for line in text.splitlines():
            d = json.loads(line)
            assert d["session_id"].startswith("00000000-")
            assert d["uuid"].startswith("00000000-")
        assert "sk-ant" not in text and "/home/" not in text


class TestParseHelpers:

    def test_parse_result_tolerates_leading_noise(self):
        obj, err = car.parse_result('warning: x\n{"type": "result"}\n')
        assert err is None and obj == {"type": "result"}

    def test_parse_result_takes_the_last_result_event_of_a_stream(self):
        obj, err = car.parse_result(_stream())
        assert err is None and obj is not None and obj["type"] == "result"
        assert obj["total_cost_usd"] == pytest.approx(0.003494)
        init = car.parse_init(_stream())
        assert init is not None and init["apiKeySource"] == "ANTHROPIC_API_KEY"

    def test_a_stream_without_a_result_is_unparsable(self):
        obj, err = car.parse_result(json.dumps(INIT_EVENT) + "\n")
        assert obj is None and err

    def test_summarize_of_nothing_is_zeroes(self):
        s = car.summarize(None)
        assert s["total_cost_usd"] is None and s["model_usage"] == {}


def _stripped_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in (
        "VCT_VENV", "VCT_INSTALL_ROOT", "VCT_ORCHESTRATOR_ROOT",
        "VIRTUAL_ENV")}


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash .sh leg; Windows runs the .ps1 sibling: "
    "tests/test_v02104_claude_api_run.py::test_ps1_wrapper_refuses_with_exit_3")
class TestBashWrapper:

    def test_refuses_with_exit_3_without_a_qualifying_venv(self, tmp_path):
        scripts = stage_scripts(tmp_path / ".claude" / "scripts",
                                "claude-api-run.sh")
        proc = subprocess.run(
            ["bash", str(scripts / "claude-api-run.sh"), "--prompt", "x"],
            capture_output=True, text=True, env=_stripped_env(),
            cwd=str(tmp_path), timeout=120)
        assert proc.returncode == 3, proc.stderr
        assert "claude-api-run: ERROR - no Python environment" in proc.stderr
        assert "nothing was billed" in proc.stderr
        assert proc.stdout.strip() == ""

    def test_forwards_the_project_and_every_argument(self, tmp_path):
        clone = tmp_path / "orch"
        (clone / ".venv" / "bin").mkdir(parents=True)
        py = clone / ".venv" / "bin" / "python"
        py.write_text('#!/bin/sh\nif [ "$1" = "-c" ]; then exit 0; fi\n'
                      'echo "ARGS=$*"\n', encoding="utf-8")
        py.chmod(0o755)
        (clone / "install.py").write_text("", encoding="utf-8")
        (clone / "first-install.sh").write_text("", encoding="utf-8")
        proj = tmp_path / "proj"
        scripts = stage_scripts(proj / ".claude" / "scripts",
                                "claude-api-run.sh")
        env = {**_stripped_env(), "VCT_ORCHESTRATOR_ROOT": str(clone)}
        proc = subprocess.run(
            ["bash", str(scripts / "claude-api-run.sh"), "--prompt",
             "hello", "--max-turns", "2"],
            capture_output=True, text=True, env=env, cwd=str(tmp_path),
            timeout=120)
        assert proc.returncode == 0, proc.stderr
        assert (f"ARGS=-m vco_lib.claude_api_run --project {proj} "
                "--prompt hello --max-turns 2") in proc.stdout


def test_ps1_wrapper_refuses_with_exit_3(tmp_path):
    exe = shutil.which("pwsh") or shutil.which("powershell")
    if exe is None:
        assert not os.environ.get("CI"), (
            "PowerShell is absent in CI — this gate cannot run and must not "
            "be reported as passing.")
        pytest.skip("no PowerShell interpreter on this machine")
    from tests.common.pwsh import PWSH_BASE_FLAGS

    scripts = stage_scripts(tmp_path / ".claude" / "scripts",
                            "claude-api-run.ps1")
    proc = subprocess.run(
        [exe, *PWSH_BASE_FLAGS, "-ExecutionPolicy", "Bypass", "-File",
         str(scripts / "claude-api-run.ps1"), "--prompt", "x"],
        capture_output=True, text=True, env=_stripped_env(),
        cwd=str(tmp_path), timeout=120)
    assert proc.returncode == 3, proc.stderr
    assert "claude-api-run: ERROR - no Python environment" in proc.stderr
    assert "nothing was billed" in proc.stderr


@pytest.mark.parametrize("native_eap", [False, True])
@pytest.mark.parametrize("module_rc", [0, 1, 2, 3])
def test_ps1_wrapper_exit_code_survives_stderr_output(tmp_path, module_rc,
                                                       native_eap):
    """R27 NIT: the module writes its cost summary to stderr on every run.
    With ``$ErrorActionPreference = "Stop"`` still in force at the call, a
    redirected native stderr line (Windows PowerShell 5.1) or a non-zero
    native exit under ``$PSNativeCommandUseErrorActionPreference`` (PowerShell
    7.3+, reproducible here) becomes a terminating error and the wrapper
    exits 1 whatever the module returned. The wrapper must exit with the
    module's own code."""
    exe = shutil.which("pwsh") or shutil.which("powershell")
    if exe is None:
        assert not os.environ.get("CI"), (
            "PowerShell is absent in CI — this gate cannot run and must not "
            "be reported as passing.")
        pytest.skip("no PowerShell interpreter on this machine")
    if sys.platform == "win32":
        pytest.skip("the fake interpreter is a POSIX shell script")
    from tests.common.pwsh import PWSH_BASE_FLAGS

    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    py.write_text(
        '#!/bin/sh\nif [ "$1" = "-c" ]; then exit 0; fi\n'
        'echo "claude-api-run: model x · cost 0.01" >&2\n'
        'echo "ERROR-LIKE stderr line" >&2\n'
        f'echo "ARGS=$*"\nexit {module_rc}\n', encoding="utf-8")
    py.chmod(0o755)
    scripts = stage_scripts(tmp_path / "proj" / ".claude" / "scripts",
                            "claude-api-run.ps1")
    env = {**_stripped_env(), "VCT_VENV": str(venv)}
    proc = subprocess.run(
        [exe, *PWSH_BASE_FLAGS, "-ExecutionPolicy", "Bypass", "-Command",
         ("$PSNativeCommandUseErrorActionPreference = $true; "
          if native_eap else "")
         + f"& '{scripts / 'claude-api-run.ps1'}' --prompt hello 2>&1; "
         "exit $LASTEXITCODE"],
        capture_output=True, text=True, env=env, cwd=str(tmp_path),
        timeout=120)
    assert proc.returncode == module_rc, proc.stdout + proc.stderr
    assert "ARGS=-m vco_lib.claude_api_run --project" in proc.stdout
    assert "ERROR-LIKE stderr line" in proc.stdout + proc.stderr


def test_the_wrapper_pair_ships():
    from fnmatch import fnmatch

    from vco_lib.bundle_globs import script_patterns
    for name in ("claude-api-run.sh", "claude-api-run.ps1"):
        assert (REPO / "templates" / "scripts" / name).is_file()
        assert any(fnmatch(name, p) for p in script_patterns()), name


def test_the_fixture_carries_no_account_identifiers():
    text = FIXTURE.read_text(encoding="utf-8")
    d = json.loads(text)
    assert d["session_id"].startswith("00000000-")
    assert d["uuid"].startswith("00000000-")
    assert "sk-ant" not in text


class TestWindowsHookShell:
    """FINAL3 finding 4: under Claude Code's PowerShell hook fallback the
    bash-syntax guard command cannot run, so ``review`` needs Git Bash."""

    def test_pinned_git_bash_counts(self, tmp_path):
        bash = tmp_path / "bash.exe"
        bash.write_text("")
        assert car.windows_hook_shell_is_bash(
            {"CLAUDE_CODE_GIT_BASH_PATH": str(bash), "PATH": ""})

    def test_git_with_its_bash_counts(self, tmp_path):
        (tmp_path / "Git" / "cmd").mkdir(parents=True)
        (tmp_path / "Git" / "bin").mkdir()
        git = tmp_path / "Git" / "cmd" / "git"
        git.write_text("#!/bin/sh\n")
        git.chmod(0o755)
        (tmp_path / "Git" / "bin" / "bash.exe").write_text("")
        assert car.windows_hook_shell_is_bash(
            {"PATH": str(tmp_path / "Git" / "cmd")})

    def test_nothing_found_is_false(self, tmp_path):
        assert not car.windows_hook_shell_is_bash({"PATH": str(tmp_path)})

    def test_review_on_windows_without_git_bash_refuses_before_the_key(
            self, harness, monkeypatch):
        monkeypatch.setattr(car, "_on_windows", lambda: True)
        monkeypatch.setattr(car, "windows_hook_shell_is_bash", lambda env: False)
        assert harness.run() == car.EXIT_REFUSED
        assert not harness.record.exists()


class TestR28WindowsResiduals:

    def test_wsl_bash_is_not_git_bash(self):
        assert car._is_wsl_launcher(r"C:\Windows\System32\bash.exe")
        assert car._is_wsl_launcher(
            "C:/Users/a/AppData/Local/Microsoft/WindowsApps/bash.exe")
        assert not car._is_wsl_launcher(r"C:\Program Files\Git\bin\bash.exe")

    def test_cmd_meta_in_an_argument_is_reported(self):
        msg = car.cmd_unsafe_refusal(["claude", "-p", "C:/t/a&b/settings.json"])
        assert msg is not None and "C:/t/a&b/settings.json" in msg
        assert car.cmd_unsafe_refusal(
            ["claude", "--settings", "C:/t/x.json"]) is None


class TestFinal4CmdRefusalNamesTheRole:
    """FINAL4 NIT 1: the refusal names WHICH argument carries the cmd.exe
    metacharacter and the fix for THAT argument — never "set TEMP" when the
    cause is the claude CLI's own path."""

    def test_the_claude_path_is_named_and_temp_is_not_blamed(self):
        msg = car.cmd_unsafe_refusal(
            ["C:/Users/A&B/npm/claude.cmd", "-p", "--settings", "C:/t/s.json"])
        assert msg is not None
        assert "the claude CLI path 'C:/Users/A&B/npm/claude.cmd'" in msg
        assert "install the claude CLI" in msg
        assert "TEMP" not in msg

    def test_a_temp_file_names_its_file_and_temp(self):
        msg = car.cmd_unsafe_refusal(
            ["claude.cmd", "--settings", "C:/T/a&b/settings.json",
             "--mcp-config", "C:/T/a&b/mcp.json"])
        assert msg is not None
        assert "the review settings file 'C:/T/a&b/settings.json'" in msg
        assert "the MCP config file 'C:/T/a&b/mcp.json'" in msg
        assert "set TEMP" in msg
        assert "claude CLI path" not in msg

    def test_another_flag_value_names_its_flag(self):
        msg = car.cmd_unsafe_refusal(["claude.cmd", "--model", "opus|x"])
        assert msg is not None
        assert "the --model value 'opus|x'" in msg
        assert "TEMP" not in msg

    def test_a_clean_argv_has_no_refusal(self):
        assert car.cmd_unsafe_refusal(
            ["C:/npm/claude.cmd", "-p", "--settings", "C:/t/s.json"]) is None

    def test_main_names_the_claude_path_end_to_end(
            self, harness, monkeypatch, capsys):
        real_which = car.shutil.which

        def which(name, *a, **kw):
            if name == "claude":
                return "C:/Users/A&B/npm/claude.cmd"
            return real_which(name, *a, **kw)

        monkeypatch.setattr(car, "_on_windows", lambda: True)
        monkeypatch.setattr(car, "windows_hook_shell_is_bash", lambda env: True)
        monkeypatch.setattr(car.shutil, "which", which)
        assert harness.run() == car.EXIT_REFUSED
        err = capsys.readouterr().err
        assert "the claude CLI path 'C:/Users/A&B/npm/claude.cmd'" in err
        assert "TEMP" not in err
        assert not harness.record.exists(), "refused before anything ran"
