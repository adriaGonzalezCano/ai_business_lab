# Cost Model

All prices below are **illustrative placeholders, not current provider prices**. The application never hard-codes prices: the owner enters them in Settings → `model_pricing`, and a model without a configured price cannot be called.

Formula per call: `cost = input_tokens × price_in / 1e6 + output_tokens × price_out / 1e6`.

## Assumptions for the scenarios

- Placeholder "routine model" price: €1 / 1M input tokens, €5 / 1M output tokens.
- Research extraction call: 6k in / 1k out ≈ €0.011. Opportunity-scout call: 8k in / 2k out ≈ €0.018.
- Reviewer call: 3k in / 0.8k out ≈ €0.007. A committee round = 6 reviewers ≈ €0.042. Revision call ≈ €0.010.
- Average 1.7 committee rounds per evaluated idea (≈ €0.072 + revision overhead ≈ €0.08).

## Scenarios (monthly, AI only)

| Scenario | Research | Ideas evaluated | Estimated AI cost |
|---|---|---|---|
| Low | 4 runs × (5 sources) ≈ €0.29 | 8 ideas ≈ €0.64 | **≈ €1** |
| Moderate | 30 runs × (5 sources) ≈ €2.2 | 40 ideas ≈ €3.2 | **≈ €5–6** |
| High | 120 runs × (10 sources) ≈ €15.4 | 150 ideas ≈ €12 | **≈ €27–30**; ×1.5–2 if the critic and chair-summary use a stronger model |

These are screening costs only. Phase 2+ adds per-business lead-agent work, content generation and monitoring; those are modelled when implemented.

The default cap (€20/month, 70% warning, 10% reserve) therefore comfortably covers Low/Moderate usage and trips before High.

## Non-AI costs

| Item | Local | Low-cost hosted |
|---|---|---|
| Compute | €0 | ≈ €4–8/month small VPS (assumption) |
| Domain | — | ≈ €10–15/year |
| Search API (optional) | — | provider-dependent; not required for MVP |

## Controls (how overspend is prevented)

1. **Reserve before call**: worst-case cost (estimated input + `max_tokens` output) is reserved in a `BEGIN IMMEDIATE` transaction; the call is refused if reserved + spent would cross a limit.
2. **Settle after call** with provider-reported token counts; **release** on failure.
3. Limits: total monthly, per-agent monthly, per-business, per-experiment.
4. Warning at configurable % (default 70%); hard stop at 100%; configurable critical reserve (default 10%) available only to calls flagged critical.
5. Loop detection: identical prompt hash repeated ≥ N times (default 3) is blocked.
6. Response cache for repeatable extraction calls (cache hits cost €0 and are logged).
7. Bounded revision rounds and retries.

**Limitations:** the cap covers only calls routed through this application; a provider-side limit is still recommended. Estimates can differ from invoices.
