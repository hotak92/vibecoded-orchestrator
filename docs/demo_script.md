# Asciinema Demo Script — VibeCoded Tools Orchestrator

Target duration: **75 seconds** (range 60-90s).
Output: terminal recording embedded in README. No voiceover — pacing relies on prompt timing and visible output.

Record with:

```bash
asciinema rec -i 1.5 --title "VibeCoded Tools Orchestrator — persistent memory for Claude Code" demo.cast
```

`-i 1.5` caps idle time at 1.5s so viewers aren't waiting on real LLM responses.

---

## Prep (run BEFORE recording — do not include in the cast)

```bash
# 1. Fresh working directory
rm -rf /tmp/vct-demo && mkdir -p /tmp/vct-demo && cd /tmp/vct-demo

# 2. Seed a sample Python project (simulates a real codebase)
cat > auth.py <<'EOF'
def validate_token(token: str) -> bool:
    """Validate JWT token against our auth service."""
    return token.startswith("vct_") and len(token) == 40

def refresh_session(user_id: int) -> str:
    """Issue new session token for user."""
    return f"vct_{user_id:04d}{'x' * 36}"[:40]
EOF

# 3. Pre-stage a KG node that the demo will "discover"
mkdir -p knowledge/concepts
cat > knowledge/concepts/auth-strategy.md <<'EOF'
---
title: Auth Strategy
type: concept
tags: [auth, security]
status: active
---
We use opaque 40-char tokens prefixed `vct_`. JWT was rejected
because we don't need stateless validation (single service).
Rotation handled by refresh_session in auth.py.
EOF

# 4. Ensure containers are up (warm cache so demo feels snappy)
cd ~/vibecoded-orchestrator && docker compose up -d >/dev/null 2>&1
cd /tmp/vct-demo
```

---

## Recording script (12 steps)

### 1. Clean slate (t=0s, ~2s)

```bash
$ pwd
/tmp/vct-demo
$ ls
auth.py  knowledge/
```

`# note: small real project, no orchestrator yet`

### 2. One-command install (t=2s, ~8s)

```bash
$ curl -sSL https://vibecodedtools.it/install.sh | bash
[ok] docker detected
[ok] weaviate container started (port 8081)
[ok] ollama container started (port 11435)
[ok] qwen3-embedding:0.6b pulled
[ok] orchestrator installed in .claude/
Done. Run `claude` to start.
```

`# note: real install is ~2 min; asciinema -i 1.5 compresses this`

### 3. Index the existing project into the code graph (t=10s, ~6s)

```bash
$ .claude/scripts/code-graph-analyze . --project demo
Parsing 1 file... auth.py (2 functions)
Embedded with CodeSage-Large-v2 (2048-dim)
Indexed: 1 module, 0 classes, 2 functions
```

`# note: AST-based, not regex`

### 4. Sync the seeded KG node (t=16s, ~3s)

```bash
$ .claude/scripts/kg-sync --all
knowledge/concepts/auth-strategy.md -> ClaudeKnowledgeGraph (1 node)
```

### 5. Start Claude Code and ask a question (t=19s, ~10s)

```bash
$ claude
> How does auth work in this project?

[UserPromptSubmit hook] searching KG... found 1 node
[UserPromptSubmit hook] injected: concepts/auth-strategy.md (0.87)

You use opaque 40-char tokens prefixed `vct_` (not JWT).
Validation is in auth.py:validate_token. Rotation via
refresh_session. The decision to skip JWT was deliberate —
see knowledge/concepts/auth-strategy.md.
```

`# note: Claude cited the KG node it was never explicitly shown`

### 6. Make a decision, have Claude capture it (t=29s, ~8s)

```bash
> Remember: we will move token validation to a middleware in v2.

[store_knowledge_node] wrote knowledge/decisions/token-middleware-v2.md
[PostToolUse] synced to Weaviate
Noted. Captured as decisions/token-middleware-v2.md.
```

### 7. Exit Claude, close terminal (t=37s, ~3s)

```bash
> /exit
$ exit
```

`# note: session gone. Real "context amnesia" moment.`

### 8. New terminal, same project (t=40s, ~3s)

```bash
$ cd /tmp/vct-demo
$ claude
```

### 9. Ask about the decision made in the previous session (t=43s, ~8s)

```bash
> What did we decide about token validation?

[UserPromptSubmit hook] searching KG... found 2 nodes
[UserPromptSubmit hook] injected: decisions/token-middleware-v2.md (0.91)

We decided to move token validation to a middleware in v2.
Current implementation is inline in auth.py:validate_token.
```

`# note: persisted across session restart — this is the core value prop`

### 10. Code graph query — instant, no LLM call (t=51s, ~6s)

```bash
> /exit
$ .claude/scripts/code-graph-query search "token validation"
CodeFunction: auth.validate_token  (score 0.89)
  def validate_token(token: str) -> bool
  Validate JWT token against our auth service.
```

`# note: semantic code search, ~200ms, runs fully local`

### 11. Structural query — callers, dependencies, inheritance (t=57s, ~6s)

```bash
$ .claude/scripts/code-graph-query structure callers "auth.validate_token"
No callers found (0 references in indexed code).
Suggestion: this function is defined but unused in demo project.
```

`# note: AST-level truth, not grep`

### 12. Final frame — link to repo (t=63s, ~4s)

```bash
$ echo "github.com/VibeCoded-Tools/orchestrator"
github.com/VibeCoded-Tools/orchestrator
$ echo "AGPL-3.0 | runs 100% local | alpha"
AGPL-3.0 | runs 100% local | alpha
```

Total: ~67s (fits 60-90s window with buffer).

---

## Post-processing

1. Upload `.cast` to asciinema.org OR convert to SVG/GIF via `agg demo.cast demo.gif`.
2. Drop into README under a "See it work" heading above the feature list.
3. If GIF >3MB, regenerate with `agg --font-size 14 --speed 1.3`.

## Known risks to rehearse

- Step 2 install output must match current install.sh messages — verify before recording.
- Step 5/9 hook output format depends on `pre-edit-context-inject.sh` — check it still prints the `[UserPromptSubmit hook]` prefix.
- Step 6 assumes Claude will call `store_knowledge_node` autonomously. If the model doesn't, fall back to `kg add` CLI.
