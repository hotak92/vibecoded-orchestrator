---
title: GDPR and EU VAT Lite for Solo Founders
type: concept
tags: [saas, compliance, gdpr, vat, business, founder, mid-level-architecture, active]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# GDPR and EU VAT Lite for Solo Founders

## Why "Lite" and Why You Should Not Ignore This

This note is calibrated to a solo founder running a self-serve SaaS with <$50K MRR and EU customers. It is **not** legal advice — it's an operational checklist that gets you to "defensible if questioned," not "audit-proof for a Fortune 500 customer."

The reason to take this seriously even at small scale:
- **GDPR fines** can be up to €20M or 4% of global annual turnover, whichever is higher. In practice, regulators don't chase indies — but they do escalate on complaint, and one disgruntled EU customer can file a complaint with their national DPA for free.
- **EU VAT** on digital services has been mandatory for **any** seller (no threshold) since 2015 for B2C sales. Most indies don't know this.
- **B2B customers** ask for a Data Processing Agreement (DPA) by month 3. Not having one in 24 hours costs you the deal.

The whole bundle is tractable in ~1 weekend if you start before you need it.

## The 5-Item GDPR Checklist (Defensible-Indie Edition)

### 1. Privacy Policy that names actual processors

A generic privacy policy is worse than none — it signals you copy-pasted. The minimum content:

- **What data you collect** (email, IP for security, payment info via processor)
- **Why** (legal basis: usually contract performance for paid customers, legitimate interest for security/analytics)
- **Who you share it with** — name every sub-processor: Stripe/Lemon, your email provider (Resend/Postmark/Mailgun), analytics (PostHog/Plausible), error tracking (Sentry), hosting (Vercel/Fly/Hetzner)
- **How long you keep it** (be specific: "30 days post-deletion in backups")
- **User rights**: access, deletion, portability, objection — with a single email address to use
- **Contact**: your name (or a DPO if hired) and an email

Template generators (Termly, Iubenda, Termsfeed) get you 80% there. Spend 30 minutes editing — the sub-processor list MUST be yours, not the template's.

### 2. Cookie consent (only if you actually use tracking cookies)

If you only use a session cookie + payment cookies = strictly necessary = **no banner needed.** If you load Google Analytics, Meta Pixel, or any non-functional script = you need consent before loading them (not just a banner — actual blocking until consent).

The 2026 trend is **server-side analytics + first-party cookies** (PostHog self-hosted, Plausible, Fathom) which usually don't require a banner because they're privacy-preserving. This is the cheapest compliance path.

### 3. Data Processing Agreement (DPA) ready to send

A one-page DPA covering: what you process, on whose behalf, security measures, sub-processors, breach notification, deletion on termination. The European Commission publishes standard contractual clauses (SCCs) — annex them.

Pre-build this and email it within 24 hours when a B2B customer asks. Bonus: link to it from the bottom of your pricing page ("DPA available on request") — signals "ready for B2B" without being noisy.

### 4. Delete-my-account that actually deletes

GDPR's "right to erasure" requires actual deletion within 30 days, including backups (within reasonable cycles). Indie reality:

- Have a self-serve delete button in your account settings
- Delete from primary DB immediately
- Document that backups are deleted on rotation (typical 30 days)
- Soft-delete with a 30-day grace is fine — but it must be truly purged after that
- Anonymise analytics events (hash the user_id) rather than trying to delete from PostHog/Plausible

### 5. Breach notification process

If you suspect a breach involving EU personal data, you have **72 hours** to notify the relevant DPA. You need:

- A way to detect a breach (Sentry alert + db audit log is fine)
- A template breach-notification email (one page) ready to send
- A list of which DPA you notify (usually your country of establishment; if multi-country complex, get a lawyer)

## EU VAT in 60 Seconds

If you sell **digital services to EU consumers** (B2C), VAT is owed in the **customer's country** at the customer's country rate (17–27%), regardless of where you live.

You have three real options:

1. **Register for OSS (One-Stop Shop)** — file one quarterly VAT return that covers all 27 EU countries. You collect VAT at the customer's local rate, remit it via OSS. **Pros**: one form, one filing. **Cons**: you still need to determine each customer's country and apply the right rate. Requires a real accounting setup.

2. **Use a Merchant of Record (MoR)** — Lemon Squeezy / Polar / Paddle / FastSpring become the legal seller. They handle VAT entirely. **Pros**: zero VAT work. **Cons**: 2% fee premium over direct Stripe. See `[[Merchant of Record vs Stripe for Indies]]`.

3. **Just don't sell to the EU** — geo-block at checkout. **Pros**: zero compliance. **Cons**: kills ~25% of typical SaaS TAM.

For solo founders pre-$50K MRR, **option 2 (MoR) wins almost universally** because the time cost of OSS quarterly filings exceeds the 2% fee premium.

### B2B: Reverse Charge

If your customer provides a valid VAT number, the **reverse charge** applies — you don't collect VAT, the customer self-accounts in their country. You still need to:
- Validate the VAT number against VIES (https://ec.europa.eu/taxation_customs/vies/)
- Include their VAT number on the invoice
- Note "Reverse charge — VAT to be accounted for by the customer"

If you use Stripe Tax or any MoR, this is automatic.

## The "Lite" Reality Check

The above is what's **legally required**. Reality:

- The chance of an EU DPA chasing a $5K-MRR solo founder is low absent a complaint
- The chance of a B2B customer asking for the DPA is high (and the deal dies without one)
- The chance of an EU tax authority chasing you is low at <$50K, escalates above

So the priority order is:
1. **DPA template + privacy policy** — gates B2B deals; do this immediately
2. **Use a MoR** if EU customers > 10% of revenue — solves VAT properly
3. **Privacy-preserving analytics** (Plausible/PostHog) — avoids cookie-banner work
4. **Delete-account flow that works** — required for free-tier compliance complaints
5. **Breach process + cookie banner if needed** — defensible position

## US Equivalents (Brief)

- **CCPA / CPRA** (California): similar to GDPR; required if you have >$25M revenue OR process data of >100K California residents, OR derive >50% revenue from selling personal info. Most indie SaaS is **out of scope**, but adding a "Do Not Sell My Info" link costs nothing.
- **HIPAA** (health data): not "lite" — if you touch health data you need a BAA-signed processor stack. Most indies should avoid this category entirely until they can afford a lawyer.
- **State-level laws** (Virginia, Colorado, Connecticut, Utah, ...): a patchwork emerging since 2023; following GDPR-level practices usually covers them.

## Related

- `[[relatedTo::Merchant of Record vs Stripe for Indies]]`
- `[[relatedTo::SaaS Pricing Psychology for Solo Founders]]`

## Sources

- GDPR official text — Article 83 (fines), Article 33 (breach notification 72h), Article 17 (erasure). Reference: https://gdpr.eu/
- EU VAT OSS portal — https://taxation-customs.ec.europa.eu/online-services/online-services-and-databases-taxation/oss-one-stop-shop_en
- European Commission Standard Contractual Clauses (SCC) for international transfers, Commission Decision 2021/914
- CCPA / CPRA — https://oag.ca.gov/privacy/ccpa

**Important**: this is operational guidance, not legal advice. For B2B SaaS contracts above ~$25K ARR per customer, have a real lawyer review your DPA + ToS.
