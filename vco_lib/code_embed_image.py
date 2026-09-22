# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""ONE home for "is the running code-embed service built from THIS source?".

v0.2.92 BLOCKER-1.  ``code_embed`` is the only VCO service that ships as a
container image **built from the checkout**
(``infrastructure/docker-compose.yml::code_embed.build``), and both
``docker compose up`` and ``podman-compose up`` build an image only when it is
MISSING — a changed build context is NOT a rebuild trigger, and neither is
``--force-recreate`` (that replaces the CONTAINER, from the same image).

The consequence, measured on the maintainer's machine: image built
2026-05-16, container recreated 2026-07-12, and the live service still
silently truncating over-window input at HTTP 200 — while the v0.2.92 source
refuses it at HTTP 400.  A fix correct in git, green in CI, and absent from
every existing install.

This module is the falsifiable answer.  Two readings, one rule:

* :func:`checkout_source_sha` — what the CHECKOUT would build.
* :func:`probe_health` / :func:`served_state` — what the RUNNING service says
  it is built from (``/health.source_sha``, which the service computes from
  the files it actually loaded, so it cannot overstate its freshness).

Both digests come from ``image_source.source_sha``, a module that lives in
the build context and is COPYed into the image — one rule, one home, no
mirror to drift (CLAUDE.md A>B>C, rule A).

Consumers:

* the installer's compose-up — adds ``--build`` (and names ``code_embed``
  for ``--force-recreate``) when the state is not provably ``current``.
* ``vco_lib.doctor`` — ``probe_code_embed_image`` reports the mismatch and
  defers ``code_embed_image_stale``.
* ``templates/hooks/ensure-code-embed-service.{sh,ps1}`` — print the same
  one-line verdict at session start via the ``__main__`` CLI below.

Tri-state throughout: ``unknown`` is never rendered as "fine".  A service
that does not answer, an image that cannot compute its own digest, and a
digest-reporting image with nothing to compare it against are all *could not
look* — and a caller that cannot prove currency REBUILDS rather than
assuming.  A tree with no service source is NOT automatically one of them:
an image whose ``/health`` lacks the ``source_sha`` KEY is positively
pre-v0.2.92 — the silently-truncating population — and that reading needs no
digest at all (WP-3 judgment call 3, resolved 2026-09-22).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

#: Repo-relative location of the compose build context for ``code_embed``.
#: Matches ``docker-compose.yml``'s default
#: ``${VCT_CODE_EMBED_BUILD_CONTEXT:-../claude_mcp_servers/code_embedding_service}``
#: resolved from the repo root rather than from ``infrastructure/``.
SERVICE_SOURCE_REL = Path("claude_mcp_servers") / "code_embedding_service"

#: Compose service name (and therefore the name passed to ``--force-recreate``).
COMPOSE_SERVICE = "code_embed"

#: Default port, matching ``install.DEFAULT_CODE_EMBED_PORT`` and the
#: compose ``${CODE_EMBED_PORT:-11440}``.
DEFAULT_PORT = 11440

#: Verdicts. ``CURRENT`` requires POSITIVE evidence (a digest that matches);
#: everything else is one of the other two.
CURRENT = "current"
STALE = "stale"
UNKNOWN = "unknown"

#: Exit codes of the ``__main__`` CLI, so a shell hook can branch without
#: parsing text.
EXIT_BY_VERDICT = {CURRENT: 0, STALE: 1, UNKNOWN: 2}


@dataclass(frozen=True)
class ImageState:
    """One verdict about the running service, with the evidence behind it."""

    verdict: str
    #: One line, safe to print verbatim in a hook or the doctor.
    summary: str
    #: Digest the checkout would build (``None`` when it could not be read).
    expected_sha: Optional[str] = None
    #: Digest the service reports (``None`` when absent/unreadable).
    served_sha: Optional[str] = None

    @property
    def is_stale(self) -> bool:
        return self.verdict == STALE

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "summary": self.summary,
            "expected_sha": self.expected_sha,
            "served_sha": self.served_sha,
        }


