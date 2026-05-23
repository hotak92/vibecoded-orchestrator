---
name: sales-call-prep
description: Produces a one-pager prep doc for an upcoming sales call (discovery, demo, negotiation). Given a prospect's LinkedIn URL and company URL (plus optionally their last email/reply), generates company context, likely buying-committee map, expected pain points, discovery questions, objection-handling cheat sheet, and the first 60-second pitch. Use before any call where you have < 15 min to prep.
short_desc: one-pager: prospect, pains, discovery Q's, objections
keywords: [discovery call, sales prospect, buying committee, objection handling, MEDDIC, BANT, sales call]
argument-hint: "[LinkedIn URL] [company URL] [optional: last email/context]"
model: opus
effort: high
---

# /sales-call-prep

Build the prep doc you'd ideally write in 60 minutes but actually need in 10. Designed for the vendor with a call at 2pm who got the brief at 1:45pm.

## Usage

```
/sales-call-prep https://linkedin.com/in/mariachen https://acme.io
/sales-call-prep https://linkedin.com/in/jamal https://startup.co "last reply: 'we're scoping for Q3'"
/sales-call-prep                                                                # interview first
```

If invoked with no arguments, ASK:

1. Prospect's LinkedIn URL (or pasted profile text if private)
2. Company URL (or pasted About / Pricing / blog text)
3. What kind of call is this? Discovery / demo / negotiation / renewal / churn-save / partnership?
4. What does the vendor sell, in one sentence (so the cheat sheet is relevant)?
5. Anything specific they should ask or watch out for? (e.g. "they hinted at procurement involvement")
6. Last touchpoint with this prospect (email, LinkedIn DM, prior call notes)?

## What you produce

A single markdown file, ~1.5 pages, structured for a 5-minute pre-call skim:

1. **The 60-second pitch** — what the vendor should say if asked "so what does your company do?" specific to this prospect
2. **Company context** — what they do, scale, recent events (funding, hires, launches)
3. **The buyer (this person)** — role, tenure, prior companies, what they post about, likely JTBD
4. **Buying committee map** — who else is likely involved (titles + how to surface them)
5. **Likely pains** — 3–5 specific pains based on company + role (not generic)
6. **Discovery questions** — 8–12 ranked by depth (open → specific → consequence)
7. **Objection cheat sheet** — the 5–7 most likely objections + the vendor's strongest response
8. **Red flags to watch for** — signs this is not a fit, or signs the prospect is wasting your time
9. **The ask + the close** — what to actually ask for at end of call
10. **5-minute reading checklist** — the 3 things to know if you only read for 5 min

## Call-type adaptation

The brief structure changes by call type. Auto-detect from the user's answer:

- **Discovery** — heavy on pains, questions, qualification. No demo plan, no pricing.
- **Demo** — light on questions, heavy on "show this not that" based on their use case. Include a demo flow.
- **Negotiation** — heavy on procurement context, multi-thread, BATNA, deal structure. Pricing focus.
- **Renewal** — usage data summary, expansion angles, churn risk signals, the renewal ask.
- **Churn-save** — what's their actual complaint, who else is involved, what's the alternative they're considering, the rescue ask (discount, change of plan, exec involvement).
- **Partnership** — focus on strategic fit, value exchange, co-marketing potential. Discovery still but oriented to fit.

If user doesn't specify, default to **discovery**.

## Data sources you can use

Given the tools available (Read, Write, Edit, Bash + WebFetch if granted):

