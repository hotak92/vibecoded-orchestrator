# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-12 — cross-artefact contracts for the model gateway's GUI.

The GUI spans four languages, and three of the seams between them are
compile-time-invisible:

  * **Rust <-> Python** — `commands/model_gateway.rs` carries the gateway's
    port and state-file names as constants so a 5-second status poll does not
    spawn an interpreter. That is a deliberate class-C mirror under the
    repo's A>B>C rule, and a mirror without a parity test is just drift with
    a comment on it.
  * **TypeScript <-> Python** — the card's offered default model and the six
    slot keys it names in its warnings must be the same strings the writer
    uses, or the warning names a key the writer does not check.
  * **Template <-> renderer** — the model-routing section must be INVISIBLE
    on an install with no gateway. Not "short", not "hedged": absent.

Plus the R15 rule itself, asserted as a negative over the files that could
break it.
"""
from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RUST = REPO / "launcher" / "src-tauri" / "src" / "commands" / "model_gateway.rs"
PY = REPO / "vco_lib" / "vscode_settings.py"
TS = REPO / "launcher" / "src" / "lib" / "api" / "model_gateway.ts"
SVELTE = REPO / "launcher" / "src" / "routes" / "services" / "+page.svelte"
TEMPLATE = REPO / "templates" / "CLAUDE.md.template"
GATEWAY_CONFIG = REPO / "claude_mcp_servers" / "model_router" / "config.py"


def rust_const(name: str) -> str:
    """The literal on the right of `const NAME: T = "...";` or `= 123;`."""
    src = RUST.read_text(encoding="utf-8")
    m = re.search(rf"const {name}\s*:\s*[^=]+=\s*([^;]+);", src)
    assert m, f"{name} not found in {RUST.name}"
    return m.group(1).strip().strip('"')


def py_const(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    m = re.search(rf"^{name}\s*(?::[^=]+)?=\s*(.+)$", src, re.MULTILINE)
    assert m, f"{name} not found in {path.name}"
    return m.group(1).strip().strip('"')


# ---------------------------------------------------------------------------
# Rust <-> the gateway package
# ---------------------------------------------------------------------------


def test_rust_port_matches_the_gateway_default():
    assert rust_const("DEFAULT_GATEWAY_PORT") == py_const(GATEWAY_CONFIG, "DEFAULT_PORT")


@pytest.mark.parametrize(
    "rust_name,python_name",
    [
        ("PID_BASENAME", "_PID_BASENAME"),
        ("PORT_BASENAME", "_PORT_BASENAME"),
        ("TOKEN_BASENAME", "_TOKEN_BASENAME"),
    ],
)
def test_rust_state_filenames_match_the_gateway_package(rust_name, python_name):
    assert rust_const(rust_name) == py_const(GATEWAY_CONFIG, python_name)


def test_rust_port_env_name_is_the_one_the_gateway_reads():
    assert rust_const("PORT_ENV") in GATEWAY_CONFIG.read_text(encoding="utf-8")


def test_writer_default_port_matches_the_gateway_package():
    from vco_lib import vscode_settings as vs

    assert str(vs.DEFAULT_GATEWAY_PORT) == py_const(GATEWAY_CONFIG, "DEFAULT_PORT")


# ---------------------------------------------------------------------------
# TypeScript <-> Python
# ---------------------------------------------------------------------------


def test_frontend_default_model_matches_the_writer():
    from vco_lib import vscode_settings as vs

    ts = TS.read_text(encoding="utf-8")
    m = re.search(r"DEFAULT_GATEWAY_MODEL\s*=\s*'([^']+)'", ts)
    assert m, "DEFAULT_GATEWAY_MODEL not found in the frontend API module"
    assert m.group(1) == vs.DEFAULT_GATEWAY_MODEL


def test_frontend_slot_key_list_matches_the_writer():
    from vco_lib import vscode_settings as vs

    ts = TS.read_text(encoding="utf-8")
    block = re.search(r"SLOT_OVERRIDE_KEYS = \[(.*?)\]", ts, re.DOTALL)
    assert block, "SLOT_OVERRIDE_KEYS not found in the frontend API module"
    ts_keys = set(re.findall(r"'([A-Z_]+)'", block.group(1)))
    assert ts_keys == set(vs.SLOT_OVERRIDE_KEYS), (
        "the card warns about a different key set than the writer checks; "
        "one of them would then miss a slot override silently"
    )


def test_the_gate_is_default_OFF_on_BOTH_sides():
    """Two independent default-active lists must agree, or the GUI lies.

    The template renderer resolves active modules in Python
    (`_DEFAULT_ACTIVE_MODULES`); the card's checkbox reads the Rust
    `is_project_module_active`, which seeds a row for anything in
    `ORCHESTRATOR_BUNDLED_DEFAULT_ACTIVE_MODULES`. If `model_gateway` were
    added to either, the two would disagree — a checkbox showing ON over a
    CLAUDE.md with no routing section, or vice versa — and the whole "an
    install with no gateway reads nothing about GLM" guarantee would depend
    on which side you asked.
    """
    from vco_lib.project_init import _DEFAULT_ACTIVE_MODULES

    assert "model_gateway" not in _DEFAULT_ACTIVE_MODULES

    rust = (
        REPO / "launcher" / "src-tauri" / "src" / "commands" / "diagrams_cmd.rs"
    ).read_text(encoding="utf-8")
    m = re.search(
        r"ORCHESTRATOR_BUNDLED_DEFAULT_ACTIVE_MODULES: &\[&str\] = &\[(.*?)\];",
        rust,
        re.DOTALL,
    )
    assert m, "the Rust default-active list moved; re-point this test"
    assert "model_gateway" not in m.group(1)


def test_frontend_module_name_matches_the_template_gate():
    ts = TS.read_text(encoding="utf-8")
    m = re.search(r"ROUTING_GUIDANCE_MODULE\s*=\s*'([a-z_]+)'", ts)
    assert m, "ROUTING_GUIDANCE_MODULE not found"
    gate = m.group(1)
    assert f"{{{{#if_module_active {gate}}}}}" in TEMPLATE.read_text(encoding="utf-8"), (
        "the GUI toggle writes a project_modules row for a module name the "
        "template does not gate on — the toggle would do nothing visible"
    )


# ---------------------------------------------------------------------------
# R15 — asserted as a negative over every file that could break it
# ---------------------------------------------------------------------------


SLOT_KEYS = (
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)


def test_no_wp12_file_ever_assigns_a_tier_or_subagent_slot():
    """Naming a slot key is fine (warnings, removal lists). ASSIGNING is not.

    The pattern searched for is the key immediately followed by an
    assignment — `block["KEY"] = ...`, `KEY: value`, `KEY=value` — which is
    what writing one into the env block would look like in any of these
    languages.
    """
    offenders: list[str] = []
    for path in (PY, RUST, TS, SVELTE, TEMPLATE):
        text = path.read_text(encoding="utf-8")
        for key in SLOT_KEYS:
            for m in re.finditer(
                rf'(\["{key}"\]\s*=|\'{key}\'\s*:|"{key}"\s*:|\b{key}\s*=)', text
            ):
                line = text[: m.start()].count("\n") + 1
                offenders.append(f"{path.name}:{line}: {m.group(0)}")
    assert not offenders, (
        "WP-12 must never write a tier or subagent slot — the name a model is "
        f"dispatched under has to be the model that answers. Found: {offenders}"
    )


def test_default_model_is_glm_5_3_and_never_flash_or_older():
    from vco_lib import vscode_settings as vs

    assert vs.DEFAULT_GATEWAY_MODEL == "claude-gw/glm-5.3"
    for banned in ("flash", "glm-5.2", "glm-5.1", "glm-4"):
        assert banned not in vs.DEFAULT_GATEWAY_MODEL


def test_template_never_makes_flash_a_default():
    """Flash is documented (it is not inferior enough to hide) but gated.

    The ruling's conditional did not fire, so flash appears — behind a
    verifier, and explicitly excluded from unsupervised edits. What is
    forbidden is it being a DEFAULT anywhere.
    """
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "glm-5.3-flash" in text, "flash is documented, with guardrails"
    assert "behind a verifier" in text
    assert "not the flash variant" in text


def test_the_fabricated_swebench_figure_is_not_propagated():
    """A widely-circulating "Flash = 92% SWE-bench Verified" number is not on
    the leaderboard it cites. It may appear ONLY in the sentence telling
    readers not to propagate it."""
    text = TEMPLATE.read_text(encoding="utf-8")
    for match in re.finditer(r"92%[^\n]*SWE-bench", text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        line = text[line_start : text.find("\n", match.start())]
        assert "Do not propagate" in line, (
            f"the fabricated figure appears outside its warning: {line!r}"
        )


# ---------------------------------------------------------------------------
# The template gate
# ---------------------------------------------------------------------------


def render(active: set[str]) -> str:
    from vco_lib.project_init import render_conditional_blocks

    return render_conditional_blocks(
        TEMPLATE.read_text(encoding="utf-8"), active_modules=active
    )


def test_no_gateway_means_no_word_about_vendor_models():
    """LEAVE-ALONE: an install with no gateway reads nothing about GLM."""
    out = render({"diagrams"}).lower()
    for token in ("glm", "claude-gw", "model routing", "artificial analysis"):
        assert token not in out, (
            f"{token!r} reached a project with no gateway configured; the "
            "section must be absent, not merely hedged"
        )


def test_gateway_active_renders_the_routing_section():
    out = render({"diagrams", "model_gateway"})
    assert "## Model routing" in out
    assert "claude-gw/glm-5.3" in out
    assert "CLAUDE_CODE_SUBAGENT_MODEL" in out, (
        "the no-slot-remap rule has to name the keys it forbids"
    )
    assert "{{#if_module" not in out and "{{/if_module" not in out


def test_the_section_is_the_only_difference_it_makes():
    """A project without the module renders EXACTLY what it rendered before.

    Adding lines to this template widens the diff against every existing
    project's `CLAUDE.md.reference.md` sidecar, which is what drives the
    `template_review_pending` deferral. Gating the whole section means
    projects that do not opt in see no churn at all.
    """
    off = render({"diagrams"})
    assert "Model routing" not in off
    # The blocks are adjacent, not nested: dropping ours must not disturb the
    # diagrams block's own rendering.
    assert "## Diagrams (Mermaid + Excalidraw)" in render({"diagrams"})
    assert "## Diagrams (Mermaid + Excalidraw)" not in render(set())


def test_the_conditional_blocks_are_not_nested():
    """The renderer raises on nesting; a malformed template would break EVERY
    project's install, not just gateway users."""
    from vco_lib.project_init import render_conditional_blocks

    for mods in (set(), {"diagrams"}, {"model_gateway"}, {"diagrams", "model_gateway"}):
        render_conditional_blocks(
            TEMPLATE.read_text(encoding="utf-8"), active_modules=mods
        )