def service_source_dir(install_root) -> Optional[Path]:
    """The build context inside ``install_root``, or ``None`` when absent.

    Absent is the NORMAL state for a per-project install: user projects do
    not bundle the service source (that is exactly why the compose build
    context is overridable via ``VCT_CODE_EMBED_BUILD_CONTEXT``).  A tree
    with no source has nothing to compare against — ``unknown``, not a
    problem.
    """
    candidate = Path(install_root) / SERVICE_SOURCE_REL
    override = os.environ.get("VCT_CODE_EMBED_BUILD_CONTEXT", "").strip()
    if override:
        candidate = Path(override)
    return candidate if candidate.is_dir() else None


def install_root_with_service_source() -> Optional[Path]:
    """The orchestrator install root, but only when it can answer about the
    image — i.e. only when it actually carries the build context.

    THE one home for "which tree decides the machine's image freshness".
    The machine runs exactly ONE ``code_embed`` service, built from the
    install root's source, so a caller holding a PER-PROJECT tree (the resync
    driver's ``repo_root``, the deferral dispatcher's folder, the exposure
    heal) must ask about this tree instead of its own — and before v0.2.96's
    ship-gate MAJOR-2 fix, only the heal did, which left the gate protecting
    it inert on every per-project path.

    ``None`` means no tree on this machine carries the build context (no
    install root resolves — e.g. a ``vco_lib`` imported from outside a clone
    — or the clone is pruned). Never raises: could-not-look is ``None``.
    """
    try:
        from vco_lib.python_exe import resolve_install_root

        install_root = resolve_install_root()
        if install_root is None:
            return None
        return (
            Path(install_root)
            if service_source_dir(install_root) is not None
            else None
        )
    except Exception:  # noqa: BLE001 — could not look is not a verdict
        return None


def checkout_source_sha(install_root) -> Optional[str]:
    """Digest the checkout would build into the image, or ``None``.

    Loads ``image_source.py`` FROM the build context by path — the same file
    the Dockerfiles COPY into the image — so the host and the service can
    never disagree about the rule.  Any failure yields ``None`` (could not
    look), never a fabricated digest.
    """
    source_dir = service_source_dir(install_root)
    if source_dir is None:
        return None
    return source_sha_of(source_dir)


def source_sha_of(source_dir) -> Optional[str]:
    """``image_source.source_sha(source_dir)``, loaded by path. ``None`` on failure."""
    source_dir = Path(source_dir)
    module_path = source_dir / "image_source.py"
    try:
        spec = importlib.util.spec_from_file_location(
            "_vco_code_embed_image_source_host", module_path
        )
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.source_sha(source_dir)
    except Exception:  # noqa: BLE001 — could not look is not a verdict
        return None


def service_base_url(explicit: Optional[str] = None) -> str:
    """Base URL of the code-embed service.

    Order: explicit arg → ``CODE_EMBED_SERVICE_URL`` → ``http://localhost:<CODE_EMBED_PORT|11440>``.

    THE one home for this order (v0.2.92 R2).  It was previously inlined in
    ``vco_lib/embedding_service.py`` (two sites, via
    ``_shared_service_base_url``) and ``vco_lib/codegraph_resync.py``
    (``code_embed_service_healthy``); all three now call this.  Do not add a
    fourth copy — an empty ``CODE_EMBED_SERVICE_URL`` and an empty
    ``CODE_EMBED_PORT`` are both "unset" here, and the two embedding_service
    copies additionally ignored ``CODE_EMBED_PORT`` entirely, which is the
    kind of divergence a second copy produces within one release.
    """
    if explicit:
        return _strip_health_suffix(explicit.rstrip("/"))
    from_env = os.environ.get("CODE_EMBED_SERVICE_URL", "").strip()
    if from_env:
        return _strip_health_suffix(from_env.rstrip("/"))
    port = os.environ.get("CODE_EMBED_PORT", "").strip() or str(DEFAULT_PORT)
    return f"http://localhost:{port}"


