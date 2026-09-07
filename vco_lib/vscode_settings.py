# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Point the VS Code Claude Code panel at the local model gateway — or back.

This module is a WRITER OF A USER-OWNED FILE OUTSIDE THE PROJECT. VS Code's
global ``settings.json`` belongs to the user, holds settings for every
extension they have installed, and is the file VS Code itself rewrites when
they click anything in the settings editor. Everything below is shaped by
that: the writer touches exactly two keys, refuses rather than guesses,
backs up before it changes anything, and reports what it left alone.

Why this file exists at all
---------------------------
The VS Code Claude Code extension does NOT read ``.claude/settings.json`` for
its login/routing decision. It reads VS Code's own GLOBAL ``settings.json``,
under ``claudeCode.environmentVariables``. So the "point the panel at the
gateway" action cannot go through the config channel that configures
everything else in VCO — it has to write this file. (Per-WORKSPACE
``.vscode/settings.json`` is NOT a way around this, and not merely an
untested one: ``claudeCode.environmentVariables`` is declared with VS Code
setting scope ``machine`` in the extension's ``package.json`` (verified
2026-09-04), so a per-workspace override cannot give one workspace a
native panel while others stay on the gateway.)

The two keys, and why the reset must remove BOTH
-------------------------------------------------
* ``claudeCode.environmentVariables`` — a string->string map injected into
  the extension's environment. The gateway routing lives here.
* ``claudeCode.disableLoginPrompt`` — suppresses the interactive login while
  env-auth is in force. Leaving this ``true`` after removing the environment
  block strands the user: env-auth is gone AND the login flow is suppressed,
  so the panel has no way back. :func:`reset_native` therefore removes both,
  and a test pins that it removes exactly those two.

What "point at gateway" writes — and what it must never write
--------------------------------------------------------------
Written: ``ANTHROPIC_BASE_URL``, ``ANTHROPIC_AUTH_TOKEN`` (the gateway's
loopback host token), ``ANTHROPIC_API_KEY=""``,
``CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1``; plus ``ANTHROPIC_MODEL``
ONLY when the caller passes an explicit choice.

NEVER written: ``ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU,FABLE}_MODEL``,
``ANTHROPIC_SMALL_FAST_MODEL``, ``CLAUDE_CODE_SUBAGENT_MODEL``. Pointing a
tier slot at a vendor model is a hidden substitution: the user selects
"Sonnet" in the picker and something else answers. Model selection stays the
user's, made in the ``/model`` picker, visible. This is not a style
preference — it is the whole reason the gateway namespaces vendor ids as
``claude-gw/<id>`` instead of relabelling them.

That omission is SAFE here specifically because the gateway serves real
Claude models under their real names: a built-in ``claude-*`` id that is not
overridden reaches Anthropic through the gateway's claude route. Pointed
DIRECTLY at a third-party endpoint the same omission would be dangerous —
those endpoints answer ``claude-*`` names with their own small model, HTTP
200, no error (documented vendor behaviour, docs.z.ai). The gateway is what
makes leaving the slots unset the honest choice.

The ONE permitted touch on those values — the ``[1m]`` decoration (R41)
-----------------------------------------------------------------------
A literal model id that never passes through the gateway's ``/v1/models``
catalog (a tier-slot override, a subagent slot, ``ANTHROPIC_MODEL`` itself)
gets the client's conservative default context window, so a 1M-window model
read as far fuller than it was and a session auto-compacted on the next
turn (observed live, 2026-09-04). R41 relaxes the NEVER-write rule above by
exactly one operation, and the boundary is hard:

* PERMITTED — appending the client's context-window hint ``[1m]`` to the
  SAME model id, when the version-keyed table
  (``model_router.context_table``, exact full-id match, never a wildcard:
  ``glm-5.2`` is 1M while ``glm-5.1`` is 200K) marks it 1M-windowed. The
  gateway strips the suffix before routing, so the model that answers is
  unchanged. Every healed value is reported by name in the done message.
* FORBIDDEN — everything else, above all changing which model an id names
  (``glm-5.3-flash`` → ``glm-5.3[1m]`` would be a hidden substitution, not
  a decoration), and any change to a value the table does not vouch for.

:func:`decorate_1m` is that operation, and it is a silent no-op when the
table is unavailable or unreadable — this module runs at session startup
and must never be the reason a session fails to start.

The two-state mode switch — ``multimodel`` <-> ``remote-control``
------------------------------------------------------------------
Claude Code's Remote Control (phone / claude.ai control of this machine) is
ENDPOINT-GATED: from 2.1.196 the client refuses to start it whenever
``ANTHROPIC_BASE_URL`` is anything but api.anthropic.com, claude.ai login or
not. And because ``claudeCode.environmentVariables`` is machine-scoped there
is no per-workspace split. So the user gets exactly one of the two at a
time, and :func:`set_mode` is the switch:

* ``remote-control`` — remove the four routing keys and the login-prompt
  key (what :func:`reset_native` does) but KEEP the rest of the env block:
  the user's other keys, and every ``ANTHROPIC_MODEL`` / tier / subagent
  value that names a model the stock client can resolve (a real Claude id).
  Only values that CANNOT resolve natively — ``claude-gw/<id>`` names, or
  ids the context table attributes to a third-party vendor — are dropped,
  and they are STASHED (with the routing key NAMES; never the token or the
  api-key value, which are re-derived from the gateway's token file on the
  way back) in ``<vct_root>/model-gateway/vscode-mode-stash.json`` (0600).
* ``multimodel`` — :func:`point_at_gateway` with the stashed model and slot
  values put back, then the stash is cleared. A missing stash is a plain
  point.

Restoring a stashed slot value is the ONE case in which this module writes
a tier/subagent key, and it is not an exception to the NEVER-write rule so
much as its corollary: the value is the user's own, made in their file and
taken out of it by the other leg of the same switch. It is put back
verbatim (plus the ``[1m]`` decoration below), only into the file it was
taken from, and only for the ``ANTHROPIC_MODEL`` / slot keys — a stash
cannot inject a routing key or any other name into the env block.

Both legs are idempotent: re-applying the current mode changes nothing and
leaves the stash exactly as it was, so a second click cannot destroy the
choices the first one saved.

Merge, never replace
--------------------
:func:`point_at_gateway` carries forward every key it does not itself write
and reports them in ``keys_preserved``. The field prototype rewrote the whole
block, so re-running it silently reset the user's ``ANTHROPIC_MODEL`` choice.
A tier/subagent override already present in the file is preserved too (it is
the user's key, not ours to delete) but reported separately in
``slot_overrides_preserved`` so the GUI can say so out loud; passing
``remove_slot_overrides=True`` is the explicit, user-initiated way to drop
them.

JSONC
-----
VS Code accepts comments and trailing commas in ``settings.json``. Parsing
that with :func:`json.loads` raises, and "helpfully" re-serialising a parsed
form would silently delete the user's comments. So a file that is not strict
JSON is REFUSED, byte-for-byte untouched, with the exact key block the user
should paste returned in ``paste_block``.

Permissions
-----------
The value written into ``ANTHROPIC_AUTH_TOKEN`` is a credential (it
authorises proxying under the user's Claude login and paid vendor
subscription — bounded by the gateway binding loopback only, but a
credential). The file and every backup are restricted to the owner after
each write via :mod:`model_router.fileperms`, which is the ONE cross-OS
implementation of that operation with a loud-failure contract (``chmod 0600``
on POSIX, an inheritance-breaking ACL on Windows, an exception when neither
can be applied). If the restriction cannot be applied the write is ROLLED
BACK from the backup — a token must not be left in a file this module could
not lock down.

Known caveat, stated rather than hidden: VS Code's own settings editor may
rewrite ``settings.json`` with default permissions, undoing the restriction.
:func:`inspect_target` re-probes on every status refresh so the GUI can warn,
and the probe is a TRI-STATE — ``unknown`` when it cannot tell, never a
reassuring ``owner_only``.

Import direction
----------------
``vco_lib`` importing ``model_router`` inverts the usual layering (the root
distribution is installed first). It is deliberate: ``fileperms`` is the only
implementation in the tree that fails loudly instead of no-opping on Windows,
and adding a fourth answer to "restrict this file to its owner" is exactly
what the modularity rule forbids. ``install.py`` step 4/10 runs
``pip install -e claude_mcp_servers/`` on every install where that
directory's ``pyproject.toml`` exists — i.e. every clone — so the import
resolves on every healthy install; a failure means a broken install and
surfaces loudly. The register's item 24 (extract to ``vco_lib/fileperms.py``,
which would put the layering back) is a merge-lane item; this module follows
it with a one-line import change when it lands.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlsplit

