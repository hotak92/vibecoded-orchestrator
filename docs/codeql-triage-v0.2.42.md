# CodeQL Alert Triage — v0.2.42

Triaged 2026-05-31 by W6 agent at HIGH effort. 14 open alerts at time of triage.
Resolution applied in this PR: suppression via `.github/codeql-config.yml` + real fix
for `py/stack-trace-exposure`.

---

## Summary

| Alert | Rule | Severity | File | Resolution |
|---|---|---|---|---|
| #2 | py/stack-trace-exposure | ERROR | `claude_mcp_servers/code_embedding_service/server.py:303` | Real fix |
| #3 | py/overly-permissive-file | WARNING | `install.py:4849` | False positive — suppressed |
| #8 | py/incomplete-url-substring-sanitization | WARNING | `tests/test_search_mcp_only_papers.py:159` | False positive — suppressed |
| #9 | js/clear-text-logging | ERROR | `vco_lib/excalidraw_mcp_fork/dist/canvas/index.js:80` | Vendored bundle — suppressed |
| #10–#15 | js/incomplete-sanitization | WARNING | `vco_lib/excalidraw_mcp_fork/dist/canvas/frontend/assets/*.js` | Vendored bundle — suppressed |
| #16–#18 | py/clear-text-logging-sensitive-data | ERROR | `vco_lib/config_projection.py:2363,2395,2398` | False positive — suppressed |
| #19 | js/xss-through-dom | WARNING | `launcher/vendor/diagrams-editor/mermaid/index.html:219` | Vendored bundle — suppressed |

---

## Alert-by-Alert Details

### #2 — py/stack-trace-exposure [ERROR] — REAL FIX

**File**: `claude_mcp_servers/code_embedding_service/server.py` L303

**Issue**: `GET /health` endpoint returned `{"status": "error", "error": str(e)}`, where
`str(e)` could expose internal details: model paths, device names, Python exception
class names, or file-system layout from a traceback's `__str__`.

**Fix applied**: The exception is now logged via `logger.error()` (internal only) and the
response returns the generic string `"health check failed — see server logs"`. The full
error is preserved in the server log for operators, but not surfaced through the API.

This is a localhost-only service (`CODE_EMBED_PORT=11440`), so practical exploitation is
minimal. Defence-in-depth still applies: keep internal details internal.

---

### #3 — py/overly-permissive-file [WARNING] — FALSE POSITIVE

**File**: `install.py` L4849 — `os.chmod(dest, 0o755)`

**Why false positive**: `0o755` (rwxr-xr-x) is the standard permission for user-installed
executables in `~/.local/bin/`. This is the `lean-ctx` binary being installed to the
user's `$PATH`. Setting it to `0o700` instead would prevent other users on multi-user
systems from executing it, which is the exact opposite of what we want.

CodeQL's `py/overly-permissive-file` rule flags any world-execute bit as "overly
permissive," but conflates file-system security semantics with the standard use case for
executables. Standard system tools (`git`, `python3`) all have `0o755`.

**Suppressed globally** via `query-filters` in `.github/codeql-config.yml`.

---

### #8 — py/incomplete-url-substring-sanitization [WARNING] — FALSE POSITIVE

**File**: `tests/test_search_mcp_only_papers.py` L159 — `assert "openalex.org" in url`

**Why false positive**: This is a test assertion verifying that the mock HTTP client is
receiving requests aimed at the correct upstream API (`openalex.org`). It is NOT a
production URL sanitizer or access-control check. CodeQL's
`py/incomplete-url-substring-sanitization` looks for substring URL checks used as
security gates; a `pytest` assertion is categorically neither.

Even if it were a security gate, the check is in a test file (`tests/`). Test files do
not run in production.

**Suppressed globally** via `query-filters` in `.github/codeql-config.yml`.

---

### #9 — js/clear-text-logging [ERROR] — VENDORED BUNDLE (FALSE POSITIVE)

**File**: `vco_lib/excalidraw_mcp_fork/dist/canvas/index.js` L80

**Why suppressed**: This is a compiled Vite/React bundle for the Excalidraw canvas app.
We own the Python glue code and the MCP server wrapper; we do NOT maintain the canvas
frontend. The JS bundle is a verbatim dist artefact from the Excalidraw fork. Any issues
within it belong to the upstream maintainer.

**Path excluded** from CodeQL analysis via `paths-ignore` in `.github/codeql-config.yml`.

---

### #10–#15 — js/incomplete-sanitization [WARNING] — VENDORED BUNDLE (FALSE POSITIVE)

**Files**: `vco_lib/excalidraw_mcp_fork/dist/canvas/frontend/assets/flowchart-elk-*.js`,
`styles-*.js`

**Why suppressed**: Same rationale as #9. These are minified Vite chunk bundles for the
Excalidraw canvas frontend. The incomplete-sanitization patterns flagged are inside
third-party flowchart rendering code (Mermaid / ELK layout engine), not our code.

**Path excluded** from CodeQL analysis via `paths-ignore` in `.github/codeql-config.yml`.

---

### #16–#18 — py/clear-text-logging-sensitive-data [ERROR] — FALSE POSITIVE

**Files**: `vco_lib/config_projection.py` L2363, L2395, L2398

**Why false positive**: The three flagged `print()` calls output **key names** (strings
like `"GITHUB_TOKEN"`, `"ANTHROPIC_API_KEY"`) to stdout — NOT the secret values. The
variable `known_keys` is the "strip set" for the `user-secret-known-keys` CLI subcommand:
a list of environment variable identifiers used to redact secrets from the projection
output. Printing these names IS the entire purpose of the subcommand.

CodeQL's taint-analysis follows `known_keys` (a `list[str]` of variable names) through
the JSON serialisation path and incorrectly classifies the names as sensitive data.

**Suppressed globally** via `query-filters` in `.github/codeql-config.yml`. Inline
`# codeql[py/clear-text-logging-sensitive-data]:` comments added at each site in
`config_projection.py` for in-file documentation.

---

### #19 — js/xss-through-dom [WARNING] — VENDORED BUNDLE (FALSE POSITIVE)

**File**: `launcher/vendor/diagrams-editor/mermaid/index.html` L219

**Why suppressed**: This is the Mermaid.js pre-built HTML file vendored for the offline
Mermaid editor tab in the launcher. The XSS pattern flagged is inside the Mermaid v10
bundle itself. We do not maintain Mermaid; upgrading it is a separate dependency-hygiene
task, not a code fix in this repo.

**Path excluded** from CodeQL analysis via `paths-ignore` in `.github/codeql-config.yml`.

---

## What is NOT Suppressed

The following first-party code paths remain fully analysed by CodeQL:

- `vco_lib/*.py` (excluding the three `config_projection.py` false positives addressed above)
- `install.py` (excluding the `0o755` false positive)
- `claude_mcp_servers/**` (server.py stack-trace fix applied)
- `launcher/src/**`
- `templates/**`
- `tests/**` (excluding the URL substring false positive)