_HEALTH_SUFFIX = "/health"


def _strip_health_suffix(base: str) -> str:
    """A URL that already names ``/health`` is still a base URL here.

    install.py's service detector hands over the exact URL it probed
    (``http://localhost:11440/health``); before v0.2.93 ``probe_health``
    appended the path again, GET ``/health/health`` 404'd, and the verdict
    read "service is not answering /health" for a service that was answering
    fine (field 2026-09-07). The rebuild happened to fire anyway, for the
    wrong reason — the digest comparison this module exists for never ran.
    """
    if base.endswith(_HEALTH_SUFFIX):
        return base[: -len(_HEALTH_SUFFIX)]
    return base


def probe_health(url: Optional[str] = None, timeout: float = 3.0) -> Optional[dict]:
    """``GET /health`` as a dict, or ``None`` when the service could not be read.

    Never raises: an unreachable, slow, or non-JSON service is *could not
    look*, and this runs on install and session-start paths that must not be
    able to fail because a probe did.
    """
    health = f"{service_base_url(url)}/health"
    try:
        with urllib.request.urlopen(health, timeout=timeout) as resp:
            if resp.status >= 400:
                return None
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — every failure is "could not look"
        return None
    return payload if isinstance(payload, dict) else None


def served_state(expected_sha: Optional[str], health: Optional[dict]) -> ImageState:
    """Pure: turn (checkout digest, /health payload) into one verdict.

    Kept pure so every branch — including the two that must NOT read as
    "fine" — is unit-testable without a service, a container runtime, or a
    network.  The ordering matters and is deliberate:

    1. no health payload → ``unknown`` (service down / CPU tier / not ours);
    2. ``/health`` not reporting ``status=ok`` → ``unknown``;
    3. health without a ``source_sha`` KEY → ``stale``: every image that
       ships v0.2.92's ``server.py`` reports the field unconditionally, so
       its ABSENCE positively identifies a pre-v0.2.92 image — the exact
       population that is still truncating silently.  **This arm needs no
       checkout digest**: it is a fact about the RUNNING image, not a
       comparison (WP-3 judgment call 3, resolved 2026-09-22).  It used to
       sit BELOW the no-digest arm, so on any tree without the build
       context the evidence was discarded and the WP-3 gate could not bite
       — including on a machine whose ``vco_lib`` does not live inside a
       clone (the site-packages-shadow shape), where no install root
       resolves either;
    4. no checkout digest → ``unknown`` (the image reports a digest, so it
       carries v0.2.92's refusal; there is simply nothing to compare it to);
    5. ``source_sha`` present but ``null`` → ``unknown`` (the service could
       not hash its own files; different fact, different verdict);
    6. digests equal → ``current``; otherwise ``stale``.
    """
    if not health:
        return ImageState(
            UNKNOWN,
            "code_embed: service is not answering /health — cannot read the "
            "image's source digest."
            if expected_sha else
            "code_embed: no service source in this tree and the service is "
            "not answering /health — cannot say whether the running image "
            "is current.",
            expected_sha=expected_sha,
        )
    if health.get("status") != "ok":
        return ImageState(
            UNKNOWN,
            "code_embed: /health did not report status=ok — cannot read the "
            "image's source digest.",
            expected_sha=expected_sha,
        )
    if "source_sha" not in health:
        return ImageState(
            STALE,
            "code_embed: the running service predates v0.2.92 (its /health "
            "reports no source_sha). It still TRUNCATES over-window input "
            "silently at HTTP 200 instead of refusing it.",
            expected_sha=expected_sha,
        )
    if not expected_sha:
        return ImageState(
            UNKNOWN,
            "code_embed: no service source in this tree — cannot say whether "
            "the running image is current (it does report a source digest, "
            "so it refuses over-window input rather than truncating it).",
        )
    served = health.get("source_sha")
    if served is None:
        return ImageState(
            UNKNOWN,
            "code_embed: the running service could not compute its own source "
            "digest — cannot say whether the image is current.",
            expected_sha=expected_sha,
        )
    if str(served) == str(expected_sha):
        return ImageState(
            CURRENT,
            "code_embed: the running service is built from the current source.",
            expected_sha=expected_sha,
            served_sha=str(served),
        )
    return ImageState(
        STALE,
        "code_embed: the running service is built from OLDER source than this "
        f"checkout (image {str(served)[:12]}, source {str(expected_sha)[:12]}).",
        expected_sha=expected_sha,
        served_sha=str(served),
    )