# `pyright: ignore` here and at the two `model_router.config` imports below is
# a TOOLING gap, not a runtime one: `pyrightconfig.json` analyses `vco_lib`,
# `weaviate_mcp` and `search_mcp`, and has no `extraPaths` entry putting
# `claude_mcp_servers/` on the analysis path — so `model_router` is invisible
# to the checker while resolving perfectly at runtime (`install.py` runs
# `pip install -e claude_mcp_servers/`, and `tests/test_vscode_settings.py`
# imports this module and exercises these symbols). The config's own comment
# names the per-line ignore + rationale as the sanctioned response. The real
# fix is register item 22 + 24 (add the package to pyrightconfig, then move
# `fileperms` to `vco_lib/`), both merge-lane items; when they land, delete
# these three ignores and this comment.
from model_router.fileperms import (  # pyright: ignore[reportMissingImports]
    OwnerOnlyState,
    PermissionHardeningError,
    owner_only_state,
    restrict_to_owner,
)

from vco_lib.atomic import atomic_copy_file, atomic_write_text
from vco_lib.paths import user_home

# ---------------------------------------------------------------------------
# The two keys this module is allowed to touch, and the env-block key names.
# ---------------------------------------------------------------------------

#: VS Code settings key holding the extension's injected environment.
ENV_BLOCK_KEY = "claudeCode.environmentVariables"

#: VS Code settings key suppressing the interactive login while env-auth runs.
LOGIN_PROMPT_KEY = "claudeCode.disableLoginPrompt"

#: The complete set of top-level settings keys this module may add, change or
#: remove. Anything else in the file is the user's and is copied verbatim.
MANAGED_SETTINGS_KEYS = (ENV_BLOCK_KEY, LOGIN_PROMPT_KEY)

#: Env-block keys "point at gateway" always writes.
ROUTING_KEYS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
)

#: Env-block key written ONLY when the user explicitly picks a model.
MODEL_KEY = "ANTHROPIC_MODEL"

#: Env-block keys this module must NEVER write. Each one silently re-points a
#: name the user selected at a different model; see the module docstring.
SLOT_OVERRIDE_KEYS = (
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)

#: The GLM id the GUI pre-selects when the user chooses to set a default at
#: all. GLM-5.3, never the flash variant and never a pre-5.3 version: flash
#: breaks an already-passing baseline test in 6.9% of agent rollouts (vs 4.4%
#: for 5.3) and longer runs help it only 46% of the time, so it is a
#: verified-breadth model, not a default. Pinned against the shipped context
#: seed by ``tests/test_vscode_settings.py``.
DEFAULT_GATEWAY_MODEL = "claude-gw/glm-5.3"

#: The gateway's documented port. Duplicated from ``model_router.config`` on
#: purpose: :func:`is_vco_gateway_base_url` must still recognise a panel
#: pointed at a gateway whose package has been uninstalled, and the pure
#: function takes its ports as an argument so callers choose the source.
DEFAULT_GATEWAY_PORT = 11436

#: Vendor-model namespace the gateway serves ids under. Duplicated from
#: ``model_router.vendors`` for the same reason as the port: the decoration
#: below must keep recognising gateway ids even when the package is gone.
GATEWAY_ID_PREFIX = "claude-gw/"

#: The client-side context-window hint :func:`decorate_1m` may append. The
#: gateway strips it before routing — it changes the window the client
#: ASSUMES, never the model that answers.
CONTEXT_1M_SUFFIX = "[1m]"

#: Env var carrying the host token to this process. Present so a caller that
#: already holds the token can pass it without it ever appearing in argv (or
#: in shell history, or in a ps listing). Absent is the normal case: the
#: token is then read from the gateway's own token file, which keeps it from
#: crossing a process boundary at all.
ENV_TOKEN = "VCT_GW_TMP_TOKEN"

#: Env var overriding target discovery: an ``os.pathsep``-separated list of
#: absolute ``settings.json`` paths. For portable installs, ``--user-data-dir``
#: setups and any VS Code variant this module does not know by name.
ENV_TARGET_OVERRIDE = "VCT_VSCODE_SETTINGS_FILES"

#: The two states of the switch, plus the two a probe can report but the
#: switch never writes.
MODE_MULTIMODEL = "multimodel"
MODE_REMOTE_CONTROL = "remote-control"
MODE_UNMANAGED = "unmanaged"
MODE_UNPARSEABLE = "unparseable"
MODES = (MODE_MULTIMODEL, MODE_REMOTE_CONTROL)

#: ``vendor`` value of a first-party (Claude) row in the chat-model context
#: table. Duplicated from ``model_router.vendors.ANTHROPIC_FAMILY.family_id``
#: for the same reason as the port and the namespace above; pinned by
#: ``tests/test_vscode_settings.py``. A row whose vendor is anything else
#: names a model only the gateway can serve.
FIRST_PARTY_VENDOR = "anthropic"

#: Where the mode switch keeps the choices it takes out of the file.
#: ``<vct_root>/model-gateway/`` is the gateway's own state subdirectory
#: (``model_router.config._STATE_SUBDIR``), so everything about the gateway
#: lives in one place and an uninstall sweep finds it.
STASH_SUBDIR = "model-gateway"
STASH_BASENAME = "vscode-mode-stash.json"
_STASH_SCHEMA_VERSION = 1

#: Routing keys whose VALUE is a credential (or its deliberate blank). Their
#: names are stashed; their values never are — the token is re-read from the
#: gateway's token file when the panel is pointed again.
_STASH_SECRET_KEYS = frozenset({"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"})

_BACKUP_STEM = ".bak-"


# ---------------------------------------------------------------------------
# Target discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VSCodeVariant:
    """One VS Code-family application and its per-OS user-settings directory.

    ``linux_dir`` / ``macos_dir`` / ``windows_dir`` are the directory names
    the app uses under its platform's config root; they differ per app AND
    per platform (Cursor is ``Cursor`` everywhere, VS Code Insiders is
    ``Code - Insiders``), which is exactly why this is a table and not a
    format string.
    """

    app_id: str
    display_name: str
    linux_dir: str
    macos_dir: str
    windows_dir: str
    #: Flatpak application id, when the app ships one. Flatpak redirects the
    #: config root to ``~/.var/app/<id>/config``, so a Flatpak VS Code's
    #: settings are NOT under ``~/.config`` and would otherwise be invisible.
    flatpak_id: Optional[str] = None


VARIANTS: tuple[VSCodeVariant, ...] = (
    VSCodeVariant(
        app_id="code",
        display_name="VS Code",
        linux_dir="Code",
        macos_dir="Code",
        windows_dir="Code",
        flatpak_id="com.visualstudio.code",
    ),
    VSCodeVariant(
        app_id="code-insiders",
        display_name="VS Code Insiders",
        linux_dir="Code - Insiders",
        macos_dir="Code - Insiders",
        windows_dir="Code - Insiders",
        flatpak_id="com.visualstudio.code.insiders",
    ),
    VSCodeVariant(
        app_id="vscodium",
        display_name="VSCodium",
        linux_dir="VSCodium",
        macos_dir="VSCodium",
        windows_dir="VSCodium",
        flatpak_id="com.vscodium.codium",
    ),
    VSCodeVariant(
        app_id="cursor",
        display_name="Cursor",
        linux_dir="Cursor",
        macos_dir="Cursor",
        windows_dir="Cursor",
        flatpak_id=None,
    ),
)


@dataclass(frozen=True)
class Target:
    """One discovered ``settings.json``."""

    app_id: str
    display_name: str
    path: str
    #: ``"native"`` or ``"flatpak"`` (Linux only) or ``"override"`` for a path
    #: supplied through :data:`ENV_TARGET_OVERRIDE`.
    flavour: str

    def as_dict(self) -> dict:
        return asdict(self)