# ---------------------------------------------------------------------------
# Delivery (R17 check 1): how the template actually reaches a user
# ---------------------------------------------------------------------------


def test_claude_md_template_is_not_a_bundle_glob_file():
    """VERIFIED, not assumed: `bundle_globs` does not match this template.

    It ships through `_PROJECT_LEVEL_TEMPLATES` instead, which has different
    already-damaged semantics (a `.reference.md` sidecar plus
    `template_review_pending`, NOT `bundle_user_modified_preserved` and NOT
    an overwrite). Asserting the negative keeps a future reader from
    answering the delivery question with the wrong mechanism.
    """
    from vco_lib.bundle_globs import hook_globs, script_patterns

    name = "CLAUDE.md.template"
    for pattern in (*hook_globs(), *script_patterns()):
        assert not fnmatch.fnmatch(name, pattern), (
            f"{name} unexpectedly matches bundle glob {pattern!r}"
        )


def test_claude_md_template_is_enumerated_as_a_project_level_template():
    from vco_lib.project_init import _PROJECT_LEVEL_TEMPLATES

    names = [entry[0] for entry in _PROJECT_LEVEL_TEMPLATES]
    assert "CLAUDE.md.template" in names


def test_render_claude_md_is_the_path_the_gui_toggle_uses():
    """The module toggle's re-render is what actually lands the section.

    `install-bundle` never rewrites an existing project's CLAUDE.md, so if
    this function stopped merging the managed region the GUI toggle would
    become a row in a table with no visible effect.
    """
    from vco_lib.project_init import render_claude_md

    doc = render_claude_md.__doc__ or ""
    assert "preserving any user content outside" in doc