def image_state(
    install_root,
    url: Optional[str] = None,
    timeout: float = 3.0,
    health: Optional[dict] = None,
    expected_sha: Optional[str] = None,
) -> ImageState:
    """Composed reading: checkout digest vs the live service.

    ``health`` / ``expected_sha`` are injection seams so tests describe a
    whole machine without a service or a checkout.
    """
    expected = expected_sha if expected_sha is not None else checkout_source_sha(install_root)
    payload = health if health is not None else probe_health(url, timeout=timeout)
    return served_state(expected, payload)


@dataclass(frozen=True)
class RebuildPlan:
    """What the installer's compose-up should do about the image, and say."""

    #: append ``--build`` to the compose invocation.
    build: bool
    #: services to add to the caller's ``services_to_recreate`` list.
    recreate: tuple = ()
    #: stdout lines the installer prints verbatim.
    lines: tuple = ()
    verdict: str = UNKNOWN
    expected_sha: Optional[str] = None
    served_sha: Optional[str] = None


def rebuild_allowed(
    decisions: Optional[dict],
    has_gpu: bool,
    force_separate: bool,
    *,
    adopt_action: str,
    managed_probe: str,
) -> bool:
    """Pure: may this run rebuild the ``code_embed`` IMAGE?

    Ownership, in the same shape as install.py's weaviate reclaim-drift gate:

    * ``has_gpu`` False → the CPU tier does not run this service at all
      (the installer's disposition loop already skips it); nothing to build.
    * ``force_separate`` → no adopt classification exists on that path; the
      caller brings up the whole stack it owns.
    * no ``decisions`` (legacy caller) → same rule the legacy start-list
      uses: GPU hosts manage code_embed.
    * a FOREIGN adopt (someone else's container, only reachable via an
      explicit ``--on-conflict adopt``) → NEVER. Rebuilding the image under a
      service we do not own is the same violation as recreating it.

    ``adopt_action`` / ``managed_probe`` are passed IN rather than imported:
    the decision vocabulary belongs to install.py, and copying its constants
    here would be a second home for them.
    """
    if not has_gpu:
        return False
    if force_separate or not decisions:
        return True
    entry = (decisions or {}).get(COMPOSE_SERVICE, {}) or {}
    if entry.get("action") == adopt_action and entry.get("probe") != managed_probe:
        return False
    return True


