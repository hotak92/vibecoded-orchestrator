---
name: interview
description: Interview the user using AskUserQuestion to discover requirements for a feature or task. Probe technical implementation, UX, edge cases, and constraints. Writes final spec to SPEC.md.
argument-hint: "[feature-or-task-description]"
model: sonnet
---

# /interview [feature or task]

Structured requirements discovery through interactive questioning.

## Usage

```
/interview user authentication feature
/interview redesign the task board UI
/interview                              # ask about the current task
```

## Interview Process

### Phase 1: Context (1-2 questions)

Understand the starting point:
- What exists today? What are the pain points?
- Who is the user? What's the expected load/scale?

### Phase 2: Core Requirements (2-3 questions)

Pin down must-haves:
- What does "done" look like? What's the primary success metric?
- What are the hard constraints? (performance, compatibility, deadlines)
- What integrates with this? What must NOT break?

### Phase 3: Edge Cases & UX (1-2 questions)

Surface the tricky parts:
- What happens when X fails / is empty / is at max?
- Any accessibility, i18n, or mobile requirements?

### Phase 4: Technical Preferences (1-2 questions, if unclear)

Resolve approach ambiguity:
- Any technology constraints? (must use X, can't use Y)
- Existing patterns to follow? (point to similar code)

## Rules

- **Max 4 questions per AskUserQuestion call** — don't overwhelm
- **Never repeat** a question that was answered
- **Stop** when you have enough to write an unambiguous spec
- Typical interview: 3-6 questions total across 2 rounds

## Output: SPEC.md

After the interview, write the spec to `SPEC.md` (or a path the user specifies):

```markdown
# Spec: [Feature Name]

## Goal
One sentence: what this feature achieves and for whom.

## Requirements

### Must Have
- [bullet list of hard requirements]

### Should Have
- [bullet list of important-but-flexible requirements]

### Out of Scope
- [explicit exclusions to prevent scope creep]

## Acceptance Criteria
- [ ] Given [context] when [action] then [observable outcome]
- [ ] [...]

## Technical Notes
- [Integration points, constraints, preferred patterns]
- [Edge cases and how they should be handled]

## Open Questions
- [Any remaining ambiguities that need resolution before implementation]
```

## Example Invocation Flow

```
User: /interview file upload feature
Claude: [AskUserQuestion: What file types? Max size? Where stored? Who uploads?]
User: [answers]
Claude: [AskUserQuestion: What happens on duplicate? Virus scan required? Progress indicator?]
User: [answers]
Claude: [Writes SPEC.md]
```