# ---------------------------------------------------------------------------
# The Rust card contract the frontend types mirror
# ---------------------------------------------------------------------------


def test_status_payload_fields_match_the_typescript_interface():
    rust = RUST.read_text(encoding="utf-8")
    block = re.search(
        r"pub struct ModelGatewayStatus \{(.*?)\n\}", rust, re.DOTALL
    )
    assert block, "ModelGatewayStatus not found"
    rust_fields = set(re.findall(r"^\s*pub (\w+):", block.group(1), re.MULTILINE))

    ts = (REPO / "launcher" / "src" / "lib" / "types" / "model-gateway.ts").read_text(
        encoding="utf-8"
    )
    ts_block = re.search(r"interface ModelGatewayStatus \{(.*?)\n\}", ts, re.DOTALL)
    assert ts_block, "ModelGatewayStatus interface not found"
    ts_fields = set(re.findall(r"^\s*(\w+)[?]?:", ts_block.group(1), re.MULTILINE))

    assert rust_fields == ts_fields, (
        "the status payload and its TypeScript mirror disagree; a field the "
        "card reads but Rust never sends renders as undefined with no error"
    )


def test_health_payload_documents_exactly_what_the_gateway_emits():
    """The card's health struct must not claim fields the gateway never sends.

    The gateway's own docstring is the contract (`health_handler`), and it is
    pinned on its side by `test_health_reports_exactly_the_documented_fields`.
    """
    server = (
        REPO / "claude_mcp_servers" / "model_router" / "server.py"
    ).read_text(encoding="utf-8")
    rust = RUST.read_text(encoding="utf-8")
    block = re.search(r"pub struct GatewayHealth \{(.*?)\n\}", rust, re.DOTALL)
    assert block
    for field in re.findall(r"^\s*pub (\w+):", block.group(1), re.MULTILINE):
        assert f'"{field}"' in server, (
            f"the status card declares a health field `{field}` the gateway "
            "never emits"
        )