def plan_rebuild(
    *,
    decisions: Optional[dict],
    has_gpu: bool,
    force_separate: bool,
    install_root,
    url: Optional[str],
    services_to_start,
    services_to_recreate,
    adopt_action: str,
    managed_probe: str,
    state: Optional[ImageState] = None,
) -> RebuildPlan:
    """Decide whether the installer's compose-up must rebuild the image.

    Every other compose service pulls a pinned upstream image; ``code_embed``
    is BUILT from ``claude_mcp_servers/code_embedding_service``. Compose
    builds only when the image is missing, so on every existing install the
    image stays at whatever it was first built from — v0.2.92's over-window
    REFUSAL (and the ``CODE_EMBED_MAX_SEQ_LEN`` knob) never shipped, and the
    release's own code-graph remedy would re-walk the whole graph through the
    old, silently-truncating service.

    Why not an unconditional rebuild: on a host without the base image cached
    it is a multi-GB pull on every install run, and the CUDA variant's base is
    ~6 GB. Why not "never": that is the defect. The middle is POSITIVE
    EVIDENCE — the service reports the digest of the source it is running on
    ``/health``, and we rebuild only when that does not match the checkout.
    Steady state after this release is therefore a no-op; the one rebuild
    every existing install owes happens once, on the update the release note
    names.

    Naming the service in ``recreate`` is what makes the rebuilt image REACH
    the running container: the flag alone leaves an already-running container
    on its old image under podman-compose. It also keeps the invocation
    surgical — every unnamed service (including a foreign adopt) is left
    untouched, the v0.2.61 invariant.

    ``state`` is an injection seam for tests; production passes ``None`` and
    the probe runs. A probe that RAISES yields "no rebuild planned" plus a
    warning line: a freshness check must never be able to fail an install.
    """
    if not rebuild_allowed(
        decisions, has_gpu, force_separate,
        adopt_action=adopt_action, managed_probe=managed_probe,
    ):
        return RebuildPlan(build=False)
    if state is None:
        try:
            state = image_state(install_root, url=url)
        except Exception as exc:  # noqa: BLE001 — a probe never fails the install
            return RebuildPlan(
                build=False,
                lines=(f"  WARNING: could not check the code_embed image state: {exc}",),
            )
    if state.is_stale:
        # v0.2.96 (M-2): persist the truncating-cohort observation BEFORE the
        # rebuild this plan sets up can erase the live evidence — on an owned
        # machine the same update run rebuilds the image and only THEN reaches
        # the codegraph-maintenance step whose exposure detection needs to
        # know a pre-v0.2.92 image was serving.  Soft-fail: an observation is
        # evidence bookkeeping and must never gate the rebuild itself.
        try:
            from vco_lib.code_embed_exposure import observe_stale_image

            observe_stale_image(state)
        except Exception:  # noqa: BLE001 — observation never gates the rebuild
            pass
    if state.verdict == CURRENT:
        return RebuildPlan(
            build=False, verdict=state.verdict,
            expected_sha=state.expected_sha, served_sha=state.served_sha,
        )
    recreate: tuple = ()
    if (
        not force_separate
        and COMPOSE_SERVICE not in (services_to_start or ())
        and COMPOSE_SERVICE not in (services_to_recreate or ())
    ):
        recreate = (COMPOSE_SERVICE,)
    return RebuildPlan(
        build=True,
        recreate=recreate,
        lines=(
            f"  [rebuild] {state.summary}",
            "            Adding `--build` so compose rebuilds the image from "
            "source (`up -d` builds only when it is MISSING).",
        ),
        verdict=state.verdict,
        expected_sha=state.expected_sha,
        served_sha=state.served_sha,
    )


def build_rejected_lines(compose_cmd: str, infra_dir) -> tuple:
    """What the installer prints when its compose rejected ``--build``.

    Degrading LOUDLY beats both failing an install that would otherwise
    succeed and pretending the image was refreshed.
    """
    return (
        "  WARNING: the code_embed image was NOT rebuilt — this compose does "
        "not accept `--build` on `up`. The service keeps running its existing "
        "image, which for a pre-v0.2.92 image means over-window code is still "
        "TRUNCATED silently. Rebuild it explicitly:",
        f"    {compose_cmd} --profile gpu up -d --build --force-recreate "
        f"{COMPOSE_SERVICE}",
        f"    (from {infra_dir})",
    )


@dataclass(frozen=True)
class RebuildContext:
    """Everything :func:`rebuild_command` needs to know about THIS machine.

    ``compose_cmd`` is the invocation this host actually has (``podman
    compose`` / ``podman-compose`` / ``docker compose``); ``identity`` is the
    compose project that created the running container, or ``None`` when
    there is none, it carries no labels, or nothing could be asked. Both
    default to the shape a plain install has, so a failed probe degrades to
    the command that is right for the common machine rather than to silence.
    """

    compose_cmd: str = "docker compose"
    identity: Optional[object] = None


