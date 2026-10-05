---
name: debug-expert
description: Investigate complex bugs, intermittent failures, performance degradations, and system-wide issues requiring deep reasoning across multiple components
short_desc: "investigate hard bugs: flaky, intermittent, races"
keywords: ["intermittent failure", "flaky test", "performance degradation", "race condition", "system-wide bug", "root cause", "hard to reproduce", "weird bug", heisenbug, "debug this", "why does this fail"]
model: opus
---

# Debug Expert (Opus)

**Purpose**: Investigate complex bugs, intermittent failures, performance degradations, and system-wide issues requiring deep reasoning across multiple components.

**Model**: Opus (handles ambiguity, traces root causes, forms hypotheses)

**When to Invoke Autonomously**:

Use this skill when:
1. **Intermittent Failures**: Bug only reproduces sometimes (race conditions, timing issues)
2. **System-Wide Problems**: Issue spans multiple components/services
3. **Performance Degradation**: Unexplained slowdowns or resource exhaustion
4. **Production Incidents**: Critical issues affecting users
5. **No Obvious Cause**: Error messages are vague or misleading
6. **Multiple Failed Fixes**: Previous attempts haven't resolved the issue

**DO NOT invoke for**:
- Obvious errors with clear stack traces
- Syntax errors or type mismatches
- Simple null pointer issues
- Errors with clear messages pointing to exact line

## Decision Tree

```
Bug is:
├─ Clear error message + obvious fix? → Just fix it
├─ Intermittent/hard to reproduce? → Use this skill
├─ Spans multiple components? → Use this skill
├─ Performance issue without clear cause? → Use this skill
├─ Production incident affecting users? → Use this skill
├─ Simple syntax/type error? → Don't use this skill
└─ Already tried 2+ fixes that didn't work? → Use this skill
```

## Usage

```
/debug-expert investigate [issue description]
/debug-expert performance-trace [slow operation]
/debug-expert root-cause-analysis [incident]
```

## What This Skill Does

### 1. Hypothesis Formation
- Generates 3-5 potential root causes based on symptoms
- Ranks hypotheses by likelihood
- Identifies key assumptions to test

### 2. Evidence Gathering
- Reviews relevant code paths
- Analyzes logs, traces, metrics
- Examines recent changes (git history)
- Checks system state (DB, cache, queues)

### 3. Systematic Investigation
- Designs tests to confirm/eliminate hypotheses
- Adds instrumentation (logging, metrics, traces)
- Reproduces issue in controlled environment
- Bisects problem space (binary search approach)

### 4. Root Cause Identification
- Traces issue to specific code/config/data
- Distinguishes symptoms from underlying cause
- Documents failure chain (what led to what)

### 5. Fix Recommendation
- Proposes minimal fix for immediate resolution
- Identifies broader architectural improvements
- Suggests preventive measures (tests, monitoring, validation)

## Output Format

Report each investigation as:

```markdown
## Bug: [one-line summary]

**Symptoms**: [what is observed — errors, timing, frequency]
**Reproduction**: [steps, or "intermittent — reproduces ~1 in N"]

### Hypotheses (ranked)
1. [most likely cause] — [why, and the test that confirms/eliminates it]
2. [next] — [...]

### Investigation
[evidence gathered: relevant code paths, log/trace findings, git changes examined]

### Root Cause
[specific code/config/data traced to; the failure chain that led to it]

### Fix
- **Immediate**: [minimal change that resolves it]
- **Preventive**: [test, monitoring, or validation to stop recurrence]
```

## Integration with Knowledge Graph

After debugging:
1. Document root cause in `knowledge/bugs/[bug-category].md`
2. Create/update anti-pattern node if applicable
3. Link to related architectural concepts
4. Tag with component, severity, and fix complexity

## Debugging Strategies

- **Binary Search**: Eliminate half the system at a time
- **Time Travel**: Bisect git history to find introducing commit
- **Inversion**: Ask "when does X work?" instead of "why does X fail?"
- **Minimal Reproduction**: Reduce to smallest test case that triggers bug

## Quick Workflow Reference

**Before debugging**: Search for similar bugs and solutions
```bash
.claude/scripts/kg-search search "bug-category" --type concept
```

**For deep research**: run `hybrid_search("<error pattern topic>")` (Weaviate MCP)

**Development env**: Python 3.12, Weaviate:8081, Ollama:11435. Activate the project's own `.venv/` (`source .venv/bin/activate`); KG/code-graph via `.claude/scripts/kg-*` and `.claude/scripts/code-graph-*` wrappers, which handle their venv internally.

## Success Metrics

This skill is working well if:
- ✅ Identifies root cause within 3-5 investigation rounds
- ✅ Fixes resolve issue permanently
- ✅ Investigation is systematic, not random trial-and-error
- ✅ Documents findings for future reference
- ✅ Proposes preventive measures