def _os_key(platform_key: Optional[str] = None) -> str:
    """``"linux"`` / ``"darwin"`` / ``"windows"``; explicit beats ``sys``.

    Taking the platform as an argument is what makes every path decision in
    this module unit-testable on all three OSes from one machine.
    """
    raw = (platform_key or sys.platform).lower()
    if raw.startswith("win"):
        return "windows"
    if raw.startswith("darwin") or raw == "macos":
        return "darwin"
    return "linux"


def candidate_paths(
    *,
    platform_key: Optional[str] = None,
    home: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> list[Target]:
    """Every path a VS Code-family app COULD use on this OS. No existence check.

    Split from :func:`detect_targets` so the per-OS path shapes can be
    asserted for Windows and macOS from a Linux test run — "only verifiable
    on one OS" is not an acceptable acceptance criterion here.
    """
    env = os.environ if env is None else env
    root_home = Path(home) if home is not None else user_home()
    os_key = _os_key(platform_key)
    out: list[Target] = []

    for variant in VARIANTS:
        if os_key == "windows":
            appdata = (env.get("APPDATA") or "").strip()
            base = (
                Path(appdata)
                if appdata
                else root_home / "AppData" / "Roaming"
            )
            out.append(
                Target(
                    app_id=variant.app_id,
                    display_name=variant.display_name,
                    path=str(base / variant.windows_dir / "User" / "settings.json"),
                    flavour="native",
                )
            )
            continue
        if os_key == "darwin":
            base = root_home / "Library" / "Application Support"
            out.append(
                Target(
                    app_id=variant.app_id,
                    display_name=variant.display_name,
                    path=str(base / variant.macos_dir / "User" / "settings.json"),
                    flavour="native",
                )
            )
            continue
        # Linux (and any other POSIX): XDG_CONFIG_HOME, else ~/.config.
        xdg = (env.get("XDG_CONFIG_HOME") or "").strip()
        base = Path(xdg) if xdg else root_home / ".config"
        out.append(
            Target(
                app_id=variant.app_id,
                display_name=variant.display_name,
                path=str(base / variant.linux_dir / "User" / "settings.json"),
                flavour="native",
            )
        )
        if variant.flatpak_id:
            out.append(
                Target(
                    app_id=variant.app_id,
                    display_name=f"{variant.display_name} (Flatpak)",
                    path=str(
                        root_home
                        / ".var"
                        / "app"
                        / variant.flatpak_id
                        / "config"
                        / variant.linux_dir
                        / "User"
                        / "settings.json"
                    ),
                    flavour="flatpak",
                )
            )

    # Overrides come LAST so that when one names a file a known variant also
    # names, `detect_targets`' de-duplication keeps the variant's friendlier
    # label ("VS Code") instead of the generic one.
    override = (env.get(ENV_TARGET_OVERRIDE) or "").strip()
    if override:
        for raw in override.split(os.pathsep):
            piece = raw.strip()
            if piece:
                out.append(
                    Target(
                        app_id="override",
                        display_name="Configured settings file",
                        path=str(Path(piece)),
                        flavour="override",
                    )
                )
    return out


def detect_targets(
    *,
    platform_key: Optional[str] = None,
    home: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> list[Target]:
    """Candidates whose ``settings.json`` EXISTS, de-duplicated by real path.

    Only existing files are offered. A path that does not exist is not
    evidence the app is installed, and creating ``settings.json`` for an app
    the user does not have would leave a stray file behind forever.

    De-duplication is by resolved path, so an override that names the same
    file as a detected variant is reported once (with the variant's name,
    which is the more useful label).
    """
    seen: set[str] = set()
    out: list[Target] = []
    for cand in candidate_paths(platform_key=platform_key, home=home, env=env):
        p = Path(cand.path)
        try:
            if not p.is_file():
                continue
            key = str(p.resolve())
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(cand)
    # Named variants before overrides so the friendlier label wins in the GUI
    # when both point at the same file (the de-dup above already collapsed
    # them; this only orders the remainder).
    out.sort(key=lambda t: (t.flavour == "override", t.app_id))
    return out


# ---------------------------------------------------------------------------
# Reading / classification (pure where it can be)
# ---------------------------------------------------------------------------


class SettingsRefused(Exception):
    """The file could not be safely edited. Nothing was written.

    ``reason`` is a stable machine code; ``message`` is for the user.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


_COMMENT_HINT = re.compile(r"(^|\s)(//|/\*)")
_TRAILING_COMMA_HINT = re.compile(r",\s*[}\]]")


def describe_json_failure(text: str, error: str) -> str:
    """Name the LIKELY cause of a strict-JSON failure, in the user's terms.

    VS Code accepts JSON with comments and trailing commas; ``json.loads``
    does not. Saying "expecting property name" to someone whose file simply
    has a ``// note`` on line 3 is a diagnostic dead end.
    """
    hints: list[str] = []
    if _COMMENT_HINT.search(text):
        hints.append("comments (`//` or `/* */`)")
    if _TRAILING_COMMA_HINT.search(text):
        hints.append("a trailing comma before `}` or `]`")
    if hints:
        return (
            "This settings.json is JSONC — VS Code accepts it, strict JSON "
            f"does not. Found {' and '.join(hints)}. VCO will not rewrite the "
            "file, because re-serialising it would silently delete your "
            f"comments. (Parser said: {error})"
        )
    return (
        "This settings.json is not valid JSON, so VCO will not rewrite it — "
        f"an edit could destroy content. (Parser said: {error})"
    )


def _read_text(path: Path) -> str:
    """Read with newline translation DISABLED.

    ``Path.read_text`` applies universal newlines, so a CRLF file arrives as
    LF and :func:`sniff_newline` would never see the CRLF it exists to
    detect — the writer would then silently convert a Windows user's whole
    settings file to LF.
    """
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            return handle.read()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise SettingsRefused(
            "unreadable",
            f"cannot read {path}: {exc}",
        ) from exc
    except UnicodeDecodeError as exc:
        raise SettingsRefused(
            "not_utf8",
            f"{path} is not valid UTF-8, so VCO will not rewrite it ({exc}).",
        ) from exc


def sniff_indent(text: str) -> str:
    """The file's own indentation, so a rewrite does not reformat it.

    Returns a tab or a run of spaces taken from the first indented line;
    falls back to four spaces, which is what VS Code writes.
    """
    for line in text.splitlines():
        if not line.strip():
            continue
        stripped = line.lstrip(" \t")
        prefix = line[: len(line) - len(stripped)]
        if prefix:
            return "\t" if prefix.startswith("\t") else prefix
    return "    "


def _load_settings(path: Path) -> tuple[dict, str]:
    """Return ``(settings, original_text)``; raise :class:`SettingsRefused`.

    A missing file yields ``({}, "")`` ONLY when its parent ``User``
    directory exists — that directory is the evidence the app is installed.
    Creating the whole tree would fabricate a config for an editor the user
    may not have.
    """
    try:
        text = _read_text(path)
    except FileNotFoundError:
        if not path.parent.is_dir():
            raise SettingsRefused(
                "no_settings_dir",
                f"{path.parent} does not exist, so this editor does not look "
                "installed. Open the editor once (it creates the folder), or "
                "pick a different target.",
            ) from None
        return {}, ""
    if not text.strip():
        return {}, text
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise SettingsRefused("not_strict_json", describe_json_failure(text, str(exc))) from exc
    if not isinstance(parsed, dict):
        raise SettingsRefused(
            "not_an_object",
            f"{path} does not contain a JSON object at the top level, which "
            "is not a shape VS Code settings can have. Refusing to rewrite it.",
        )
    return parsed, text


def _existing_env_block(settings: Mapping[str, Any], path: Path) -> dict:
    block = settings.get(ENV_BLOCK_KEY)
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise SettingsRefused(
            "env_block_not_an_object",
            f"{path} has `{ENV_BLOCK_KEY}` set to something other than an "
            "object. VCO will not overwrite a value it does not understand — "
            "fix or remove that key by hand first.",
        )
    return dict(block)


def is_loopback_host(host: str) -> bool:
    """Pure: is ``host`` one of the loopback spellings? No name resolution.

    Deliberately does NOT resolve names: this function decides whether a URL
    in a config file is OUR gateway, and a DNS lookup would make that answer
    depend on the network at the moment of a status refresh.
    """
    h = (host or "").strip().strip("[]").lower()
    if h in {"localhost", "127.0.0.1", "::1", "0:0:0:0:0:0:0:1"}:
        return True
    # 127.0.0.0/8 — anything in the loopback block.
    parts = h.split(".")
    if len(parts) == 4 and parts[0] == "127":
        return all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)
    return False


def is_vco_gateway_base_url(
    url: Optional[str],
    *,
    ports: Optional[Sequence[int]] = None,
) -> bool:
    """Pure: does ``url`` name a VCO model gateway on this machine?

    True only for an ``http`` loopback URL on one of ``ports``. Used by the
    uninstall path, whose whole contract is "reset the panel we pointed at
    our gateway, and leave any other value the user set completely alone" —
    so a false positive here would clobber someone else's configuration.

    ``ports`` defaults to the gateway's documented port. A caller that can
    reach ``model_router.config.resolve_port()`` should pass both it and the
    default: after an uninstall the port FILE is gone, and the recorded URL
    must still be recognised.
    """
    if not url:
        return False
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"}:
        return False
    if not is_loopback_host(parts.hostname or ""):
        return False
    allowed = tuple(ports) if ports else (DEFAULT_GATEWAY_PORT,)
    try:
        port = parts.port
    except ValueError:
        return False
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return port in allowed


def resolve_gateway_ports() -> tuple[int, ...]:
    """The ports a VCO gateway may be reachable on, most specific first.

    Reads ``model_router.config.resolve_port()`` (env, then the port file)
    and always includes the documented default, so the answer survives the
    port file being deleted — which is precisely the state an uninstalled
    gateway leaves behind.
    """
    ports: list[int] = []
    try:
        from model_router.config import (  # pyright: ignore[reportMissingImports]
            resolve_port,
        )

        ports.append(resolve_port())
    except Exception:  # noqa: BLE001 — a broken/absent gateway must not break the probe
        pass
    if DEFAULT_GATEWAY_PORT not in ports:
        ports.append(DEFAULT_GATEWAY_PORT)
    return tuple(ports)


def resolve_host_token(env: Optional[Mapping[str, str]] = None) -> str:
    """The gateway's host token: env override first, then the token file.

    The env override exists so a caller holding the token can hand it over
    without it appearing in argv. The FILE read is the default because it
    keeps the token from crossing a process boundary at all — the fewer
    places a credential is copied, the fewer places it can be logged.
    """
    env = os.environ if env is None else env
    direct = (env.get(ENV_TOKEN) or "").strip()
    if direct:
        return direct
    from model_router.config import (  # pyright: ignore[reportMissingImports]
        token_path,
    )

    path = token_path()
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SettingsRefused(
            "no_host_token",
            f"the gateway's host token file ({path}) could not be read: {exc}. "
            "Start the model gateway once — it creates the token on first "
            "run — then try again.",
        ) from exc


def inspect_target(
    path: Path,
    *,
    ports: Optional[Sequence[int]] = None,
) -> dict:
    """Describe one settings file without changing a byte of it.

    Every field is honest about not knowing: an unparseable file reports
    ``parseable: false`` and leaves the routing fields ``None`` rather than
    guessing, and ``permissions`` is the tri-state from
    ``model_router.fileperms``.
    """
    path = Path(path)
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "parseable": None,
        "refusal_reason": None,
        "message": None,
        "points_at_vco_gateway": None,
        "base_url": None,
        "model": None,
        "discovery_enabled": None,
        "disable_login_prompt": None,
        "slot_overrides": [],
        "managed_keys_present": [],
        "permissions": "unknown",
    }
    if path.is_file():
        result["permissions"] = _probe_permissions(path)
    if not result["exists"]:
        result["parseable"] = None
        return result
    try:
        settings, _text = _load_settings(path)
    except SettingsRefused as exc:
        result["parseable"] = False
        result["refusal_reason"] = exc.reason
        result["message"] = exc.message
        return result
    result["parseable"] = True
    result["managed_keys_present"] = [
        k for k in MANAGED_SETTINGS_KEYS if k in settings
    ]
    result["disable_login_prompt"] = settings.get(LOGIN_PROMPT_KEY)
    block = settings.get(ENV_BLOCK_KEY)
    if not isinstance(block, dict):
        result["points_at_vco_gateway"] = False
        return result
    base_url = block.get("ANTHROPIC_BASE_URL")
    result["base_url"] = base_url if isinstance(base_url, str) else None
    model = block.get(MODEL_KEY)
    result["model"] = model if isinstance(model, str) else None
    result["discovery_enabled"] = str(
        block.get("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY", "")
    ).strip() in {"1", "true", "True"}
    result["slot_overrides"] = [k for k in SLOT_OVERRIDE_KEYS if k in block]
    result["points_at_vco_gateway"] = is_vco_gateway_base_url(
        result["base_url"], ports=ports,
    )
    return result


def _probe_permissions(path: Path) -> OwnerOnlyState:
    try:
        return owner_only_state(path)
    except Exception:  # noqa: BLE001 — a probe that raises answers "unknown"
        return "unknown"


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def paste_block(
    *,
    base_url: str,
    token: str,
    model: Optional[str] = None,
    redact_token: bool = True,
) -> str:
    """The exact keys a user must paste when VCO refuses to write the file.

    This string is shipped code: it is pasted into a real settings file, so
    it must be valid JSON with the right key names, and it must not silently
    hand out the token. ``redact_token`` leaves a placeholder plus the path
    to read the real value from — an instruction the user can actually
    follow without the token travelling through a UI, a log or a clipboard.
    """
    env: dict[str, str] = {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": (
            "<paste the contents of the gateway token file>" if redact_token else token
        ),
        "ANTHROPIC_API_KEY": "",
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
    }
    if model:
        env[MODEL_KEY] = model
    body = {ENV_BLOCK_KEY: env, LOGIN_PROMPT_KEY: True}
    return json.dumps(body, indent=4)[1:-1].strip("\n")


def sniff_newline(text: str) -> str:
    """``"\\r\\n"`` when the file already uses CRLF, else ``"\\n"``.

    Rewriting a Windows user's CRLF settings file as LF would show up as a
    whole-file diff in their VCS and is not a change this module was asked to
    make. ``json.dumps`` only ever emits ``\\n``, so the translation is done
    on the way out.
    """
    return "\r\n" if "\r\n" in text else "\n"


def _dump(
    settings: Mapping[str, Any],
    *,
    indent: str,
    trailing_newline: bool,
    newline: str = "\n",
) -> str:
    text = json.dumps(settings, indent=indent, ensure_ascii=False)
    if trailing_newline:
        text += "\n"
    if newline != "\n":
        text = text.replace("\n", newline)
    return text


def _backup_path(path: Path, now: Optional[float] = None) -> Path:
    """A backup name that never overwrites an earlier backup.

    Two writes inside the same second would otherwise collide and the first
    backup — the one holding the state the user actually wants back — would
    be the one destroyed.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    base = path.with_name(path.name + _BACKUP_STEM + stamp)
    if not base.exists():
        return base
    for n in range(1, 1000):
        candidate = path.with_name(f"{base.name}-{n}")
        if not candidate.exists():
            return candidate
    raise SettingsRefused(
        "backup_name_exhausted",
        f"could not find a free backup name beside {path}; nothing was changed.",
    )


def _write_settings(
    path: Path,
    settings: Mapping[str, Any],
    *,
    original_text: str,
    existed: bool,
) -> tuple[Optional[str], OwnerOnlyState]:
    """Back up, write atomically, restrict to owner. Roll back if it cannot.

    Returns ``(backup_path_or_None, permissions_state)``.

    The rollback is the point of this function. Once the new bytes are on
    disk the token is on disk, so a permission failure after the write is
    not something to warn about and move on from — the file goes back to
    what it was (or is removed if we created it) and the caller gets a
    refusal.
    """
    backup: Optional[Path] = None
    if existed:
        backup = _backup_path(path)
        atomic_copy_file(path, backup)
        try:
            # The backup can hold the OLD token; lock it down before the new
            # write, so there is never a window where a readable copy exists.
            restrict_to_owner(backup)
        except PermissionHardeningError as exc:
            try:
                backup.unlink()
            except OSError:
                pass
            raise SettingsRefused(
                "backup_not_lockable",
                f"the backup {backup} could not be restricted to your user "
                f"account ({exc}), and it may contain a credential. Nothing "
                "was changed.",
            ) from exc

    indent = sniff_indent(original_text) if original_text else "    "
    body = _dump(
        settings,
        indent=indent,
        trailing_newline=(
            original_text.endswith(("\n", "\r")) if original_text.strip() else True
        ),
        newline=sniff_newline(original_text),
    )
    atomic_write_text(path, body)
    try:
        restrict_to_owner(path)
    except PermissionHardeningError as exc:
        # The new bytes (including the token) are on disk in a file we could
        # not lock down. Undo.
        if backup is not None:
            atomic_copy_file(backup, path)
        else:
            try:
                path.unlink()
            except OSError:
                pass
        raise SettingsRefused(
            "not_lockable",
            f"{path} could not be restricted to your user account ({exc}), so "
            "the gateway token was rolled back rather than left readable by "
            "other local users on this machine.",
        ) from exc
    return (str(backup) if backup else None), _probe_permissions(path)


# ---------------------------------------------------------------------------
# The [1m] decoration (R41) — see the module docstring for the boundary.
# ---------------------------------------------------------------------------

_CONTEXT_TABLE_LOADER: Any = None


def _context_table() -> Optional[Any]:
    """The chat-model context table, or ``None`` if it cannot be had.

    Never raises: this module runs at session startup and a broken table
    must disable the decoration, not the panel writer. A corrupt export
    file falls back to the shipped seed inside the loader (warning logged
    there), so corruption costs accuracy only in the pathological case
    where the SEED is broken too — and then the loader serves an empty
    table, which is the honest "no model is vouched for" answer.
    """
    global _CONTEXT_TABLE_LOADER
    try:
        if _CONTEXT_TABLE_LOADER is None:
            from model_router.config import export_path  # pyright: ignore[reportMissingImports]
            from model_router.context_table import (  # pyright: ignore[reportMissingImports]
                ContextTableLoader,
            )
            _CONTEXT_TABLE_LOADER = ContextTableLoader(export_path())
        return _CONTEXT_TABLE_LOADER.current()
    except Exception:  # noqa: BLE001 — see docstring; never break the write
        return None


def decorate_1m(value: Any, table: Optional[Any]) -> Any:
    """Append the ``[1m]`` context-window hint when the table vouches for it.

    The ONE write-adjacent operation R41 permits on model ids (see the
    module docstring): same model, right window. Exact-match against the
    table — ``glm-5.3`` in the table does not decorate ``glm-5.3-flash``,
    because a wrong ``window_1m`` inverts the bug into silent truncation.
    Idempotent, and never strips a suffix it cannot vouch for. ``table``
    may be any object with ``advertise_1m(model_id) -> bool`` (the real
    ``ContextTable`` does; tests pass a stub).
    """
    if table is None or not isinstance(value, str) or not value:
        return value
    base = value[: -len(CONTEXT_1M_SUFFIX)] if value.endswith(CONTEXT_1M_SUFFIX) else value
    prefix = ""
    if base.startswith(GATEWAY_ID_PREFIX):
        prefix = GATEWAY_ID_PREFIX
        base = base[len(GATEWAY_ID_PREFIX):]
    try:
        advertise = table.advertise_1m(base)
    except Exception:  # noqa: BLE001 — a broken table disables, never breaks
        return value
    if advertise:
        return prefix + base + CONTEXT_1M_SUFFIX
    return value


def point_at_gateway(
    path: Path,
    *,
    base_url: str,
    token: str,
    model: Optional[str] = None,
    remove_slot_overrides: bool = False,
    restore_env: Optional[Mapping[str, str]] = None,
) -> dict:
    """Point the panel at the gateway, MERGING into the existing env block.

    Args:
        path: the VS Code global ``settings.json`` to edit.
        base_url: the gateway's base URL (``http://127.0.0.1:<port>``).
        token: the gateway's loopback host token.
        model: written to ``ANTHROPIC_MODEL`` only when non-empty. ``None``
            leaves any existing value exactly as it was — re-running this
            action must never reset the user's model choice.
        remove_slot_overrides: drop tier/subagent overrides already present
            in the file. Default False: they are the user's keys. The GUI
            offers this as an explicit choice and names the keys it would
            remove.
        restore_env: the user's OWN ``ANTHROPIC_MODEL`` / slot values, as
            taken out of this file by the ``remote-control`` leg of the mode
            switch, to put back verbatim. Any other key is ignored — a stash
            cannot inject a routing key or an unknown name. An explicit
            ``model`` still wins over a restored ``ANTHROPIC_MODEL``. See the
            module docstring for why this is the corollary of the
            never-write rule, not an exception to it.

    Returns a result dict; ``status`` is ``written``, ``unchanged`` or
    ``refused``. On ``refused`` the file is byte-for-byte untouched.
    """
    path = Path(path)
    result: dict[str, Any] = {
        "action": "point_at_gateway",
        "path": str(path),
        "ok": False,
        "status": "refused",
        "reason": None,
        "message": "",
        "backup_path": None,
        "keys_written": [],
        "keys_preserved": [],
        "keys_removed": [],
        "keys_restored": [],
        "slot_overrides_preserved": [],
        "values_healed": [],
        "permissions": "unknown",
        "paste_block": None,
        "restart_required": True,
    }
    existed = path.is_file()
    try:
        settings, original_text = _load_settings(path)
        block = _existing_env_block(settings, path)

        preserved = sorted(
            k for k in block if k not in ROUTING_KEYS and k != MODEL_KEY
        )
        removed: list[str] = []
        if remove_slot_overrides:
            for key in SLOT_OVERRIDE_KEYS:
                if key in block:
                    del block[key]
                    removed.append(key)
            preserved = [k for k in preserved if k not in removed]

        # The mode switch's stash, put back. Only the model key and the slot
        # keys can come through here; the stash is user state and this is
        # the guard that keeps it from ever writing a routing key. A value
        # the user has since set by hand on the same key is overwritten:
        # the stash IS their choice for this mode, made when they left it.
        restored: list[str] = []
        for key, value in (restore_env or {}).items():
            if key != MODEL_KEY and key not in SLOT_OVERRIDE_KEYS:
                continue
            if not isinstance(value, str) or not value.strip():
                continue
            block[key] = value
            restored.append(key)
        restored.sort()
        preserved = [k for k in preserved if k not in restored]

        block["ANTHROPIC_BASE_URL"] = base_url
        block["ANTHROPIC_AUTH_TOKEN"] = token
        block["ANTHROPIC_API_KEY"] = ""
        block["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] = "1"
        written: list[str] = list(ROUTING_KEYS)
        if model:
            block[MODEL_KEY] = model
            written.append(MODEL_KEY)
            restored = [k for k in restored if k != MODEL_KEY]
        elif MODEL_KEY in block and MODEL_KEY not in restored:
            preserved.append(MODEL_KEY)
            preserved.sort()

        # R41 decoration — the one permitted touch on model-id values we do
        # not own (see module docstring). Applies to the explicit choice AND
        # to every carried-forward value, so a stale plain id from the
        # pre-gateway prototype cannot keep its wrong context window (the
        # decoration is idempotent, so a fresh value the loop re-visits is
        # unchanged by the second pass). Keys in ROUTING_KEYS are ours and
        # are skipped — ANTHROPIC_AUTH_TOKEN is a credential, not a model id.
        table = _context_table()
        healed: list[str] = []
        for key in block:
            if key in ROUTING_KEYS:
                continue
            decorated = decorate_1m(block[key], table)
            if decorated != block[key]:
                block[key] = decorated
                healed.append(key)
        healed.sort()

        new_settings = dict(settings)
        new_settings[ENV_BLOCK_KEY] = block
        new_settings[LOGIN_PROMPT_KEY] = True

        result["keys_written"] = written
        result["keys_preserved"] = preserved
        result["keys_removed"] = removed
        result["keys_restored"] = restored
        result["values_healed"] = healed
        result["slot_overrides_preserved"] = sorted(
            k for k in SLOT_OVERRIDE_KEYS if k in block
        )

        if existed and new_settings == settings:
            result.update(
                ok=True,
                status="unchanged",
                permissions=_probe_permissions(path),
                message=(
                    "Already pointed at this gateway; nothing to change. "
                    "Restart VS Code if the panel has not picked it up."
                ),
            )
            return result

        backup, perms = _write_settings(
            path, new_settings, original_text=original_text, existed=existed,
        )
        result.update(
            ok=True,
            status="written",
            backup_path=backup,
            permissions=perms,
            message=(
                "Panel pointed at the model gateway. "
                "Restart VS Code (fully quit and reopen) for it to take effect."
                + (
                    " Same models, right context window: [1m] hint added to "
                    + ", ".join(healed)
                    + "."
                    if healed
                    else ""
                )
            ),
        )
        return result
    except SettingsRefused as exc:
        result["reason"] = exc.reason
        result["message"] = exc.message
        result["paste_block"] = paste_block(
            base_url=base_url, token=token, model=model,
        )
        return result


def reset_native(path: Path) -> dict:
    """Remove BOTH managed keys, restoring the stock Claude Code panel.

    Removing only ``claudeCode.environmentVariables`` would leave
    ``claudeCode.disableLoginPrompt: true`` behind, which suppresses the
    login flow while env-auth is already gone — a panel with no way back.
    That is why this removes exactly two keys, and why the test that pins
    it asserts on the removed SET, not on one of them.

    Every other key in the file, including any tier/subagent override the
    prototype or the user put in the env block, goes with the block: the
    block is ours to remove wholesale here, unlike in
    :func:`point_at_gateway` where the user is keeping the panel on the
    gateway and their keys must survive.
    """
    path = Path(path)
    result: dict[str, Any] = {
        "action": "reset_native",
        "path": str(path),
        "ok": False,
        "status": "refused",
        "reason": None,
        "message": "",
        "backup_path": None,
        "keys_removed": [],
        "permissions": "unknown",
        "paste_block": None,
        "restart_required": True,
    }
    if not path.is_file():
        result.update(
            ok=True,
            status="unchanged",
            message=f"{path} does not exist; nothing to reset.",
        )
        return result
    try:
        settings, original_text = _load_settings(path)
        present = [k for k in MANAGED_SETTINGS_KEYS if k in settings]
        if not present:
            result.update(
                ok=True,
                status="unchanged",
                permissions=_probe_permissions(path),
                message=(
                    "This settings file has no Claude Code routing keys; "
                    "the panel is already on stock behaviour."
                ),
            )
            return result
        new_settings = {k: v for k, v in settings.items() if k not in MANAGED_SETTINGS_KEYS}
        backup, perms = _write_settings(
            path, new_settings, original_text=original_text, existed=True,
        )
        result.update(
            ok=True,
            status="written",
            backup_path=backup,
            keys_removed=present,
            permissions=perms,
            message=(
                "Panel reset to stock Claude Code. Restart VS Code (fully "
                "quit and reopen); you may be asked to log in again."
            ),
        )
        return result
    except SettingsRefused as exc:
        result["reason"] = exc.reason
        result["message"] = (
            f"{exc.message} To reset by hand, delete these two keys: "
            f"`{ENV_BLOCK_KEY}` and `{LOGIN_PROMPT_KEY}`."
        )
        return result


def reset_native_if_vco_gateway(
    path: Path,
    *,
    ports: Optional[Sequence[int]] = None,
) -> dict:
    """Reset ONLY a panel pointed at a VCO gateway; leave anything else alone.

    This is the uninstall-time entry point. Uninstalling the orchestrator
    while VS Code still points at a gateway that is about to stop existing
    strands the panel — every request fails with a connection error and the
    user has no obvious way back. But a user who pointed their panel at
    somebody else's gateway, or at a vendor endpoint directly, made a
    decision VCO does not get to reverse. Hence the guard, and hence the
    ``left_alone`` status carrying the URL it declined to touch.
    """
    path = Path(path)
    probe = inspect_target(path, ports=ports)
    if not probe["exists"]:
        return {
            "action": "reset_native_if_vco_gateway",
            "path": str(path),
            "ok": True,
            "status": "unchanged",
            "message": "no settings file",
            "base_url": None,
        }
    if probe["parseable"] is False:
        return {
            "action": "reset_native_if_vco_gateway",
            "path": str(path),
            "ok": False,
            "status": "refused",
            "reason": probe["refusal_reason"],
            "message": (
                f"{probe['message']} To reset by hand, delete these two keys: "
                f"`{ENV_BLOCK_KEY}` and `{LOGIN_PROMPT_KEY}`."
            ),
            "base_url": None,
        }
    if not probe["points_at_vco_gateway"]:
        return {
            "action": "reset_native_if_vco_gateway",
            "path": str(path),
            "ok": True,
            "status": "left_alone",
            "message": (
                "ANTHROPIC_BASE_URL is not a VCO gateway on this machine; "
                "left exactly as it is."
            ),
            "base_url": probe["base_url"],
        }
    out = reset_native(path)
    out["action"] = "reset_native_if_vco_gateway"
    out["base_url"] = probe["base_url"]
    return out


# ---------------------------------------------------------------------------
# The mode switch — see "The two-state mode switch" in the module docstring.
# ---------------------------------------------------------------------------


def stash_path() -> Path:
    """Where the ``remote-control`` leg keeps the choices it takes out."""
    from vco_lib.paths import vct_root_dir

    return vct_root_dir() / STASH_SUBDIR / STASH_BASENAME


def is_gateway_only_model(value: Any, table: Optional[Any]) -> bool:
    """Pure: does ``value`` name a model only the gateway can resolve?

    True for a ``claude-gw/<id>`` name, and for a bare id the context table
    attributes to a vendor other than :data:`FIRST_PARTY_VENDOR`. Everything
    else — a real Claude id, an id the table does not know, a non-string —
    is False, because the stock client MAY resolve it and dropping it would
    be a guess. The ``[1m]`` suffix is ignored for the lookup, exactly as
    :func:`decorate_1m` ignores it. ``table`` may be any object with
    ``lookup(model_id) -> row-with-.vendor-or-None`` (the real
    ``ContextTable`` does; tests pass a stub) or ``None``.
    """
    if not isinstance(value, str):
        return False
    base = value.strip()
    if not base:
        return False
    if base.endswith(CONTEXT_1M_SUFFIX):
        base = base[: -len(CONTEXT_1M_SUFFIX)]
    if base.startswith(GATEWAY_ID_PREFIX):
        return True
    lookup = getattr(table, "lookup", None)
    if lookup is None:
        return False
    try:
        row = lookup(base)
    except Exception:  # noqa: BLE001 — a broken table classifies nothing
        return False
    vendor = getattr(row, "vendor", "") if row is not None else ""
    return bool(vendor) and vendor != FIRST_PARTY_VENDOR


def _read_stash(path: Path) -> tuple[Optional[dict], Optional[str]]:
    """``(document, problem)``. A missing stash is ``(None, None)``.

    A stash that exists but cannot be used is reported, not raised: the
    ``multimodel`` leg still points the panel (that is what the click asked
    for) and says the restore was skipped and why.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"stash {path} could not be read: {exc}"
    try:
        doc = json.loads(text)
    except ValueError as exc:
        return None, f"stash {path} is not valid JSON: {exc}"
    if not isinstance(doc, dict):
        return None, f"stash {path} is not a JSON object"
    if doc.get("schema_version") != _STASH_SCHEMA_VERSION:
        return None, (
            f"stash {path} has schema_version {doc.get('schema_version')!r}; "
            f"this VCO reads {_STASH_SCHEMA_VERSION}"
        )
    values = doc.get("values")
    if not isinstance(values, dict):
        return None, f"stash {path} has no 'values' object"
    return doc, None


def _write_stash(path: Path, doc: Mapping[str, Any]) -> None:
    """Write the stash owner-only. Loud when it cannot be locked down.

    The stash never holds the token, so a permission failure here is not a
    credential exposure — but it IS a file that names the user's model
    choices, and the module's contract is "restricted to the owner" for
    everything it writes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(doc, indent=2) + "\n", mode=0o600)
    try:
        restrict_to_owner(path)
    except PermissionHardeningError as exc:
        try:
            path.unlink()
        except OSError:
            pass
        raise SettingsRefused(
            "stash_not_lockable",
            f"the mode stash {path} could not be restricted to your user "
            f"account ({exc}). Nothing was changed.",
        ) from exc


def _clear_stash(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _same_file(a: Path, b: Path) -> bool:
    """Path equality that survives symlinks and ``~`` spellings; never raises."""
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a) == str(b)


def panel_mode(
    path: Path,
    *,
    ports: Optional[Sequence[int]] = None,
    stash: Optional[Path] = None,
) -> dict:
    """Which state of the switch a settings file is in. Reads only.

    * ``multimodel`` — ``ANTHROPIC_BASE_URL`` is a VCO gateway on this
      machine.
    * ``remote-control`` — no ``ANTHROPIC_BASE_URL`` at all (stock client;
      a missing file counts, it IS stock).
    * ``unmanaged`` — a base URL that is not ours. The switch never touches
      this state; the user set it, and only the user unsets it.
    * ``unparseable`` — JSONC or otherwise unreadable; nothing is guessed.
    """
    path = Path(path)
    stash = Path(stash) if stash is not None else stash_path()
    probe = inspect_target(path, ports=ports)
    out: dict[str, Any] = {
        "mode": MODE_REMOTE_CONTROL,
        "path": str(path),
        "detail": "",
        "base_url": probe["base_url"],
        "model": probe["model"],
        "slot_overrides": list(probe["slot_overrides"]),
        "stash_present": stash.is_file(),
        "stash_path": str(stash),
    }
    if not probe["exists"]:
        out["detail"] = "No settings file; the panel is on stock Claude Code."
        return out
    if probe["parseable"] is False:
        out["mode"] = MODE_UNPARSEABLE
        out["detail"] = probe["message"] or "settings.json could not be parsed."
        return out
    if probe["points_at_vco_gateway"]:
        out["mode"] = MODE_MULTIMODEL
        out["detail"] = (
            f"Panel points at the model gateway ({probe['base_url']}); "
            "GLM and Claude share one picker. Remote Control is unavailable."
        )
        return out
    if probe["base_url"] is None:
        out["detail"] = (
            "Stock Claude Code; Remote Control works. Gateway models are "
            "not in the panel."
        )
        if out["stash_present"]:
            out["detail"] += " Model choices from Multimodel mode are stashed."
        return out
    out["mode"] = MODE_UNMANAGED
    out["detail"] = (
        f"Panel points at a custom endpoint ({probe['base_url']}); VCO "
        "leaves it alone."
    )
    return out


def _mode_result(action_mode: str, path: Path, stash: Path) -> dict[str, Any]:
    return {
        "action": "set_mode",
        "mode": action_mode,
        "path": str(path),
        "ok": False,
        "status": "refused",
        "reason": None,
        "message": "",
        "backup_path": None,
        "keys_written": [],
        "keys_preserved": [],
        "keys_removed": [],
        "keys_restored": [],
        "values_stashed": [],
        "slot_overrides_preserved": [],
        "values_healed": [],
        "stash_path": str(stash),
        "stash_present": stash.is_file(),
        "permissions": "unknown",
        "paste_block": None,
        "restart_required": True,
    }


def set_mode_remote_control(path: Path, *, stash: Optional[Path] = None) -> dict:
    """The ``remote-control`` leg. See the module docstring for the rules.

    Order of operations is the safety argument: the stash is written FIRST,
    then the settings file. If the settings write is refused (and rolled
    back by :func:`_write_settings`), the previous stash bytes are put back
    — or the new file removed — so the stash and the settings file never
    disagree about what has been taken out.
    """
    path = Path(path)
    stash = Path(stash) if stash is not None else stash_path()
    result = _mode_result(MODE_REMOTE_CONTROL, path, stash)
    if not path.is_file():
        result.update(
            ok=True,
            status="unchanged",
            message=f"{path} does not exist; the panel is already on stock Claude Code.",
        )
        return result
    try:
        settings, original_text = _load_settings(path)
        block = _existing_env_block(settings, path)
        table = _context_table()

        removed_routing = [k for k in ROUTING_KEYS if k in block]
        routing_values = {
            k: block[k]
            for k in removed_routing
            if k not in _STASH_SECRET_KEYS and isinstance(block[k], str)
        }
        stashed_values: dict[str, str] = {}
        for key in (MODEL_KEY, *SLOT_OVERRIDE_KEYS):
            if key in block and is_gateway_only_model(block[key], table):
                stashed_values[key] = block[key]

        new_block = {
            k: v
            for k, v in block.items()
            if k not in removed_routing and k not in stashed_values
        }
        # R41 on the values that stay: a plain 1M Claude id in a slot reads
        # as 200K to the stock client too, and the fix is the client's own
        # suffix. Same model, right window — nothing else is touched.
        healed: list[str] = []
        for key in (MODEL_KEY, *SLOT_OVERRIDE_KEYS):
            if key in new_block:
                decorated = decorate_1m(new_block[key], table)
                if decorated != new_block[key]:
                    new_block[key] = decorated
                    healed.append(key)

        login_removed = LOGIN_PROMPT_KEY in settings
        new_settings = {k: v for k, v in settings.items() if k != LOGIN_PROMPT_KEY}
        if ENV_BLOCK_KEY in settings:
            if new_block or not block:
                # Either keys remain, or the block was empty before we got
                # here — keep the shape the user had.
                new_settings[ENV_BLOCK_KEY] = new_block
            else:
                # We emptied it; an empty husk is not a setting.
                del new_settings[ENV_BLOCK_KEY]

        keys_removed = removed_routing + ([LOGIN_PROMPT_KEY] if login_removed else [])
        result["keys_removed"] = keys_removed
        result["values_stashed"] = sorted(stashed_values)
        result["values_healed"] = healed
        result["slot_overrides_preserved"] = sorted(
            k for k in SLOT_OVERRIDE_KEYS if k in new_block
        )
        result["keys_preserved"] = sorted(new_block)

        if new_settings == settings:
            result.update(
                ok=True,
                status="unchanged",
                permissions=_probe_permissions(path),
                message=(
                    "Already on stock Claude Code; nothing to change. "
                    "Restart VS Code if the panel has not picked it up."
                ),
            )
            return result

        # Stash first (see docstring). Only when something is actually
        # being taken out: a run whose only change is the [1m] decoration
        # must not overwrite a stash an earlier run made.
        previous_stash: Optional[bytes] = None
        wrote_stash = False
        if removed_routing or stashed_values or login_removed:
            try:
                previous_stash = stash.read_bytes()
            except OSError:
                previous_stash = None
            _write_stash(
                stash,
                {
                    "schema_version": _STASH_SCHEMA_VERSION,
                    "stashed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "settings_path": str(path),
                    "routing_keys": removed_routing,
                    "routing_values": routing_values,
                    "login_prompt_removed": login_removed,
                    "values": stashed_values,
                },
            )
            wrote_stash = True
        try:
            backup, perms = _write_settings(
                path, new_settings, original_text=original_text, existed=True,
            )
        except SettingsRefused:
            if wrote_stash:
                if previous_stash is None:
                    _clear_stash(stash)
                else:
                    atomic_write_text(
                        stash, previous_stash.decode("utf-8", "replace"), mode=0o600,
                    )
            raise
        result["stash_present"] = stash.is_file()
        stashed_note = (
            " Gateway-only model choices stashed for Multimodel mode: "
            + ", ".join(sorted(stashed_values))
            + "."
            if stashed_values
            else ""
        )
        healed_note = (
            " Same models, right context window: [1m] hint added to "
            + ", ".join(healed)
            + "."
            if healed
            else ""
        )
        result.update(
            ok=True,
            status="written",
            backup_path=backup,
            permissions=perms,
            message=(
                "Panel set to stock Claude Code; Remote Control can start. "
                "Restart VS Code (fully quit and reopen); you may be asked to "
                "log in again." + stashed_note + healed_note
            ),
        )
        return result
    except SettingsRefused as exc:
        result["reason"] = exc.reason
        result["message"] = (
            f"{exc.message} To switch by hand, delete these two keys: "
            f"`{ENV_BLOCK_KEY}` and `{LOGIN_PROMPT_KEY}`."
        )
        return result


def set_mode_multimodel(
    path: Path,
    *,
    base_url: str,
    token: str,
    stash: Optional[Path] = None,
) -> dict:
    """The ``multimodel`` leg: point, with the stash put back, then clear it.

    The stash is honoured only when it was made for THIS settings file. Two
    editors (VS Code and Cursor, say) share one stash path, and restoring
    one editor's choices into the other's file would be a substitution the
    user never made; in that case this is a plain point and the stash is
    left for the file it belongs to.
    """
    path = Path(path)
    stash = Path(stash) if stash is not None else stash_path()
    doc, problem = _read_stash(stash)
    restore: dict[str, str] = {}
    skipped_reason: Optional[str] = problem
    if doc is not None:
        origin = Path(str(doc.get("settings_path") or ""))
        if origin.name and _same_file(origin, path):
            restore = {
                k: v for k, v in doc["values"].items() if isinstance(v, str)
            }
        else:
            skipped_reason = (
                f"stash was made for {origin} and this is {path}; left alone"
            )

    result = point_at_gateway(
        path, base_url=base_url, token=token, restore_env=restore,
    )
    result["action"] = "set_mode"
    result["mode"] = MODE_MULTIMODEL
    result["stash_path"] = str(stash)
    result["values_stashed"] = []
    result["stash_skipped_reason"] = skipped_reason
    if result["ok"] and doc is not None and not skipped_reason:
        _clear_stash(stash)
        if result["keys_restored"]:
            result["message"] += (
                " Restored from your last Multimodel session: "
                + ", ".join(result["keys_restored"])
                + "."
            )
    result["stash_present"] = stash.is_file()
    return result


def set_mode(
    path: Path,
    mode: str,
    *,
    base_url: Optional[str] = None,
    token: Optional[str] = None,
    stash: Optional[Path] = None,
) -> dict:
    """Dispatch on ``mode``; the CLI's and the launcher's single entry point.

    ``base_url`` and ``token`` are required for ``multimodel`` and ignored
    for ``remote-control``. An unknown mode is a caller bug and raises.
    """
    if mode == MODE_REMOTE_CONTROL:
        return set_mode_remote_control(path, stash=stash)
    if mode == MODE_MULTIMODEL:
        if not base_url or not token:
            raise ValueError("multimodel needs base_url and token")
        return set_mode_multimodel(
            path, base_url=base_url, token=token, stash=stash,
        )
    raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")


# ---------------------------------------------------------------------------
# CLI — the Rust launcher's entry point. stdout is a machine contract.
# ---------------------------------------------------------------------------


def _emit(payload: Any) -> None:
    """JSON to stdout, nothing else. Human notes go to stderr."""
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.vscode_settings",
        description=(
            "Read and edit VS Code's global settings.json for the Claude Code "
            "panel's gateway routing. Writes exactly two keys; refuses rather "
            "than rewriting a file it cannot parse."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("detect", help="list existing VS Code-family settings files")

    p_inspect = sub.add_parser("inspect", help="describe one settings file")
    p_inspect.add_argument("--path", required=True)

    p_point = sub.add_parser("point", help="point the panel at the gateway")
    p_point.add_argument("--path", required=True)
    p_point.add_argument(
        "--base-url",
        default=None,
        help="default: http://127.0.0.1:<resolved gateway port>",
    )
    p_point.add_argument(
        "--model",
        default=None,
        help=(
            "value for ANTHROPIC_MODEL. Omit to leave the user's current "
            "choice untouched (the default)."
        ),
    )
    p_point.add_argument(
        "--remove-slot-overrides",
        action="store_true",
        help=(
            "also delete tier/subagent overrides already in the file. VCO "
            "never writes them; this only removes ones already there."
        ),
    )

    p_reset = sub.add_parser("reset", help="remove both managed keys")
    p_reset.add_argument("--path", required=True)

    p_reset_if = sub.add_parser(
        "reset-if-gateway",
        help="reset only when the panel points at a VCO gateway",
    )
    p_reset_if.add_argument("--path", required=True)

    p_mode = sub.add_parser(
        "mode",
        help="the Multimodel <-> Remote Control switch: --get or --set",
    )
    p_mode.add_argument("--path", required=True)
    which = p_mode.add_mutually_exclusive_group(required=True)
    which.add_argument("--get", action="store_true", help="report the current mode")
    which.add_argument(
        "--set",
        choices=MODES,
        default=None,
        help="apply a mode (idempotent; VS Code must be restarted afterwards)",
    )
    p_mode.add_argument(
        "--base-url",
        default=None,
        help="multimodel only; default: http://127.0.0.1:<resolved gateway port>",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "detect":
        _emit({"targets": [t.as_dict() for t in detect_targets()]})
        return 0
    if args.command == "inspect":
        _emit(inspect_target(Path(args.path), ports=resolve_gateway_ports()))
        return 0
    if args.command == "reset":
        result = reset_native(Path(args.path))
        _emit(result)
        return 0 if result["ok"] else 1
    if args.command == "reset-if-gateway":
        result = reset_native_if_vco_gateway(
            Path(args.path), ports=resolve_gateway_ports(),
        )
        _emit(result)
        return 0 if result["ok"] else 1
    if args.command == "mode":
        if args.get:
            _emit(panel_mode(Path(args.path), ports=resolve_gateway_ports()))
            return 0
        if args.set == MODE_REMOTE_CONTROL:
            result = set_mode(Path(args.path), MODE_REMOTE_CONTROL)
            _emit(result)
            return 0 if result["ok"] else 1
        ports = resolve_gateway_ports()
        base_url = args.base_url or f"http://127.0.0.1:{ports[0]}"
        try:
            token = resolve_host_token()
        except SettingsRefused as exc:
            _emit(
                {
                    "action": "set_mode",
                    "mode": MODE_MULTIMODEL,
                    "path": args.path,
                    "ok": False,
                    "status": "refused",
                    "reason": exc.reason,
                    "message": exc.message,
                }
            )
            return 1
        result = set_mode(
            Path(args.path), MODE_MULTIMODEL, base_url=base_url, token=token,
        )
        _emit(result)
        return 0 if result["ok"] else 1
    # point
    ports = resolve_gateway_ports()
    base_url = args.base_url or f"http://127.0.0.1:{ports[0]}"
    try:
        token = resolve_host_token()
    except SettingsRefused as exc:
        _emit(
            {
                "action": "point_at_gateway",
                "path": args.path,
                "ok": False,
                "status": "refused",
                "reason": exc.reason,
                "message": exc.message,
            }
        )
        return 1
    result = point_at_gateway(
        Path(args.path),
        base_url=base_url,
        token=token,
        model=args.model or None,
        remove_slot_overrides=bool(args.remove_slot_overrides),
    )
    _emit(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())


__all__ = [
    "CONTEXT_1M_SUFFIX",
    "DEFAULT_GATEWAY_MODEL",
    "DEFAULT_GATEWAY_PORT",
    "ENV_BLOCK_KEY",
    "ENV_TARGET_OVERRIDE",
    "ENV_TOKEN",
    "FIRST_PARTY_VENDOR",
    "GATEWAY_ID_PREFIX",
    "LOGIN_PROMPT_KEY",
    "MANAGED_SETTINGS_KEYS",
    "MODEL_KEY",
    "MODES",
    "MODE_MULTIMODEL",
    "MODE_REMOTE_CONTROL",
    "MODE_UNMANAGED",
    "MODE_UNPARSEABLE",
    "ROUTING_KEYS",
    "SLOT_OVERRIDE_KEYS",
    "STASH_BASENAME",
    "STASH_SUBDIR",
    "SettingsRefused",
    "Target",
    "VARIANTS",
    "candidate_paths",
    "decorate_1m",
    "describe_json_failure",
    "detect_targets",
    "inspect_target",
    "is_gateway_only_model",
    "is_loopback_host",
    "is_vco_gateway_base_url",
    "main",
    "panel_mode",
    "paste_block",
    "point_at_gateway",
    "reset_native",
    "reset_native_if_vco_gateway",
    "resolve_gateway_ports",
    "resolve_host_token",
    "set_mode",
    "set_mode_multimodel",
    "set_mode_remote_control",
    "sniff_indent",
    "sniff_newline",
    "stash_path",
]
