# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ONE template materializer: one renderer, one registry (v0.2.100 WP-18).

WHY THIS MODULE EXISTS
----------------------
Until v0.2.100 six Python renderers each hand-rolled a ``str.replace`` loop
over their own placeholder map (the survey's R1-R6: agent/skill substitution,
project templates, the ``VCO-REWIRE`` region rewriter, the moved-clone heal's
round-trip, the orchestrator-root ``CLAUDE.md`` renderer and the boot-unit
renderer). Five of them passed an unknown ``{{NAME}}`` through silently, the
maps drifted (``{{PROJECT_ROOT}}`` existed in one and not its round-trip
partner), and only one of them escaped a value for the context it landed in.

This module is the replacement, in two parts:

* :func:`render` — token-exact over ``{{UPPER_SNAKE}}`` only. Lowercase
  content examples (``{{first_name}}``), GitHub-Actions ``${{ … }}`` and the
  conditional-section tags (``{{#if_module_active}}``) are never touched.
  Values are escaped for their destination (:data:`ESCAPES`).
* :data:`REGISTRY` — name → resolver. Every value a template may ask for is
  declared here ONCE, with the one module that knows it: roots and project
  identity (:mod:`vco_lib.project_identity`), the venv interpreter
  (:func:`vco_lib.install_companions.resolve_install_venv_python`), the
  service endpoints (:mod:`vco_lib.service_endpoints`) and the hub port
  (:func:`vco_lib.hub_ensure.resolve_hub_port`). Boot-unit keys are declared
  too, but their VALUES come from the unit spec (:mod:`vco_lib.boot_service`),
  which alone knows them.

The renderers that remain elsewhere are thin callers: they choose the
``allowed`` set and the escape for their file class, nothing else.

THE OWNER RULES THIS MODULE ENFORCES (2026-09-30)
-------------------------------------------------
1. An unknown or unresolvable placeholder NEVER fails a materialization. The
   token is left in place, a warning goes to stderr, and a registered deferral
   row (``template_placeholder_unrendered_<file>``) names the file, the
   placeholder and the line. A later CLEAN render of the same file clears the
   row (paired resolution, :func:`settle_deferrals`).
2. Every path-valued placeholder that must exist on the machine is checked
   after rendering. A missing path is a warning plus a sibling row
   (``template_path_missing_<file>``) naming file, placeholder and value.

The render-every-template gate (``tests/test_template_materialization_complete.py``)
stays STRICT: no shipped template may carry a name this registry cannot fill.
Rule 1 is the field behaviour for a mismatch that slipped past that gate (an
old renderer reading a newer template mid-update), never a licence to ship one.
"""

from __future__ import annotations

import hashlib
import html
import platform
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple,
)

__all__ = [
    "ESCAPES",
    "PLACEHOLDER_RE",
    "Key",
    "REGISTRY",
    "PATH_KEYS",
    "GLOBAL_KEYS",
    "BOOT_UNIT_KEYS",
    "MaterializeContext",
    "LazyContext",
    "Unresolved",
    "MissingPath",
    "RenderResult",
    "RenderSpec",
    "Transform",
    "FindingsSink",
    "render",
    "render_document",
    "escape_value",
    "escape_for_filename",
    "path_subs",
    "project_display_name",
    "venv_python_path",
    "unrendered_condition_id",
    "path_missing_condition_id",
    "UNRENDERED_PREFIX",
    "PATH_MISSING_PREFIX",
    "warn",
    "settle_deferrals",
    "bundle_rerender_command",
]

# ---------------------------------------------------------------------------
# Token grammar
# ---------------------------------------------------------------------------

#: ``{{UPPER_SNAKE}}`` — and nothing else. The negative look-behind keeps a
#: GitHub-Actions ``${{X}}`` expression out; ``{{#if…}}`` / ``{{/if…}}`` and any
#: lowercase ``{{name}}`` fail the character class.
PLACEHOLDER_RE = re.compile(r"(?<!\$)\{\{([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*)\}\}")

# The vct-hub unit templates' own ``__SCREAMING_SNAKE__`` tokens are NOT this
# module's: they are rendered in Rust (``vct-hub/src/boot.rs``, R7) from the ONE
# key table ``tests/fixtures/hub_unit_placeholders.json``, which the
# completeness gate also reads to prove none survives into a Python render.

#: The escape modes a caller may request (the destination's literal context).
ESCAPES = ("none", "yaml", "xml", "py", "ps1", "sh")

# ---------------------------------------------------------------------------
# Escaping — ONE home (absorbs rewire._escape_for + the boot-unit XML escape)
# ---------------------------------------------------------------------------


def _xml_content(value: str) -> str:
    """``&``, ``<`` and ``>`` for XML ELEMENT CONTENT.

    Quotes are deliberately left alone: every substituted value in the shipped
    unit templates lands in element content, never in an attribute, and the
    Task XML builds an ``&quot;``-quoted inner command line by hand around the
    values. (``vct-hub/src/boot.rs::xml_escape`` escapes all five — a different
    template with different quoting; not a MUST-MATCH pair.)
    """
    return html.escape(value, quote=False)


def _yaml_double_quoted(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def escape_value(value: str, escape: str) -> str:
    """Escape ``value`` for the literal context named by ``escape``.

    * ``none`` — verbatim.
    * ``xml``  — XML element content (see :func:`_xml_content`).
    * ``py``   — inside a Python double-quoted literal: ``\\`` then ``"``. A
      Windows root baked verbatim would otherwise start a ``\\U`` escape.
    * ``ps1``  — inside a PowerShell single-quoted literal: ``'`` doubles and
      ``\\`` stays literal.
    * ``sh``   — inside a POSIX double-quoted word: ``\\``, ``"``, ``$`` and a
      backtick, so a path can never introduce an expansion.
    * ``yaml`` — handled per LINE by :func:`render` (a scalar is quoted as a
      whole, not per token); a bare call escapes for a double-quoted scalar.
    """
    if escape == "none":
        return value
    if escape == "xml":
        return _xml_content(value)
    if escape == "py":
        return value.replace("\\", "\\\\").replace('"', '\\"')
    if escape == "ps1":
        return value.replace("'", "''")
    if escape == "sh":
        out = value.replace("\\", "\\\\")
        for ch in ('"', "$", "`"):
            out = out.replace(ch, "\\" + ch)
        return out
    if escape == "yaml":
        return _yaml_double_quoted(value)
    raise ValueError(f"unknown escape {escape!r}; expected one of {ESCAPES}")


def escape_for_filename(filename: str) -> str:
    """The literal context of a ``VCO-REWIRE`` region, by the script's suffix."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".py":
        return "py"
    if suffix == ".ps1":
        return "ps1"
    return "sh"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaterializeContext:
    """What a render is FOR: this install, this project, this OS.

    ``project_name`` is only the FALLBACK display name for a folder that is
    not registered in launcher.db (``install-bundle --project-name`` or the
    launcher's own argument); a registered project's name always wins, so
    both CLAUDE.md render paths agree (survey gap d.2).
    """

    orchestrator_root: Path
    project_root: Optional[Path] = None
    project_name: Optional[str] = None
    os_name: Optional[str] = None
    home: Optional[Path] = None
    db_path: Optional[Path] = None

    @property
    def effective_os(self) -> str:
        return self.os_name if self.os_name is not None else platform.system()

    @property
    def effective_project_root(self) -> Path:
        return self.project_root if self.project_root else self.orchestrator_root


Resolver = Callable[[MaterializeContext], Optional[str]]


@dataclass(frozen=True)
class Key:
    """One placeholder the materializer knows.

    ``resolver`` is ``None`` for a CALLER-SUPPLIED key (the boot-unit values,
    which only the unit spec knows). ``must_exist`` marks a path the rendered
    file will use — owner rule 2 checks it after every render. ``verbatim``
    values are never escaped (the runtime-expansion literal
    ``${VCT_ORCHESTRATOR_ROOT}``; the pre-rendered plist argv fragment).
    """

    name: str
    doc: str
    resolver: Optional[Resolver] = None
    must_exist: bool = False
    verbatim: bool = False


def _home(ctx: MaterializeContext) -> str:
    return str(ctx.home if ctx.home is not None else Path.home())


def venv_python_path(orchestrator_root: Path, *, os_name: Optional[str] = None) -> Path:
    """The install's venv interpreter — OS-aware, never ``claude_mcp_servers/.venv``
    by construction unless that legacy venv is the one that exists.

    The resolver behind install.py's relaunch
    (:func:`vco_lib.install_companions.resolve_install_venv_python`). When no
    venv exists yet the CANONICAL location is returned (``<root>/.venv/bin/python``
    or ``<root>\\.venv\\Scripts\\python.exe``), and the path check (owner rule 2)
    reports it — a rendered path that becomes right the moment the venv is
    built beats a token nothing can run.
    """
    from vco_lib.install_companions import resolve_install_venv_python

    name = os_name if os_name is not None else platform.system()
    found = resolve_install_venv_python(orchestrator_root, os_name=name)
    if found is not None:
        return found
    if str(name).lower().startswith("win"):
        return Path(orchestrator_root) / ".venv" / "Scripts" / "python.exe"
    return Path(orchestrator_root) / ".venv" / "bin" / "python"


def project_display_name(
    folder: Path,
    fallback: Optional[str] = None,
    *,
    db_path: Optional[Path] = None,
) -> str:
    """THE ``{{PROJECT_NAME}}`` value — one source for every renderer.

    The registered ``projects.name`` when launcher.db knows the folder
    (:func:`vco_lib.project_identity.resolve_identity`), else ``fallback``,
    else the folder basename. Template text only: this never names a
    collection, so the basename rung is safe here (the identity module's
    warning is about collection names).
    """
    try:
        from vco_lib.project_identity import resolve_identity

        identity, _snap = resolve_identity(folder, fallback_name=fallback, db_path=db_path)
        if identity is not None and identity.name:
            return identity.name
    except Exception:  # noqa: BLE001 — identity is best-effort for template text
        pass
    return (fallback or "").strip() or Path(folder).name or "Project"


def _service_url(key: str) -> Resolver:
    def _resolve(ctx: MaterializeContext) -> Optional[str]:
        from vco_lib import service_endpoints

        return str(service_endpoints.machine_service_urls(ctx.db_path)[key])

    return _resolve


def _hub_port(_ctx: MaterializeContext) -> Optional[str]:
    from vco_lib.hub_ensure import resolve_hub_port

    return str(resolve_hub_port())


_BOOT_DOC = "boot unit (value from the unit spec, vco_lib/boot_service.py)"

_KEYS: Tuple[Key, ...] = (
    # ── roots ────────────────────────────────────────────────────────────
    Key("ORCHESTRATOR_ROOT", "The orchestrator clone this install runs from",
        lambda c: str(c.orchestrator_root), must_exist=True),
    Key("PROJECT_ROOT", "The project folder being installed into (the "
        "orchestrator root on a self-install)",
        lambda c: str(c.effective_project_root), must_exist=True),
    Key("PROJECTS_ROOT", "Parent of the orchestrator dir",
        lambda c: str(c.orchestrator_root.parent), must_exist=True),
    Key("HOME", "Your home directory", _home, must_exist=True),
    Key("VCT_ORCHESTRATOR_ROOT", "The literal `${VCT_ORCHESTRATOR_ROOT}`, "
        "expanded by the consumer at run time",
        lambda _c: "${VCT_ORCHESTRATOR_ROOT}", verbatim=True),
    # ── identity ─────────────────────────────────────────────────────────
    Key("PROJECT_NAME", "The project's registered name (launcher.db), else "
        "the name given at install, else the folder basename",
        lambda c: project_display_name(c.effective_project_root, c.project_name,
                                       db_path=c.db_path)),
    # ── interpreter ──────────────────────────────────────────────────────
    Key("VENV_PYTHON", "The install venv's interpreter (`.venv/bin/python`, "
        "`.venv\\Scripts\\python.exe` on Windows)",
        lambda c: str(venv_python_path(c.orchestrator_root, os_name=c.effective_os)),
        must_exist=True),
    # ── service endpoints (this machine's `service_endpoints` rows) ──────
    Key("WEAVIATE_URL", "Weaviate HTTP URL from this machine's endpoint row",
        _service_url("weaviate_url")),
    Key("WEAVIATE_GRPC_PORT", "Weaviate gRPC port from this machine's endpoint row",
        _service_url("weaviate_grpc_port")),
    Key("OLLAMA_URL", "Ollama URL from this machine's endpoint row",
        _service_url("ollama_url")),
    Key("CODE_EMBED_URL", "Code-embedding service URL from this machine's endpoint row",
        _service_url("code_embed_url")),
    Key("HUB_PORT", "The vct-hub port a client uses (`$VCT_HUB_PORT` → "
        "`hub.port` → 7700)", _hub_port),
    # ── boot units: declared here, valued by the spec ────────────────────
    Key("INSTALLED_AT_PATH", _BOOT_DOC),
    Key("WORKING_DIR", _BOOT_DOC, must_exist=True),
    Key("WRAPPER_SCRIPT", _BOOT_DOC, must_exist=True),
    Key("LOG_FILE", _BOOT_DOC),
    Key("BOOT_LOG_FILE", _BOOT_DOC),
    Key("LABEL", _BOOT_DOC),
    Key("CREATED_AT", _BOOT_DOC),
    Key("USER_ID", _BOOT_DOC),
    Key("EXEC_START", _BOOT_DOC),
    Key("EXEC_ARGV_PLIST", _BOOT_DOC + "; a pre-escaped `<string>` fragment",
        verbatim=True),
    Key("EXEC_COMMAND", _BOOT_DOC),
    Key("EXEC_ARGUMENTS", _BOOT_DOC),
    Key("STATE_DIR", _BOOT_DOC),
    Key("SECRET_PROJECT", _BOOT_DOC),
)

#: name → :class:`Key`. The ONE vocabulary.
REGISTRY: Dict[str, Key] = {k.name: k for k in _KEYS}

#: The path vocabulary every file-shaped renderer shares (agents, skills,
#: project templates, ``VCO-REWIRE`` regions, the moved-clone heal). ONE set —
#: the R1/R3/R4 ``{{PROJECT_ROOT}}`` drift was two sets.
PATH_KEYS = frozenset(
    {"ORCHESTRATOR_ROOT", "PROJECT_ROOT", "PROJECTS_ROOT", "HOME", "VCT_ORCHESTRATOR_ROOT"}
)
#: Every key the registry can resolve by itself.
GLOBAL_KEYS = frozenset(k.name for k in _KEYS if k.resolver is not None)
#: Keys only a boot-unit spec can value.
BOOT_UNIT_KEYS = frozenset(k.name for k in _KEYS if k.resolver is None)


def path_subs(orchestrator_root: Path, project_root: Optional[Path] = None,
              *, home: Optional[Path] = None) -> Dict[str, str]:
    """``{"{{NAME}}": value}`` for :data:`PATH_KEYS` — the shape the legacy
    call sites (``project_init._agent_subs``, ``rewire.rewire_subs``, the
    moved-clone heal) exposed. Raw values, no escaping."""
    ctx = MaterializeContext(Path(orchestrator_root), project_root, home=home)
    return {"{{" + name + "}}": str(REGISTRY[name].resolver(ctx))  # type: ignore[misc]
            for name in sorted(PATH_KEYS)}


class LazyContext(Mapping[str, Optional[str]]):
    """Registry values for one :class:`MaterializeContext`, resolved on first
    use and cached — a file that names no service URL never opens launcher.db.

    ``extra`` values (a boot spec's) take precedence over the registry.
    A resolver that raises yields ``None`` (unresolvable) — owner rule 1, the
    render carries on.
    """

    def __init__(self, context: MaterializeContext,
                 extra: Optional[Mapping[str, object]] = None) -> None:
        self.context = context
        self._extra = {k: (None if v is None else str(v)) for k, v in (extra or {}).items()}
        self._cache: Dict[str, Optional[str]] = {}

    def __getitem__(self, name: str) -> Optional[str]:
        if name in self._extra:
            return self._extra[name]
        if name in self._cache:
            return self._cache[name]
        key = REGISTRY.get(name)
        if key is None or key.resolver is None:
            raise KeyError(name)
        try:
            value = key.resolver(self.context)
        except Exception:  # noqa: BLE001 — unresolvable, never fatal
            value = None
        self._cache[name] = value
        return value

    def __iter__(self):
        return iter(sorted(set(self._extra) | GLOBAL_KEYS))

    def __len__(self) -> int:
        return len(set(self._extra) | GLOBAL_KEYS)


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Unresolved:
    """A token left in place. ``reason``: ``unknown`` (not allowed for this
    file class / not in the registry) or ``unresolvable`` (allowed, but its
    resolver produced nothing)."""

    name: str
    line: int
    reason: str


@dataclass(frozen=True)
class MissingPath:
    """A rendered path-valued placeholder whose value does not exist."""

    name: str
    value: str
    line: int


@dataclass(frozen=True)
class RenderResult:
    text: str
    unresolved: Tuple[Unresolved, ...] = ()
    missing_paths: Tuple[MissingPath, ...] = ()
    used: frozenset = field(default_factory=frozenset)

    @property
    def clean(self) -> bool:
        return not self.unresolved and not self.missing_paths

    def problems(self) -> Tuple[Tuple[str, ...], ...]:
        """Hashable identity of this result's findings (for de-duplication)."""
        return (
            tuple(f"{u.name}@{u.line}:{u.reason}" for u in self.unresolved),
            tuple(f"{m.name}={m.value}" for m in self.missing_paths),
        )

    def merged(self, other: "RenderResult", text: str) -> "RenderResult":
        return RenderResult(
            text=text,
            unresolved=self.unresolved + other.unresolved,
            missing_paths=self.missing_paths + other.missing_paths,
            used=self.used | other.used,
        )


_YAML_LINE = re.compile(
    r"^(?P<prefix>\s*(?:-\s+)?(?:[^\s:#'\"-][^:#]*?:[ \t]+)?)(?P<scalar>.*?)(?P<trail>[ \t]*)$"
)
_YAML_PLAIN_UNSAFE_START = set("-?:,[]{}#&*!|>'\"%@`")


def _yaml_plain_safe(s: str) -> bool:
    if not s or s != s.strip() or "\t" in s or "\n" in s:
        return False
    if s[0] in _YAML_PLAIN_UNSAFE_START:
        return False
    if ": " in s or " #" in s or s.endswith(":"):
        return False
    return True


def _frontmatter_line_span(lines: Sequence[str]) -> Tuple[int, int]:
    """``(first, last)`` 0-based indexes of the YAML frontmatter BODY lines,
    or ``(0, -1)`` when the document has none."""
    if not lines or lines[0].rstrip("\r\n") != "---":
        return (0, -1)
    for idx in range(1, len(lines)):
        if lines[idx].rstrip("\r\n") == "---":
            return (1, idx - 1)
    return (0, -1)


def render(
    text: str,
    ctx: Mapping[str, Optional[str]],
    *,
    allowed: Iterable[str],
    escape: str,
    verbatim: Iterable[str] = (),
    first_line: int = 1,
) -> RenderResult:
    """Substitute every allowed, resolvable ``{{NAME}}`` in ``text``.

    * ``allowed`` — the names this file class may use. A name outside it (or
      outside the registry) is ``unknown``; an allowed name whose value is
      ``None`` / absent is ``unresolvable``. Either way the token STAYS and is
      reported (owner rule 1) — this function never raises for a placeholder.
    * ``escape`` — one of :data:`ESCAPES`. ``yaml`` escapes only inside a
      leading ``---`` frontmatter block (the Markdown body below it is prose),
      and quotes a whole plain scalar when the rendered value would break it.
    * ``verbatim`` — extra names never escaped (on top of registry
      ``verbatim`` keys).
    * ``first_line`` — line number of ``text``'s first line in its file, for
      region renders.

    Every substituted ``must_exist`` key is checked on disk (owner rule 2).
    """
    if escape not in ESCAPES:
        raise ValueError(f"unknown escape {escape!r}; expected one of {ESCAPES}")
    allowed_set = frozenset(allowed)
    verbatim_set = frozenset(verbatim) | frozenset(k.name for k in _KEYS if k.verbatim)
    unresolved: List[Unresolved] = []
    missing: List[MissingPath] = []
    used: set = set()
    exists_cache: Dict[str, bool] = {}

    lines = text.splitlines(keepends=True)
    fm_first, fm_last = _frontmatter_line_span(lines) if escape == "yaml" else (0, -1)

    def _value(name: str, lineno: int) -> Optional[str]:
        if name not in allowed_set or name not in REGISTRY:
            unresolved.append(Unresolved(name, lineno, "unknown"))
            return None
        try:
            value = ctx.get(name)
        except Exception:  # noqa: BLE001 — a broken resolver is unresolvable
            value = None
        if value is None:
            unresolved.append(Unresolved(name, lineno, "unresolvable"))
            return None
        used.add(name)
        if REGISTRY[name].must_exist:
            if value not in exists_cache:
                try:
                    exists_cache[value] = Path(value).exists()
                except OSError:
                    exists_cache[value] = False
            if not exists_cache[value]:
                missing.append(MissingPath(name, value, lineno))
        return value

    def _sub_line(line: str, lineno: int, mode: str) -> str:
        def _one(m: "re.Match[str]") -> str:
            name = m.group(1)
            value = _value(name, lineno)
            if value is None:
                return m.group(0)
            if name in verbatim_set:
                return value
            if mode == "yaml_sq":  # inside a single-quoted YAML scalar
                return value.replace("'", "''")
            return escape_value(value, mode)
        return PLACEHOLDER_RE.sub(_one, line)

    out: List[str] = []
    for idx, line in enumerate(lines):
        lineno = first_line + idx
        if "{{" not in line:
            out.append(line)
            continue
        if escape == "yaml":
            if fm_first <= idx <= fm_last:
                out.append(_render_yaml_line(line, lineno, _sub_line))
            else:
                out.append(_sub_line(line, lineno, "none"))
            continue
        out.append(_sub_line(line, lineno, escape))

    return RenderResult(
        text="".join(out),
        unresolved=tuple(unresolved),
        missing_paths=tuple(missing),
        used=frozenset(used),
    )


def _render_yaml_line(line: str, lineno: int,
                      sub_line: Callable[[str, int, str], str]) -> str:
    """One frontmatter line: escape per the scalar's quoting style."""
    body = line.rstrip("\r\n")
    eol = line[len(body):]
    m = _YAML_LINE.match(body)
    if m is None:  # pragma: no cover — the pattern matches any line
        return sub_line(line, lineno, "none")
    prefix, scalar, trail = m.group("prefix"), m.group("scalar"), m.group("trail")
    if len(scalar) >= 2 and scalar[0] == scalar[-1] == '"':
        rendered = sub_line(scalar, lineno, "yaml")
    elif len(scalar) >= 2 and scalar[0] == scalar[-1] == "'":
        rendered = sub_line(scalar, lineno, "yaml_sq")
    else:
        rendered = sub_line(scalar, lineno, "none")
        if rendered != scalar and not _yaml_plain_safe(rendered):
            rendered = '"' + _yaml_double_quoted(rendered) + '"'
    return prefix + rendered + trail + eol


# ---------------------------------------------------------------------------
# Document-level entry points (what the bundle engine binds)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderSpec:
    """How ONE file class renders: its allowed names and its escape.

    ``regions=True`` renders only inside ``VCO-REWIRE`` regions
    (:func:`vco_lib.rewire.render_regions`); the escape is then chosen per
    file suffix.
    """

    allowed: frozenset
    escape: str = "none"
    regions: bool = False


def render_document(
    raw: bytes, spec: RenderSpec, ctx: Mapping[str, Optional[str]], *, filename: str = "",
) -> Tuple[bytes, RenderResult]:
    """Render file BYTES; returns ``(bytes, result)``.

    A region-scoped spec returns a region-less file BYTE-IDENTICAL (no decode
    round-trip). A whole-file render decodes UTF-8 with ``errors="replace"``,
    the shape every previous whole-file renderer used.
    """
    if spec.regions:
        from vco_lib.rewire import render_regions  # lazy: rewire imports this module

        return render_regions(raw, ctx, allowed=spec.allowed, filename=filename)
    text = raw.decode("utf-8", errors="replace")
    result = render(text, ctx, allowed=spec.allowed, escape=spec.escape)
    return result.text.encode("utf-8"), result


# De-dup for warnings printed without a sink (one line per finding per process).
_WARNED: set = set()


def warn(label: str, result: RenderResult, *, stream=None) -> None:
    """The stderr warning half of owner rules 1 and 2."""
    out = stream if stream is not None else sys.stderr
    for u in result.unresolved:
        what = ("unknown placeholder" if u.reason == "unknown"
                else "placeholder with no value on this machine")
        _safe_print(out, f"[vco] WARNING: {label}: line {u.line}: {what} "
                         f"{{{{{u.name}}}}} left in place (recorded in UPDATE_DEFERRED.md)")
    for m in result.missing_paths:
        _safe_print(out, f"[vco] WARNING: {label}: line {m.line}: {{{{{m.name}}}}} rendered "
                         f"to {m.value!r}, which does not exist on this machine "
                         f"(recorded in UPDATE_DEFERRED.md)")


def _safe_print(stream, msg: str) -> None:
    try:
        print(msg, file=stream, flush=True)
    except Exception:  # noqa: BLE001 — a warning must never break a render
        pass


def _warn_once(label: str, result: RenderResult) -> None:
    key = (label, result.problems())
    if key in _WARNED:
        return
    _WARNED.add(key)
    warn(label, result)


class FindingsSink:
    """Collects one run's per-file render results, warns once per finding,
    and settles the deferral rows at the end of the run.

    ``record`` may be called many times for the same file (the bundle engine
    renders for the hash and again for the write): the LAST result wins and a
    warning is printed only when the findings change.
    """

    def __init__(self, *, rerender_command: str = "") -> None:
        self.rerender_command = rerender_command
        self.results: Dict[str, RenderResult] = {}

    def record(self, label: str, result: RenderResult) -> None:
        prior = self.results.get(label)
        self.results[label] = result
        if not result.clean and (prior is None or prior.problems() != result.problems()):
            warn(label, result)

    def settle(self, folder: Path, *, log=None) -> Dict[str, List[str]]:
        return settle_deferrals(folder, self.results,
                                rerender_command=self.rerender_command, log=log)


class Transform:
    """The bundle engine's ``transform=`` callable, bound to one file.

    It stays a plain ``bytes -> bytes`` callable for the engine, and exposes
    ``spec`` / :meth:`render` so the completeness gate re-renders EXACTLY what
    ships under a synthetic context (no second enumeration, no mirror).
    """

    def __init__(self, label: str, spec: RenderSpec, context: MaterializeContext,
                 *, filename: str = "", sink: Optional[FindingsSink] = None) -> None:
        self.label = label
        self.spec = spec
        self.context = context
        self.filename = filename or Path(label).name
        self.sink = sink
        self._ctx = LazyContext(context)

    def render(self, raw: bytes, ctx: Optional[Mapping[str, Optional[str]]] = None
               ) -> Tuple[bytes, RenderResult]:
        return render_document(raw, self.spec, ctx if ctx is not None else self._ctx,
                               filename=self.filename)

    def __call__(self, raw: bytes) -> bytes:
        data, result = self.render(raw)
        if self.sink is not None:
            self.sink.record(self.label, result)
        elif not result.clean:
            _warn_once(self.label, result)
        return data


# ---------------------------------------------------------------------------
# Deferral rows (owner rules 1 + 2)
# ---------------------------------------------------------------------------

UNRENDERED_PREFIX = "template_placeholder_unrendered_"
PATH_MISSING_PREFIX = "template_path_missing_"


def _label_slug(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.replace("\\", "/").lower()).strip("_") or "file"
    if len(slug) > 60:
        digest = hashlib.sha1(label.encode("utf-8")).hexdigest()[:10]
        slug = slug[:48].rstrip("_") + "_" + digest
    return slug


def unrendered_condition_id(label: str) -> str:
    return f"{UNRENDERED_PREFIX}{_label_slug(label)}"


def path_missing_condition_id(label: str) -> str:
    return f"{PATH_MISSING_PREFIX}{_label_slug(label)}"


def _default_rerender_command(folder: Path) -> str:
    return ("# From the orchestrator root, re-run the ordinary update; a clean\n"
            "# render of the file clears this row by itself:\n"
            "python install.py --update")


def _unrendered_entry(label: str, result: RenderResult, command: str):
    from vco_lib.deferral_report import DeferralEntry

    items = "; ".join(
        f"{{{{{u.name}}}}} on line {u.line} "
        f"({'unknown to this VCO' if u.reason == 'unknown' else 'no value on this machine'})"
        for u in result.unresolved
    )
    return DeferralEntry(
        condition_id=unrendered_condition_id(label),
        title=f"Shipped file written with an unrendered placeholder: {label}",
        detected=(
            f"VCO materialized `{label}` but could not render: {items}. The file "
            "was written with the placeholder text left in place, so whatever reads "
            "that value sees the literal token."
        ),
        why_deferred=(
            "A placeholder the materializer cannot fill never fails the install or "
            "update (owner rule): the rest of the files are written and this row "
            "records the gap. An UNKNOWN placeholder means the template is newer "
            "than the code rendering it (usually the middle of an update); one with "
            "NO VALUE means this machine lacks what it names. The next clean render "
            "of this file clears the row automatically."
        ),
        command_to_apply=command,
        severity="warning",
        dismiss_fields={
            "file": label,
            "placeholders": ",".join(sorted({u.name for u in result.unresolved})),
        },
    )


def _missing_entry(label: str, result: RenderResult, command: str):
    from vco_lib.deferral_report import DeferralEntry

    items = "; ".join(f"{{{{{m.name}}}}} = `{m.value}` (line {m.line})"
                      for m in result.missing_paths)
    return DeferralEntry(
        condition_id=path_missing_condition_id(label),
        title=f"Rendered path does not exist on this machine: {label}",
        detected=(
            f"VCO rendered `{label}` and checked every path it baked in: {items} "
            "does not exist. The file was written; whatever it launches or reads "
            "at that path will fail until the path exists."
        ),
        why_deferred=(
            "The materializer writes the correct path for this install and never "
            "fails the run over it (owner rule). A missing interpreter or root "
            "usually means the venv or the clone moved or has not been built yet; "
            "the next render that finds every path present clears this row."
        ),
        command_to_apply=command,
        severity="warning",
        dismiss_fields={
            "file": label,
            "paths": ",".join(sorted(f"{m.name}={m.value}" for m in result.missing_paths)),
        },
    )


def settle_deferrals(
    folder: Path,
    results: Mapping[str, RenderResult],
    *,
    rerender_command: str = "",
    log=None,
) -> Dict[str, List[str]]:
    """Write/clear the two row families for every file rendered this run.

    One locked cycle. A file with findings gets its row (re-emission keeps the
    first ``detected_at``); a file rendered CLEAN clears any row it had
    (paired resolution). No ledger and nothing to add ⇒ nothing is touched —
    a clean install never creates the lock file or the ledger.

    Soft-fail: returns ``{"emitted": [...], "resolved": [...], "error": [...]}``.
    """
    summary: Dict[str, List[str]] = {"emitted": [], "resolved": [], "error": []}
    if not results:
        return summary
    folder = Path(folder)
    command = rerender_command or _default_rerender_command(folder)
    to_add = []
    to_clear: List[str] = []
    for label, result in sorted(results.items()):
        if result.unresolved:
            to_add.append(_unrendered_entry(label, result, command))
        else:
            to_clear.append(unrendered_condition_id(label))
        if result.missing_paths:
            to_add.append(_missing_entry(label, result, command))
        else:
            to_clear.append(path_missing_condition_id(label))

    from vco_lib.deferral_report import _DEFERRED_JSON_REL, _DEFERRED_REL

    if not to_add and not ((folder / _DEFERRED_REL).exists()
                           or (folder / _DEFERRED_JSON_REL).exists()):
        return summary
    try:
        from vco_lib.deferral_emit import WriteGate, _first_detected, locked_report

        gate = WriteGate()
        changed = False
        with locked_report(folder, gate=gate) as report:
            for cid in to_clear:
                if report.has_condition(cid):
                    report.mark_resolved(cid)
                    summary["resolved"].append(cid)
                    changed = True
            for entry in to_add:
                prior = report.entry_for(entry.condition_id)
                entry = _first_detected(prior, entry)
                if prior == entry:
                    continue
                report.add_entry(entry)
                summary["emitted"].append(entry.condition_id)
                changed = True
            if not changed:
                gate.write = False
    except Exception as exc:  # noqa: BLE001 — deferral I/O is best-effort
        summary["error"].append(f"{type(exc).__name__}: {exc}")
        if log is not None:
            try:
                log(f"materialize: deferral settle failed in {folder}: {exc}")
            except Exception:  # noqa: BLE001
                pass
    return summary


def _quote_arg(value: str) -> str:
    if platform.system().lower().startswith("win"):
        return '"' + value.replace('"', '\\"') + '"'
    import shlex

    return shlex.quote(value)


def bundle_rerender_command(folder: Path, orchestrator_root: Path) -> str:
    """The printed re-render command for a project's rows: the ordinary bundle
    update, run with the install's own interpreter (a bare ``python`` may not
    import ``vco_lib``)."""
    py = venv_python_path(Path(orchestrator_root))
    return (
        f"{_quote_arg(str(py))} -m vco_lib.project_init install-bundle "
        f"--folder {_quote_arg(str(folder))} "
        f"--orchestrator-root {_quote_arg(str(orchestrator_root))} --update"
    )


# ---------------------------------------------------------------------------
# The reader-facing placeholder table (templates/README.md)
# ---------------------------------------------------------------------------

README_TABLE_BEGIN = "<!-- BEGIN: placeholder-table (generated from vco_lib/materialize.py REGISTRY) -->"
README_TABLE_END = "<!-- END: placeholder-table -->"


def readme_placeholder_table() -> str:
    """The Markdown table ``templates/README.md`` carries between its
    placeholder-table markers — generated from :data:`REGISTRY`, so the
    documentation cannot list fewer names than the renderer accepts (survey
    gap a.3). ``tests/test_template_materialization_complete.py`` pins the
    README block to this output; regenerate with
    ``python -m vco_lib.materialize --readme-table``."""
    rows = ["| Placeholder | Expands to | Where it may appear |", "|---|---|---|"]
    for key in _KEYS:
        where = ("boot-unit templates (`templates/systemd`, `launchd`, `windows`)"
                 if key.resolver is None else
                 "`VCO-REWIRE` regions, agents, skills, project templates"
                 if key.name in PATH_KEYS else
                 "agents, skills, project templates, `rendered_root_files.toml` entries")
        rows.append(f"| `{{{{{key.name}}}}}` | {key.doc} | {where} |")
    return "\n".join(rows)


if __name__ == "__main__":  # pragma: no cover — maintenance helper
    if sys.argv[1:] == ["--readme-table"]:
        print(README_TABLE_BEGIN)
        print(readme_placeholder_table())
        print(README_TABLE_END)
    else:
        print("usage: python -m vco_lib.materialize --readme-table", file=sys.stderr)
        raise SystemExit(2)