def rebuild_context() -> RebuildContext:
    """Read the machine once for the two facts the printed command needs.

    Soft-fail throughout and READ-ONLY: a runtime resolution (the ONE home in
    :mod:`vco_lib.containers`, so the doctor cannot disagree with the
    installer about which compose exists) plus two container probes. Called
    only from the STALE branch of the doctor's probe — the branch that is
    about to print a command — so a healthy machine pays nothing.
    """
    from vco_lib import containers as _containers

    compose_cmd = "docker compose"
    runtime = "podman"
    try:
        resolution = _containers.resolve()
    except Exception:  # noqa: BLE001 — a probe never fails the doctor
        resolution = None
    if resolution is not None:
        resolved = getattr(resolution, "runtime", None)
        if resolved:
            runtime = str(resolved)
        argv = getattr(resolution, "compose", None)
        if argv:
            compose_cmd = " ".join(str(part) for part in argv)
    return RebuildContext(
        compose_cmd=compose_cmd, identity=owning_compose_identity(runtime),
    )


def owning_compose_identity(runtime: str = "podman"):
    """The compose identity that CREATED the running ``code_embed`` container.

    ``None`` when there is no such container, when it carries no compose
    labels, or when the runtime cannot be asked. Read-only: two probes
    (``container exists`` and ``inspect``) through
    :mod:`vco_lib.containers`, the one home for both.

    Why the doctor needs this at all (v0.2.95 F2): the rebuild command is only
    true if it is run in the project that owns the container. Compose derives
    the image NAME from the project, so ``--build`` under a different project
    produces a different image that the running container never loads — which
    is exactly why ``install_services_guard.apply_recreate_guard`` strips
    ``code_embed`` from the build list on those machines rather than building
    the wrong thing.
    """
    from vco_lib import containers as _containers

    try:
        ref = _containers.find_existing_container(COMPOSE_SERVICE, runtime)
    except Exception:  # noqa: BLE001 — could not look is not a verdict
        return None
    if not ref:
        return None
    try:
        return _containers.compose_identity_of(ref, runtime)
    except Exception:  # noqa: BLE001 — could not look is not a verdict
        return None


