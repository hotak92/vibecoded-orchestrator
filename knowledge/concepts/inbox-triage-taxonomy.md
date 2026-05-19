---
title: Multi-Channel Inbox Triage Taxonomy
type: concept
tags:
  - sales
  - marketing
  - inbox
  - triage
  - operations
  - email
  - linkedin
  - mid-level-architecture
  - b2b
  - b2c
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Multi-Channel Inbox Triage Taxonomy

A vendor running their own sales/marketing operation faces 50–200 messages per day across email, LinkedIn DMs, Instagram DMs, WhatsApp Business, and X DMs. Without a triage system, this consumes 1.5–2 hours/day. With one — and an LLM or human assistant applying it — the same volume clears in 15 minutes of review. The taxonomy below is the operational schema underneath that compression.

## The seven-class taxonomy

Every inbound message falls into exactly one of seven classes. The classes are mutually exclusive by design — a "hot lead" cannot also be "support"; pick the one that drives the next action.

| Class       | Definition                                                                       | Default action                              |
|-------------|----------------------------------------------------------------------------------|---------------------------------------------|
| HOT         | Active lead asking about pricing/demo/contract; OR existing customer with renewal/expansion signal | Draft reply same channel; high priority    |
| WARM        | Prospect engaged previously, follow-up appropriate (replied to outbound, downloaded asset, attended webinar) | Draft reply with single CTA; medium priority |
| COLD        | Cold outbound TO you (someone selling). If on-ICP for reverse pitch, flag.       | Polite decline OR reverse-pitch ask         |
| SUPPORT     | Existing customer with a problem/question                                        | Acknowledge + restate + set expectation; route to support workflow |
| SPAM        | Mass cold outbound, obviously templated, low signal                              | Archive (auto)                              |
| NEWSLETTER  | Marketing/newsletter content from a list you subscribed to                       | Archive (auto)                              |
| INTERNAL    | From your own team / contractors / vendors you pay                              | Skip triage; the vendor handles directly    |

Tag each classification with confidence: `[high]`, `[medium]`, `[low]`. Anything `[low]` routes to human review rather than auto-draft.

## Cross-channel consolidation

The same person often appears across email + LinkedIn + IG in the same week. Triage must consolidate:

- Detect the same human across channels (name + company match, or known email-to-LinkedIn mapping)
- Consolidate into a single record per person
- Reply on the channel where the most substantive message landed (usually email > LinkedIn DM > IG DM > WhatsApp > X DM)
- Don't reply on three channels — looks desperate and fragments the conversation

Output should mention cross-channel duplicates explicitly: "Maria Chen — emailed and DM'd; replying via email, more substantive thread there."

## Hard escalation triggers (always human-review, never auto-draft)

These trip immediate human-review regardless of class. The cost of auto-replying to one of these wrongly is far higher than the cost of a few extra seconds of human attention.

- **Money owed by you** — refunds, disputes, chargebacks, billing complaints
- **Press / journalist / investor / acquirer** inquiries
- **Legal-tone language** — "lawyer", "lawsuit", "GDPR", "data deletion", "complaint to [authority]", "consumer protection"
- **Commitments-asking** — message asks you to sign, commit to a deadline, exaggerate a claim, or lie
- **Languages you can't confidently reply in** — flag for human translation
- **Customer rants** > 500 words — too high-context for a draft assistant; the human reads and decides
- **SUPPORT items with sentiment ≤ 2/5** — angry customers need human empathy, not template replies
- **Unknown senders claiming to be a known person** — possible spoofing; verify before any commit

## Confidence-tagging rubric

Classification confidence isn't binary. The rubric below makes it operational:

- **High** — sender + intent + context are all clear; the reply pattern is obvious. Example: existing customer asking about adding seats.
- **Medium** — class is likely but one signal is ambiguous. Example: a "warm" lead that hasn't replied in 60 days — could be cold by now.
- **Low** — class could go two ways. Example: a 3-line LinkedIn message from a stranger that might be on-ICP outbound, might be a quiet inbound signal. → Human review.

Low-confidence items don't fail the system; they route to human review with the assistant's best guess noted. Over time, the rubric learns from the human's corrections (which is why the first 3 triage runs produce ~50% edits and runs 4+ produce ~10–20%).

