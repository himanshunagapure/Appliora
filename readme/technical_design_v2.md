# ADR-002: Generalized Apply Agent as Tier 2 Fallback

**Status**: Proposed
**Date**: 2026-08-13
**Context**: Phase 7 (Application Automation) is built and working for LinkedIn Easy Apply, Workday, and a generic-form adapter. Everything else (Greenhouse, Lever, arbitrary company career pages, and any future ATS) routes to `manual_review` — which means every new platform encountered requires writing and testing a new adapter, indefinitely.

## Decision

Add a second automation tier using `browser-use` (MIT-licensed LLM browser agent) as a generalized fallback for any job whose domain isn't matched by an existing Tier 1 adapter, instead of routing it straight to `manual_review`.

The apply cascade becomes:

```
config/ats.yaml domain match?
  ├─ yes → Tier 1: existing deterministic adapter (LinkedIn/Workday/generic)
  └─ no  → Tier 2: browser-use agent, given the tailored profile + resume,
           instructed to fill the form and stop before final submit
             ├─ succeeds → apply_pending (screenshot + review, same as Tier 1)
             └─ fails / low confidence / CAPTCHA → Tier 3: manual_review
```

## Why not rewrite Tier 1 as browser-use entirely

Considered and rejected. Tier 1's adapters are already built, tested against real LinkedIn/Workday forms, and cheaper to run than an LLM-driven agent (no per-field model call, fully deterministic). Replacing working, validated code to solve a problem it doesn't have (adapter proliferation) would be a net loss. Tier 2 exists specifically for the cases Tier 1 was never going to generalize to anyway.

## Why not Skyvern instead of browser-use

Skyvern is a strong alternative with a similar vision-grounded approach and native CAPTCHA/2FA handling — but that native handling lives mainly in the paid Cloud tier, and the self-hosted core is AGPL-3.0. We already avoided vendoring ApplyPilot's code for the same copyleft reason; introducing another AGPL dependency into the same apply pipeline would reintroduce that constraint right where we removed it. `browser-use` is MIT and gives comparable generalized form-filling without that trade-off.

## What this does NOT change

- **CAPTCHA handling stays "detect only."** Neither Tier 1 nor Tier 2 solves CAPTCHAs. A job that hits one still routes to `manual_review`. Adding real CAPTCHA-solving (e.g. a paid third-party solver) is a separate decision with its own ToS/ban-risk trade-off and is deliberately out of scope here.
- **Safety infrastructure is shared, not duplicated.** `automation/hard_stops.py`, the blocked-sites/SSO list, the dry-run submit guard, the screenshot audit, and the tailored-resume gate all wrap Tier 2 exactly as they already wrap Tier 1. Tier 2 is a new *filling* strategy, not a new *safety* model.
- **Phase 8's human review/approval gate is unchanged.** Jobs from either tier land in `apply_pending` and go through the same review step before real submission.

## Consequences

- Phase 11's planned "Greenhouse / Lever site adapters" task is superseded — Tier 2 absorbs that need generically, for those platforms and any future one.
- New operational cost: Tier 2 spends LLM tokens per apply attempt, unlike Tier 1's free deterministic fill. Needs a per-run cost guard in config.
- New testing surface: Tier 2 needs its own mocked-agent unit tests (no hitting real sites/LLMs in CI) plus a manual validation pass against real Greenhouse/Lever postings currently stuck in `manual_review`.
- Stealth (Patchright) is a separate, later task — evaluate independently before rolling into both tiers, so a regression can be attributed to the right change.
