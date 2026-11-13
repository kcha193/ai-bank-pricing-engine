"""Small deterministic checks of normalisation, bps and strategy guards."""
from harness import *  # noqa
from balance_pricing_engine.competitor_rates.normalise import normalise_term, normalise_lvr_values, normalise_lvr_condition, normalise_frequency, normalise_balance
from balance_pricing_engine.pricing_intelligence.bps import rate_change_bps, spread_bps, percent_to_bps, bps_to_percent
from balance_pricing_engine.pricing_intelligence.snapshot import term_market_from_rates, MarketSnapshot
from balance_pricing_engine.pricing_intelligence.strategy import resolve_competitor_rate, MarketStrategyError

for label in ["6 months", "1 year", "18 months", "2 years", "SIX MONTHS", "-1 year", "1.5 years", "0 days", "7 days", "30 days", "90 days", "100 days",
              "200 days", "210 days", "365 days", "1 year 6 months", "6-9 months", "6 - 9 months", "12 Months Special", "Floating"]:
    t = normalise_term(label); print(f"term {label!r:>20} -> months={t.months} days={t.days} special={t.special_label}")
for text in ["less than 20% equity", "Special rates require 20% equity", "80% LVR or less", "LVR above 80% attracts a margin", "Not available for LVR above 80%"]:
    print(f"lvr {text!r:>40} -> values={normalise_lvr_values(text)} condition={normalise_lvr_condition(text)}")
for text in ["Interest paid at maturity", "Monthly or at maturity", "Annual interest rate", "Paid annually", "6 months"]:
    print(f"freq {text!r:>28} -> {normalise_frequency(text)}")
for text in ["$5,000+", "$10,000 - $499,999", "Minimum $1,000 | Balance $5,000 to $9,999", "$500k+"]:
    print(f"balance {text!r:>45} -> {normalise_balance(text)}")
print("bps 4.99->4.79", rate_change_bps(4.99, 4.79), "| 0.1+0.2 style", rate_change_bps(0.1, 0.3), "| spread", spread_bps(4.79, 4.89), "| -5bps->pct", bps_to_percent(-5))
tm = term_market_from_rates(12, {"ASB": 4.0, "ANZ": 4.8, "BNZ": 4.9})
snap = MarketSnapshot(as_of=T0, product_type=tm.product_type, terms={12: tm})
for our in [None, "ASB", " asb ", "ASB Bank"]:
    r = resolve_competitor_rate("MARKET_MEDIAN", snap, 12, our_bank=our)
    print(f"our_bank={our!r:>12} -> median {r.rate_pct} from {r.source_banks}")
for bad in [float("nan"), float("inf")]:
    try:
        resolve_competitor_rate("MARKET_MEDIAN", snap, 12, offset_bps=bad); print("offset", bad, "ACCEPTED")
    except MarketStrategyError as e: print("offset", bad, "rejected")
