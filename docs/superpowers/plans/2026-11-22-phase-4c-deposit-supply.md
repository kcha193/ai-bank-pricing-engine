# Phase 4c deposit supply implementation plan

**Goal** Bound the NZD available to fund simulated annual pricing while preserving the canonical baseline.

**Design** Keep market capacity in a validated simulation profile. Normalise synthetic balances within disjoint opportunity pools. Precompute gross bookings, eligible additions, interest deltas and acquisition costs per offered rate. Combine tiers before applying the opening balance, concentration policy, wholesale cost and funding requirement.

1. Write failing profile, weighting and hand-calculated stock/flow tests. Implement and recheck the economic contracts.
2. Write failing simulation tests for objective reconciliation, joint and segmented parity, small-grid enumeration, governance and immutable lineage. Route optional deposit economics through both existing solvers and the separate simulation entry point.
3. Compare legacy and fixed-capacity runs at matched offers and after reoptimisation. Vary sample count, capacity, share, conversion, retention, migration, concentration and the funding ratio. Record independent deposit-sample variation.
4. Document the evidence matrix, calibration gaps and single-horizon boundary. Check the existing cohort regressions and reproduce the archived baseline.
5. Review the diff independently, run Ruff and both full offline test commands, then publish one PR without merging.

Pricing uses deposit_amount as a raw eligibility and response feature. The legacy optimiser also multiplies it by uptake to compute booked balances and interest expense. Wholesale cost, surplus income and the management shortfall penalty all consume that same gross balance. The simulation retains the raw feature, evaluates gross booking and source interest separately, and passes only active usable funding to those three portfolio calculations. The annual horizon has no automatic renewal into a second period.