def rebuild_command(
    install_root, compose_cmd: str = "docker compose", identity=None,
    services: "Optional[Sequence[str]]" = None,
) -> str:
    """The explicit command a user can run to refresh the image themselves.

    Two shapes, because "the installer's compose project" and "the project
    that owns the running container" are not always the same machine fact:

    * ``identity is None`` (or it names no working directory) → the
      installer's own ``infrastructure/`` project. The shape every plain
      install can run.
    * ``identity`` names a FOREIGN compose project → that project's
      invocation, built from the container's own labels (its config files and
      working directory), because a build under any other project name
      produces an image the running container will never load.

    ``services`` (v0.2.96 WP-4 Task 3a) lets a caller rebuild the OWNING
    image name for the whole foreign set at once — the guard's deferral
    remedy derives its Option A through this parameter.  Default (``None``)
    keeps the byte-identical ``code_embed``-only shape every existing
    caller and test pins.

    v0.2.95 F2 — this function shipped in v0.2.92 with ZERO callers while
    ``doctor.py`` promised, in prose, that the ``code_embed_image_stale``
    entry carries "the explicit compose command" and printed
    ``install.py --update`` instead. On the machines that also carry
    ``services_foreign_compose_identity`` that update is the run which just
    refused to rebuild, so the remediation could not fix the condition that
    produced it. Making this live is the fix; the identity arm is what makes
    it TRUE rather than merely present.

    Three details in the identity arm, each of which alone made the printed
    command UNRUNNABLE on the population it targets (review MAJOR-1):

    * **The label's paths are absolutised against ``working_dir``.**
      podman-compose records ``config_files`` exactly as the user typed it on
      the command line (``podman_compose.py`` ``relative_files = files`` →
      the project label), so the live value on a machine started from its own
      compose directory is ``compose.yaml,compose.override.yaml``. Both
      providers resolve ``-f`` against the CURRENT directory, and the remedy's
      first step leaves the user in the orchestrator root — where neither file
      exists.
    * **The project name is passed explicitly** (``-p``, accepted by
      docker-compose v2 and podman-compose alike). Without it the name comes
      from a ``name:`` key or the directory, i.e. from whichever file happened
      to resolve — and the project name is precisely what decides the image
      name this rebuild has to overwrite.
    * **No ``--project-directory``.** podman-compose 1.5.0's parser does not
      have the flag at all (argparse error, not a warning), and it is redundant
      once the files are absolute: both providers derive the project directory
      from the first ``-f``.

    With no usable file list the arm falls back to ``cd <working_dir>``, which
    pins the same directory by the other means rather than emitting a bare
    invocation that would resolve against the caller's cwd.
    """
    working_dir = (getattr(identity, "working_dir", "") or "").strip()
    config_files = (getattr(identity, "config_files", "") or "").strip()
    service_list = " ".join(services) if services else COMPOSE_SERVICE
    if identity is not None and working_dir:
        files = [
            str(candidate if candidate.is_absolute()
                else Path(working_dir) / candidate)
            for candidate in (
                Path(part.strip())
                for part in config_files.split(",") if part.strip()
            )
        ]
        project = (getattr(identity, "project", "") or "").strip()
        command = " ".join(
            fragment for fragment in (
                compose_cmd,
                " ".join(f"-f {path}" for path in files),
                f"-p {project}" if project else "",
                f"--profile gpu up -d --build --force-recreate {service_list}",
            ) if fragment
        )
        return command if files else f"cd {working_dir} && {command}"
    infra = Path(install_root) / "infrastructure"
    return (
        f"cd {infra} && {compose_cmd} --profile gpu up -d --build "
        f"--force-recreate {service_list}"
    )


def _main(argv: Optional[list] = None) -> int:  # pragma: no cover — CLI entry
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.code_embed_image",
        description=(
            "Report whether the running code-embedding service is built from "
            "the current checkout. Exit 0=current, 1=stale, 2=unknown."
        ),
    )
    parser.add_argument("--root", default=None, help="install root (default: repo root)")
    parser.add_argument("--url", default=None, help="service base URL override")
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--quiet-unless-stale",
        action="store_true",
        help="print nothing unless the verdict is 'stale' (session-start hooks)",
    )
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else Path(__file__).resolve().parent.parent
    state = image_state(root, url=args.url, timeout=args.timeout)
    if args.json:
        print(json.dumps(state.to_dict()))
    elif not args.quiet_unless_stale or state.is_stale:
        print(state.summary)
        if state.is_stale:
            # v0.2.96 (M-2): the pre-fix text promised "rebuild, THEN re-run
            # the code-graph re-sync" — false for the rows that matter: a
            # plain resync hash-SKIPS exactly the rows that were embedded
            # through the truncating image (their content hashes are correct;
            # only their vectors are corrupted).  State the real remedy.
            print(
                "  Fix: REBUILD the image FIRST — `python install.py "
                f"--update` from {root}, or the explicit compose command the "
                "`code_embed_image_stale` deferral entry prints. Rows "
                "embedded while a stale (pre-v0.2.92) image was serving are "
                "NOT healed by a plain resync — their content hashes are "
                "correct, so every hash gate skips them. VCO detects that "
                "exposure at update time and queues a ONE-TIME re-embed "
                "(vco_lib.code_embed_exposure) that fires on the next "
                "code-graph resync running under a non-stale image; a "
                "`--force-recreate` rebuild remains the unconditional manual "
                "escape."
            )
    return EXIT_BY_VERDICT.get(state.verdict, 2)


if __name__ == "__main__":  # pragma: no cover — CLI entry
    sys.exit(_main())