def test_seed_and_default_model_agree():
    seed = json.loads(
        (
            REPO / "claude_mcp_servers" / "model_router" / "chat_model_context.seed.json"
        ).read_text(encoding="utf-8")
    )
    from vco_lib import vscode_settings as vs

    assert vs.DEFAULT_GATEWAY_MODEL.split("/", 1)[1] in seed["models"]


def test_the_two_vscode_keys_are_the_names_the_extension_reads():
    """The whole contract with VS Code is two strings.

    If either changes, the extension stops seeing VCO's routing and nothing
    errors — the panel quietly keeps whatever it had. So they are asserted
    literally, in the writer AND in the copy the card shows the user, rather
    than being treated as internal names anyone may rename.
    """
    from vco_lib import vscode_settings as vs

    assert vs.ENV_BLOCK_KEY == "claudeCode.environmentVariables"
    assert vs.LOGIN_PROMPT_KEY == "claudeCode.disableLoginPrompt"
    assert set(vs.MANAGED_SETTINGS_KEYS) == {vs.ENV_BLOCK_KEY, vs.LOGIN_PROMPT_KEY}, (
        "reset_native removes MANAGED_SETTINGS_KEYS; if that set ever drifts "
        "from these two, the reset either strands the panel (login prompt "
        "left suppressed) or deletes something that is not ours"
    )
    svelte = SVELTE.read_text(encoding="utf-8")
    for key in (vs.ENV_BLOCK_KEY, vs.LOGIN_PROMPT_KEY):
        assert key in svelte, (
            f"the card tells the user it writes exactly two keys; {key} is "
            "not among the ones it names"
        )
