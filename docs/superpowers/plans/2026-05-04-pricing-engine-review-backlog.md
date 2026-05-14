# Pricing Engine Review — Implementation Backlog

> Source: end-to-end principal-ML review, 2026-05-04. All stages A–F completed as of 2026-05-14.

---

## Out-of-scope follow-ups (parked, not backlogged)

These came up during review but are larger than a PR or depend on a product decision:

- Personalised / segmented pricing (current scope is portfolio-level by design).
- True FTP curve, tenor mismatch, basis risk in funding cost.
- Capital / RWA / Tier-1 cost-of-equity term in the objective.
- Multi-period NPV with behavioural maturity / prepayment / runoff.
- IFRS 9 staging / PD-LGD-EAD decomposition for credit loss.
- Multiple-competitor distribution rather than single scalar competitor rate.
- Smooth shadow-cost funding penalty (replace hockey-stick with quadratic or log-barrier).

Open these as design-spec tickets when business priorities require them.