## Reply-draft rules per class

### HOT / WARM
- Match the vendor's voice (length, formality, signature style) — requires voice samples to be supplied
- Reply on the same channel the message arrived on (don't migrate channels without an explicit reason)
- Keep drafts as short as the original, never longer (unless the original asked a long question)
- Include exactly ONE specific CTA — a call link, a doc, a question, a yes/no
- Use the prospect's exact language from their message where possible
- Never invent product features or pricing — leave `[VENDOR: confirm pricing for tier X]` as a placeholder when the assistant doesn't have the fact

### COLD
- Polite decline if off-ICP: one line, no explanation
- Engaged decline if on-ICP for reverse pitch: "Not buying, but we'd be a fit FOR you — quick chat?"

### SUPPORT
- Acknowledge + restate the problem in your own words (shows you read it)
- Confirm the next action (lookup, fix, escalate)
- Set an expectation (24h response, fix by Friday) — only if the vendor has a published response policy
- Flag urgent words to the human: "down", "broken", "cancel", "refund", "lawyer", "complaint"

### SPAM / NEWSLETTER / INTERNAL
- No draft. Archive or skip.

## Urgency vs importance (the second axis)

Class answers "what kind of message is this." A second axis — urgency × importance — answers "what order to handle":

```
              HIGH URGENCY     LOW URGENCY
HIGH IMPORTANCE | Do now       | Schedule today
LOW IMPORTANCE  | Delegate     | Auto-archive
```

- HOT items are typically high-importance (revenue impact) — usually high-urgency too if pricing-stage
- SUPPORT items with angry sentiment are high-importance + high-urgency
- WARM items are high-importance + medium-urgency
- COLD/SPAM/NEWSLETTER are low-importance — urgency irrelevant

A triage report that orders items by class is useful. Ordering by urgency × importance is more useful. Best practice: produce both views in the report.

## Output: the triage report

Whatever produces the triage (LLM assistant, VA, the vendor on a Sunday) should emit a single markdown file with this structure:

1. **Header**: date, total processed, time-to-review estimate, count of human-review items
2. **Review queue** (front-loaded): items requiring human judgement, with WHY flagged + a skeleton draft if any
3. **Approved drafts** (the bulk): per-item — class, channel, draft body, confidence
4. **Auto-archived**: newsletters, off-ICP cold, obvious spam (counted, not detailed)
5. **CRM updates queued**: object + action + detail (so a downstream automation can apply)
6. **Patterns observed** (15-second read): "4 inbound about the Growth plan tier — consider a public pricing page"

One file. Never multiple files. The user reviews and approves in one pass.

## Common triage mistakes

- ❌ Auto-classifying press/investor inbound as HOT (always human-review)
- ❌ Replying longer than the original message (signals you don't respect their time)
- ❌ Drafting in "default AI tone" when voice samples were skipped (the vendor rejects everything)
- ❌ Confidently inventing product features the vendor doesn't have
- ❌ Triaging 200 messages in one batch without re-anchoring on voice samples (voice drift)
- ❌ Sending the triage output as 5 files (one file, always)
- ❌ Touching the vendor's actual mailbox / CRM via tools you weren't given — produce a queue file only

## Calibration over time

First 3 runs: ~50% of drafts get edited before sending. That's the assistant learning the voice.
Runs 4–10: ~20–30% edits.
Runs 10+: <15% edits if voice samples were representative. If still high, the samples weren't representative — ask for more.

The edits themselves are the highest-value voice training data. Capture them.

## Related

- [[relatedTo::ICP and Buyer Persona Framework]]
- [[relatedTo::Copywriting Frameworks for Sales and Marketing]]
- [[relatedTo::Sales Funnel Stages and Bow-Tie Model 2026]]
- [[relatedTo::Email Deliverability 2026]]

## References

- Cal Newport — *A World Without Email* (inbox-as-task-system critique; informs the human-review pattern)
- Tiago Forte — *Building a Second Brain* (PARA classification, structurally similar)
- Practitioner playbooks: Justin Welsh, Dan Martell on inbox-zero workflows for solo founders
