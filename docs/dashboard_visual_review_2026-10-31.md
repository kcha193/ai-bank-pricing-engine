# Pricing Watch visual review — 31 October 2026

The recovered six-destination dashboard has been rendered in Chromium using disposable
synthetic data. Both markets retain their own observations, rankings, freshness and scenarios.
No live bank collection or notification was run. The operational database was not used.

## Evidence

| Check | Result |
| --- | --- |
| Baseline dashboard tests | 523 passed |
| Final full suite, `python3 -m pytest -q -m 'not live_rates'` | 2,178 passed, 2 skipped; 127 dependency/model warnings |
| `python3 -m ruff check .` | Passed |
| Browser page matrix | Six destinations × four widths × two themes = 48 page renders |
| Additional chart views | Four chart destinations × four widths × two themes = 32 renders |
| Error/empty fixtures | Five renders: partial coverage, absent deposits, all-stale deposits, absent DB, corrupt DB |
| Total recorded render checks | 85 passed; no main-page horizontal overflow or uncaught page error |
| Additional interactions | Product navigation, term/bank filters, returning navigation, keyboard focus/Enter, mobile navigation, reduced motion, deposit/joint scenarios and locked Operations passed |

The final full suite ran after the chart-renderer and header fixes. A later browser interaction
pass also captured deposit results at 390px and the financial-value chart. The
[render manifest](images/premium_dashboard/render-report.json) records viewport/overflow
measurements; 14 representative screenshots are retained below. Run
`tests/visual/dashboard_review.py` to regenerate the complete set.

## Screenshots inspected

All figures show synthetic fixture observations, not current advertised rates.

| View | Evidence |
| --- | --- |
| Command Centre, desktop | [1440px light](images/premium_dashboard/command-centre-1440-light.jpeg) |
| Command Centre, laptop | [1024px light](images/premium_dashboard/command-centre-1024-light.jpeg) |
| Command Centre, tablet | [768px dark](images/premium_dashboard/command-centre-768-dark.jpeg) |
| Market Explorer, mobile | [390px light](images/premium_dashboard/market-explorer-390-light.jpeg) |
| Deposit ranking, mobile/dark | [390px chart](images/premium_dashboard/market-explorer-chart-390-dark.jpeg) |
| Rate Movements | [Dark timeline](images/premium_dashboard/rate-movements-chart-1440-dark.jpeg) |
| Market Trends | [Bank comparison](images/premium_dashboard/market-trends-chart-1440-light.jpeg) |
| Pricing Studio | [Deposit results](images/premium_dashboard/pricing-studio-deposit-results.jpeg), [390px results](images/premium_dashboard/pricing-studio-deposit-results-mobile.jpeg), [financial value](images/premium_dashboard/pricing-studio-financial-value.jpeg) |
| Operations & Trust | [Deposit health](images/premium_dashboard/operations-deposit-health.jpeg), [mobile dark freshness](images/premium_dashboard/operations-chart-390-dark.jpeg) |
| Keyboard focus | [Product control](images/premium_dashboard/keyboard-product-focus.jpeg) |
| Missing deposits | [Honest empty state](images/premium_dashboard/state-no-deposits.jpeg) |

## Review findings and corrections

Actual screenshot inspection and an independent code review identified black text marks on
dark charts and a crowded mobile brand header. `ui.render_chart` now applies the active native
theme's text colour to every chart. Data marks use a readable mid-tone blue instead of the
branding blue. The header uses a native **Market data status** disclosure, which retains both
products' activity/freshness and lets native columns stack on mobile. The corrected ranking,
movement and freshness labels were visually inspected in dark screenshots.

The browser checks now exercise navigation back to a previously visited page, verify the latest
shared term/bank selection, switch products with Enter, check the visible focus outline and
44px product-control targets, open the mobile navigation and verify reduced-motion styles.
Missing deposit fixtures display no loan-rate metrics. Deposit and joint scenario results are
only shown after their explicit Run action; switching modes hides the other mode's result.
Operations remained locked throughout browser checks. Existing unit tests also verify the
unguarded/local control workflow, token checks, single-flight runner and cooldowns.

## Limits

- Chromium was exercised; Safari/Firefox and a physical touch device were not tested.
- Keyboard/focus, responsive layout, readable theme text and data alternatives were checked;
  this is not a formal WCAG audit or a screen-reader certification.
- Wide native tables intentionally scroll within their own region on narrow screens.
- The master font pair uses local system fallbacks when Fira is unavailable.
- Live bank fetching and external alerts were excluded from this presentation task. The
  existing term-deposit collector fixes were covered by the offline regression suite.
- No pricing assumptions or governance policy changed. Results remain synthetic,
  portfolio-level decision support requiring human review.