- **LinkedIn profile** (the user pastes content OR you fetch if WebFetch + LinkedIn-accessible)
- **Company website** (About, Pricing, Customers, Blog, Job Postings — WebFetch each)
- **Last email/reply** (the user pastes — most informative single source)
- **Past CRM notes** (user pastes from CRM if applicable)
- **Public posts** from prospect (the user pastes 2–3 recent LinkedIn/X posts)
- **Funding / hiring data** (from Crunchbase if accessible, or from the company's "About" page)

You DO NOT scrape, infer private data, or guess. If you don't have something, say "Need: [X] to be more specific."

## The "likely pain" inference

This is the high-value part. Don't list generic pains ("they probably want to grow"). Use the inputs to infer specific pains:

Given a company + a role, you can usually infer 3–5 specific likely pains by reasoning:

- "Series B + 80 employees + just hired a Head of RevOps" → likely pain: ops is firefighting, no single source of truth across HubSpot+Stripe+Mixpanel, CEO asking "how are we doing" weekly with stale data
- "DTC skincare brand + 3yr old + Shopify + just launched a new product line" → likely pain: CAC creeping post-iOS-14, need to grow LTV via subscription or repeat, attribution confusion
- "Solo founder + 2yr old SaaS + manual outbound to 100/wk" → likely pain: outbound stops scaling at one person, can't afford an SDR yet, list quality dropping

If you can't infer a specific pain, your inputs aren't rich enough. Tell the user: "Need their recent LinkedIn post or last email to specify the pain — generic pains aren't going to move the call."

## Discovery questions (the meaningful ones)

The vendor doesn't need 30 questions. They need 8–12 ranked. The framework (loosely based on SPIN, Sandler, MEDDIC):

**Situation (1–2)** — current state, scale
- "Walk me through your current [X] setup — what's working, what isn't?"
- "How many [Y] are you doing per [time period] right now?"

**Problem (2–3)** — pains, frustrations
- "What's the part of [X] that's annoying right now?"
- "When did this become a problem worth solving?"

**Implication (2–3)** — cost of inaction
- "What happens if you don't fix this in the next 6 months?"
- "Who else on your team feels this pain?"

**Need-payoff (1–2)** — what success looks like
- "If we waved a magic wand and fixed [Y], what would change?"
- "How would your job look in 90 days if this worked?"

**Qualification (2–3)** — process, budget, timing
- "Who else is involved in the decision?" (the multi-threading question)
- "What's the budget conversation look like at your stage?"
- "What's your decision timeline?"

You produce 8–12 ranked questions adapted to the specific call. The vendor uses them as a menu, not a script — picks the 5–7 they'll actually ask given how the conversation flows.

## Objection cheat sheet

The 5–7 most likely objections + the vendor's strongest response, written in the vendor's voice. Standard objections (you specialize per call):

- "Too expensive" → reframe to ROI / cost of inaction
- "Already using X" → ask what's not working, position as additive
- "Bad timing" → ask when, set a follow-up plan
- "Need to discuss with team" → ask who, offer to help build the case
- "Send me materials, I'll review" → counter with "what specifically would help you decide? — would 15 min on screen-share answer that faster than reading?"
- "We built this in-house" → ask about maintenance cost, opportunity cost
- "Just researching" → qualify: real research or polite no? — "totally fine — what would make this worth a follow-up in 30 days?"

For each objection, give the vendor:
- The trigger phrase to listen for
- A 1–2 sentence response
- A follow-up question to keep the conversation alive (not let the prospect close the loop on "no")

## Red flags

Sometimes a call is a waste. Help the vendor spot it early:

- **No clear pain** — they're researching, not buying
- **No process owner on the call** — wrong person, multi-thread needed
- **"Send me pricing and I'll get back" before any value discovery** — usually a polite no
- **"We need a custom proposal" without budget signal** — fishing for ideas
- **Long silences, distracted, takes another call** — not their priority
- **No follow-up question on what you say** — not actually engaged
- **Promised demo to a junior — junior won't introduce decision-maker** — sales-cycle stall

For each, give the vendor a graceful out: how to disqualify politely and free the calendar.

## The ask + the close

Every call should end with a specific next step proposed BY the vendor. Provide 2–3 graduated asks:

- **Best case ask**: "Want to set up the next call with you + [their boss] in the next 10 days?"
- **Solid case ask**: "I'll send a 1-pager summarising what we discussed — when can we reconnect to walk through it?"
- **Worst case ask**: "Let's set a follow-up in 30 days — by then you'll know if you want to do anything here."

NEVER end on "let us know!" or "shoot me an email when you have a sec." Always propose a specific next step with a specific timeframe.

## Output format

Write to `call-prep-{prospect_first_name}-{YYYY-MM-DD}.md`:

```markdown
# Call Prep — Maria Chen @ Acme Inc — 2026-05-21 14:00

**Call type**: Discovery
**Time**: 30 min
**Vendor's product (one-liner)**: Revenue attribution for B2B SaaS

---

## TL;DR — read this if you have 90 seconds

- Maria's been Head of Marketing Ops at Acme (Series B, ~80 employees) for 5 months — typically the time when ops leads tear out broken HubSpot+Mixpanel+Zapier stacks
- Likely pain: stitched attribution between HubSpot + Stripe + Mixpanel, weekly "where's the pipeline?" from CMO
- Buying committee: Maria (likely champion), CMO (her boss), CFO/finance (budget gate), VP Eng (integration)
- The ask: 30-min call with her + CMO next week
- One thing not to do: don't lead with features. She's seen 20 demos. Lead with the post-mortem story.

---

## The 60-second pitch (for "what does your company do?")

> We solve the attribution mess for Series-A-to-C B2B SaaS. The story most ops leads tell us is the same: HubSpot says one number, Stripe says another, Mixpanel says a third, and the CFO can't reconcile. We connect those data sources in ~20 minutes of setup and produce a single attribution view that doesn't break when one of the tools changes. We've done this for 47 SaaS teams in the $5M–$50M ARR range.

(Maria works at $14M ARR Series B — squarely the customer.)

---

## Company context — Acme Inc

- **What they do**: PLG SaaS for engineering teams (CI/CD, ~80 employees, $14M ARR per their CEO's Q4 post)
- **Recent events** (from website + LinkedIn):
  - Closed Series B ($28M) in Jan 2026
  - Hired Maria (5 mo ago) — her LinkedIn post when hired mentioned "untangling the data stack"
  - Just shipped a new pricing page (Mar 2026) — suggests positioning rework in progress
- **Stack** (inferred from website Tech Stack badges + Maria's LinkedIn tools list):
  - HubSpot CRM (Pro tier likely)
  - Mixpanel (product analytics)
  - Stripe (billing)
  - Customer.io (lifecycle email)
  - Zapier (gluing it together)

This is exactly the stack we sell into.

---

## The buyer — Maria Chen

- **Role**: Head of Marketing Operations, 5 mo tenure
- **Prior**: Marketing Ops at Notion (2 yrs), Marketing Analytics at Klaviyo (3 yrs) — strong technical-ops background
- **Recent posts** (2 in last 30 days):
  - LinkedIn: "Spent 6 hours this week reconciling Stripe + HubSpot. There has to be a better way." (engagement signal — pain articulated PUBLICLY)
  - X: Bookmarked Kyle Poyar's PLG attribution thread
- **Likely JTBD**: "Prove revenue ROI to my CMO without spending 10h/week stitching tools"
- **Persona match**: Marketing Ops Lead at PLG SaaS (our primary ICP — see knowledge/concepts/icp-and-buyer-persona-framework.md)

---

## Buying committee — multi-thread these

| Person                       | Role           | Influence       | How to surface       |
|------------------------------|----------------|-----------------|----------------------|
| Maria Chen                   | Head of Mktg Ops | Champion       | Already on the call  |
| [Probable] Sarah Lee         | CMO            | Decision maker | Ask Maria — "Who'd I be presenting to alongside you?" |
| [Probable] David Park        | CFO            | Budget gate    | Surface later — "When does finance get involved at Acme?" |
| [Maybe] VP Eng               | Integration    | Sign-off       | Surface during demo — "How does your eng team usually evaluate vendors?" |

**Multi-thread ask** (built into discovery): "If this goes anywhere, who else would be in the decision?"

---

## Likely pains (3 ranked — most likely first)

1. **Attribution chaos** — pipeline numbers don't match across tools, weekly fire drill from CMO. Maria posted about this publicly. HIGH confidence.
2. **Maria is the bottleneck** — solo MO at 80-person company is a known stall point. Tools work, but only Maria knows how they work. Series B = expectation to scale ops without scaling team. MEDIUM-HIGH confidence.
3. **CMO under board pressure** — Series B board expects revenue clarity. CMO needs to walk in with numbers. Maria is the one producing them. MEDIUM confidence.

Don't lead with #3 (too speculative). Lead with #1, surface #2 in implication questions.

---

## Discovery questions (8 ranked)

1. **Open**: "5 months in — what's the part of your job that's burning the most time right now?" (let Maria define the pain)
2. **Specific (situation)**: "Walk me through how you produce the revenue attribution report your CMO sees each week."
3. **Specific (problem)**: "What's the question your CMO asks that you don't have a clean answer for?"
4. **Implication**: "When the numbers don't match across tools, what's the workflow to reconcile? How long does it take?"
5. **Implication**: "Who else on the team is impacted when this breaks?"
6. **Need-payoff**: "If you walked into Monday and the report was already done — what would you do with the extra time?"
7. **Qualification (process)**: "If we showed you something that fixes this in setup, who'd be in the conversation with us?"
8. **Qualification (timeline)**: "What's the realistic timeline for adding new tools at Acme?"

If she opens up early on #1 + #2, skip ahead. Don't run all 8.

---

## Objection cheat sheet

| Likely objection                        | Response (vendor voice)                                                                            | Follow-up Q |
|----------------------------------------|----------------------------------------------------------------------------------------------------|-------------|
| "We can build this in-house"           | "Most teams I talk to could — the question is opportunity cost. Your VP Eng would build this in 3–6 weeks; that's 1 sprint of product not shipped. We do it in 20 min." | "What's the engineering team's roadmap pressure right now?" |
| "Already using [Mixpanel attribution]" | "Mixpanel sees product, not revenue. The attribution gap is Mixpanel ↔ Stripe ↔ HubSpot. That's where we live." | "When was the last time the Mixpanel report matched the Stripe report exactly?" |
| "Too expensive"                        | "Our price is $X/mo. The cost of NOT having clean attribution is one missed forecast at board prep. What's that cost Acme?" | "What's the budget envelope for marketing ops tooling this year?" |
| "Need to think about it"               | "Totally — what specifically would help you decide? Want me to put together a 1-pager just for your stack?" | "Can we set the follow-up while we're here?" |
| "Send me materials"                    | "Will do — but I find a 15-min screen-share answers things 10x faster than a PDF. Want to do that instead?" | (silence — let her answer) |

---

## Red flags (watch for these in the first 10 min)

- Maria doesn't engage on the attribution-chaos question → maybe NOT her pain (we guessed wrong; pivot)
- "We're not really in the market right now, just researching" said EARLY → polite no incoming; qualify hard
- No mention of CMO or other stakeholders → may not have authority to buy
- "Just give me pricing" before any discovery → fishing

If 2+ red flags: end the call early with a friendly "Sounds like we should reconnect in 30 days when this is more urgent — does that work?"

---

## The ask + the close

**Best case**: "Want to set up the next call with you + [CMO's name] in the next 10 days? I'll prep the attribution view specific to your stack."

**Solid case**: "Let me send a 1-pager summarising what we covered + a 5-min Loom walking through what the integration would look like at Acme. When can we reconnect to walk through it?"

**Worst case**: "I'll put a 30-day follow-up on the calendar — by then you'll know if attribution is something you want to tackle this quarter."

---

## After-call doc — 5-minute todo for the vendor

Within 24 hours, send Maria:
1. The Loom (5 min) of attribution-view-specific-to-her-stack
2. One specific reference customer (Series B PLG SaaS, similar profile) — names + 2-line outcome
3. The follow-up calendar link

Update CRM:
- Stage: Discovery → Demo Scheduled (if positive)
- Champion: Maria Chen
- Pain: Attribution chaos, weekly fire drill
- Next step + date

---

## Knowledge used to build this brief

- ICP framework: knowledge/concepts/icp-and-buyer-persona-framework.md (Marketing Ops Lead at PLG SaaS persona)
- Funnel stages: knowledge/concepts/sales-funnel-stages-2026.md (Discovery stage)
- Sources: Maria's LinkedIn posts (paste #1, paste #2), Acme website (About + Pricing pages), Q1 earnings post by Acme CEO

```

## Workflow

1. **Confirm inputs** — LinkedIn URL, company URL, call type, last touchpoint. If user pasted the LinkedIn/website content, even better.

2. **Pull data**:
   - If you have WebFetch and the URLs are public, fetch them
   - If not, ask the user to paste 3 things: LinkedIn About + 2 recent posts, Company About + Pricing, last email/reply

3. **Search the KG** for context:
   - `hybrid_search("ICP buyer persona")` — match the prospect to the vendor's documented ICPs
   - `hybrid_search("sales funnel stages")` — confirm the call stage maps to right discovery focus
   - `hybrid_search("copywriting frameworks")` — the 60-sec pitch should follow a framework

4. **Build the buying committee map** from job postings (often public on the company site), LinkedIn (search "people at [company]"), and inference.

5. **Infer the 3–5 specific pains** from company + role + recent signals. If you can only do generic pains, ASK the user for more context.

6. **Generate discovery questions** ranked from open → specific → consequence → qualification.

7. **Build the objection cheat sheet** — pick the 5–7 most likely for this call type and prospect.

8. **Spot red flags** — write 3–5 specific to this call.

9. **Write the ask + the close** in 3 graduated tiers.

10. **Write the file** — one markdown file, `call-prep-{prospect_first_name}-{YYYY-MM-DD}.md`.

11. **Report back** — file path + the one thing the vendor should NOT do on this call (top mistake risk). Don't dump the brief into your reply.

## What this skill is NOT

- Not a CRM updater. You produce the brief; the vendor updates the CRM.
- Not a closer. You don't promise outcomes; you give the vendor better odds.
- Not a stalker. You use public info only (LinkedIn About, public posts, company website). You don't infer personal info, family, etc.
- Not a scheduler. The vendor manages their own calendar.

## Common mistakes

- ❌ Generic pains ("they probably want to grow revenue")
- ❌ Generic questions ("what's keeping you up at night?")
- ❌ Listing every possible objection (give 5–7 most likely, not 20 hypothetical)
- ❌ No multi-thread ask in the discovery questions
- ❌ No specific next step in the close
- ❌ Speculating about personal info ("she has 2 kids based on Facebook" — no, don't)
- ❌ Forgetting the TL;DR for the vendor who has 90 seconds before the call

## Knowledge graph access

Search before writing:
- `hybrid_search("ICP buyer persona")` — map the prospect to a documented persona
- `hybrid_search("sales funnel stages")` — match discovery questions to the call's stage
- `hybrid_search("copywriting frameworks")` — for the 60-sec pitch shape
- `hybrid_search("CRM RevOps stack 2026")` — to identify the vendor's likely tools and infer points of pain in their stack

## Success criteria

You succeed when:
- The vendor reads the brief in 5 min and walks into the call confidently
- The "likely pain" is specific enough that surfacing it in the first 10 min visibly lands with the prospect
- Discovery questions get used (not just listed)
- Objection cheat sheet has the actual objection they hear, not a hypothetical
- The vendor knows the specific next step to propose
- Post-call, the vendor says "the brief was right about pain #1"
