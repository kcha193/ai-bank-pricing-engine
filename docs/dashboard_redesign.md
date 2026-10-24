# Pricing Watch dashboard redesign (2026-10)

A UX/UI redesign of the Pricing Watch Streamlit console into six task-oriented destinations,
a shared design system and chart library, and cross-page filters. **No calculation moved**:
every number still comes from `DashboardServices`, and every safety boundary (explicit-only
runs, Operations gating, untrusted-text handling, stale/quarantine rules, scenario
invalidation) is unchanged and still tested.

User-facing behaviour: [pricing_intelligence.md → Dashboard](pricing_intelligence.md#dashboard).
Architecture: [architecture.md → Pricing Watch dashboard](architecture.md#pricing-watch-dashboard).

## 1. Audit summary (before)

| Area | Finding |
| --- | --- |
| Navigation | Seven sidebar pages named after data (Overview, Market Watch, System Health …), not after tasks. Operations and System Health split one concern across two pages. |
| Landing page | Five activity counters, then a bank × term table, then a term selector, then seven more metrics and a ranking table. No answer to "where do we sit?" or "can I trust this?" without reading every table. A quiet market rendered as a row of zeros. |
| Tables first | Market Watch, Rate Changes, System Health and Scenarios led with dataframes; the one chart per page came after. |
| Filters | Every page had its own term selector; a choice on one page was lost on the next. The highlighted bank existed only on Market Watch. |
| Charts | Default Altair styling, tiny labels, a dot plot with no rate labels; a selected leader lost its leader colour; market median / leader / spread shared one chart behind a radio toggle. |
| Scenarios | Raw enum names (`MARKET_SECOND_LOWEST`), a table as the primary output, the model-optimum alias explained only in a caption. Governance flags computed by the optimiser were not shown. |
| Data trust | Collection failure, staleness and quarantine were explained in System Health only; Rate Changes did not say when a bank could not be collected. |
| Deep links | A `pages/` folder beside `app.py` made Streamlit start in legacy multipage mode: opening `/trends` before anyone opened `/` (e.g. after a restart) rendered a blank module, and other paths showed "Page not found". Present on `master`. |

What was already right and is preserved: the `DashboardServices` read model, `Result`
empty/error states, the boundary tests, explicit-only collection behind the token gate,
scenario provenance (inputs, DB stamp, config fingerprint), live-term selectors, and
Decimal basis points.

## 2. Information architecture

| Destination | Answers | Replaces | URL (old URL redirects) |
| --- | --- | --- | --- |
| **Command Centre** | What is the market doing, what changed, where do we sit, can we trust it? | Overview | `/command-centre` (`/overview`) |
| **Market Explorer** | Who leads, by how much, at which term? | Market Watch + Overview matrix/ranking | `/market-explorer` (`/market-watch`) |
| **Rate Movements** | Who repriced, by how much, and what did it do to the ranking? | Rate Changes | `/rate-movements` (`/rate-changes`) |
| **Market Trends** | How have rates, the leader, median and dispersion moved? | Historical Trends | `/market-trends` (`/trends`) |
| **Pricing Studio** | What does the optimiser say under each competitor assumption? | Pricing Scenarios + Scenario Lab | `/pricing-studio` (`/scenarios`) |
| **Operations & Trust** | Is the data fresh and complete; how do I collect more? | Operations + System Health | `/operations` (`/system-health`) |

Navigation is a top bar (no sidebar). The **term** and **highlighted bank** follow the
user between pages; `pricing_intelligence.our_bank` is the default highlight when set.

### Primary journeys

1. *Morning check*: Command Centre → (event in feed) → Rate Movements inspector → official source.
2. *Competitive position*: Command Centre (highlight our bank) → Market Explorer (same term
   and bank pre-selected) → Gap analysis / Term structure → Market Trends for history.
3. *Response analysis*: Rate Movements → Pricing Studio (define, run, trade-offs) → Advanced
   Scenario Lab for sensitivity.
4. *Trust check*: header coverage badge or Data trust panel → Operations & Trust → Data
   health → Collection & maintenance (behind the existing gate) → Refresh.

## 3. Screen specification (as built)

**Header (every page)** — product identity; activity badge (`Quiet market` … `High
activity`); coverage badge (`All 5 banks fresh` / `⚠ 4 of 5 banks fresh`); last successful
update in Pacific/Auckland; market activity state; fresh-bank count.

**Command Centre** — filter bar (term, highlight bank) → *Market pulse* (market leader,
leader rate, median, dispersion bps, banks repricing in 7 days, fresh coverage, activity
rationale) → *Market story* (lollipop from median to each bank, leader/selected emphasis,
rate labels; side panel with the highlighted bank's rate, rank, gaps) → *What changed* (last
7 days as event cards, or a calm empty state) beside *Key insights* (2–4 sentences from
`insights.py`) and *Data trust* (one warning per stale bank) → collapsed full matrix → next
steps.

**Market Explorer** — filter bar → highlighted-bank cards (current, rank, vs leader, vs
median, freshness / `⚠ QUARANTINED`) → tabs: Ranking (chart + full table), Gap analysis
(grouped bps bars), Bank × term matrix (shaded, legend), Term structure (bank multiselect)
→ highlighted bank in detail (its row across terms, its last 30 days of changes).

**Rate Movements** — filter bar (window, bank, term, direction, event type) → coverage
notice when a collector is failing or a bank is not fresh → counts by event type → timeline
(lane per bank, colour + shape by direction, size by |bps|) → event feed beside the event
inspector (previous/new rate, movement, rank before → after, provenance table, official
source link) → *Advanced: full event table*.

**Market Trends** — filter bar (term, banks, range, highlight) → rate history (step lines,
gaps not bridged, highlighted bank emphasised) → leader & median (%) beside dispersion (bps)
→ repricing timeline (table on demand) → today's term structure.

**Pricing Studio** — disclaimer → tabs: *Guided scenario* (1 · Define with input guide;
Run scenario; 2 · Results cards, governance notices, comparison dumbbell + table; 3 ·
Trade-offs objective curve with markers, differences vs Match median, decision-support
summary, limitations) and *Advanced: Scenario Lab* (unchanged behaviour).

**Operations & Trust** — tabs: *Data health* (status tiles, freshness-by-bank chart with
the limit, data-state key, components, per-product collector/run/suspicious tables) and
*Collection & maintenance* (the existing Operations page, unchanged).

## 4. Service → component map

| Component (where) | Data (service method) | New service work |
| --- | --- | --- |
| Header badges/captions (`views/_common.render_header`) | `header()` | — |
| Market pulse cards | `overview()`, `term_summary(term)` | — |
| Market story / Ranking chart (`charts.position_dot_plot`) | `bank_positions(term)`, `term_summary(term)` | — |
| Your-position panel, Explorer bank cards | `bank_positions(term)` | — |
| Event feed / cards (`ui.movement_feed`) | `changes(ChangeWindow, ChangeFilters)` | — |
| Key insights (`insights.build_insights`) | `overview`, `term_summary`, `bank_positions` | — |
| Data-trust warnings (`views/_common.exclusion_messages`) | `overview().exclusions` | — |
| Gap analysis (`charts.gap_bars`) | `bank_positions(term)` (backend bps) | — |
| Matrix (`views/_common.styled_matrix`) | `overview().matrix` | — |
| Term structure (`charts.term_structure`) | `overview().matrix` | — |
| Movement timeline (`charts.movement_timeline`) | `changes(...)` | — |
| Coverage notice (Rate Movements) | `health()` (uncached) | — |
| Event inspector | `changes(...)`, `event_detail(key)` | — |
| History / leader & median / dispersion / repricing timeline | `history(term, banks, days)` | — |
| Scenario cards, comparison, curve | `scenarios(request)` | `SelectedScenario.boundary_warning`, `.policy_band_breached` (additive, read from the optimiser's own diagnostics) |
| Scenario differences (`scenario_text.scenario_differences`) | `scenarios(request)` | — (Decimal bps via `pricing_intelligence.bps`) |
| Health tiles, freshness chart (`charts.freshness_bars`) | `health()`, `suspicious()` | — |

## 5. Design system

- **Theme** — [`.streamlit/config.toml`](../.streamlit/config.toml): navy/blue/teal on a cool
  neutral background, separate `[theme.light]` and `[theme.dark]` palettes, 0.6 rem radius,
  categorical chart colours.
- **Tokens** — [`dashboard/theme.py`](../src/balance_pricing_engine/dashboard/theme.py)
  (Streamlit-free): palette, bank colours (stable per configured bank order), role colours
  (leader teal, highlighted blue, others muted), direction colours *and symbols*
  (▲ increase, ▼ decrease, ＋ new, － withdrawn, • other), market-state badges, chart sizes,
  and scoped CSS targeting `data-testid` hooks and `st-key-pw-*` container keys only.
- **Components** — [`dashboard/ui.py`](../src/balance_pricing_engine/dashboard/ui.py):
  `page_header`, `section`, `filter_bar`, `calm_empty_state`, `market_state_badge`,
  `coverage_badge`, `shared_selectbox` / `bank_selector` (cross-page state), `nav_link`,
  `movement_card` / `movement_feed`.
- **Chart conventions** ([`charts.py`](../src/balance_pricing_engine/dashboard/charts.py)):
  rates in % on a non-zero axis, bps on a zero-based axis, never both on one axis; 12 px
  labels; day-granular time axes with overlap thinning; text labels on ranking marks;
  tooltips everywhere; step interpolation for rates; gaps instead of bridged lines; colour
  always paired with a symbol, word or position.
- **Semantics** — rate rises and cuts use a neutral orange/blue pair, not good/bad red/green:
  a competitor's cut is good for borrowers and a threat to a lender.

## 6. Before / after feature coverage

| Capability (before) | Where now | Change |
| --- | --- | --- |
| Header: last update, activity, fresh banks | Header | + activity and coverage badges |
| Changes today / 7 d, banks repricing, direction, most active term, rationale | Command Centre pulse + insights; Rate Movements counts | "Most active term" and "changes today" dropped from the landing page (available via Rate Movements window/term filters) |
| Bank × term matrix with ◆ / ⚠ markers | Explorer tab; Command Centre expander | + conditional shading |
| Exclusion warnings | Command Centre Data trust; Explorer matrix; Pricing Studio; Data health | grouped per bank on Command Centre |
| Term summary: leader, lowest, median, highest, spread, banks | Command Centre pulse + panel; Explorer caption | — |
| Ranking table with ties | Explorer Ranking tab | + lollipop chart |
| Market Watch positions table, bank card, dot plot | Explorer | + gap analysis, term structure, bank detail |
| Rate Changes filters, table, event detail, source link | Rate Movements | + timeline, feed, inspector cards, coverage notice; table under *Advanced* |
| History lines, market series, timeline + table | Market Trends | leader/median and dispersion now both visible; + highlight, gaps, term structure |
| Scenarios inputs, run, provenance, invalidation, table, curve, decision text | Pricing Studio | + 3-step flow, friendly labels, input guide, comparison chart, differences, governance flags, limitations |
| Scenario Lab | Pricing Studio → Advanced | unchanged behaviour |
| Operations controls | Operations & Trust → Collection & maintenance | unchanged |
| System Health components, collectors, runs, suspicious, product tabs | Operations & Trust → Data health | + tiles, freshness chart, data-state key |

## 7. Acceptance review

| Question | Answer (as built) |
| --- | --- |
| Market leader in seconds? | First KPI card and the top row of the Market story chart. |
| Analyst understands our position? | Highlight a bank once: rank, gaps to leader/median on Command Centre, Explorer, gap chart and an insight sentence. |
| Recent repricing found immediately? | "What changed" feed on the landing page; Rate Movements one click away. |
| Charts understandable without the developer? | Each chart has a caption saying what it shows and how to read it; rate labels on marks; legends on top. |
| Market intelligence → pricing scenario? | "Open a pricing scenario" link; the term carries over to Pricing Studio. |
| Stale or unreliable data visible? | Coverage badge on every page, Data trust panel, matrix shading, `⚠ QUARANTINED`, coverage notice on Rate Movements, Data health tiles and chart. |
| One unified platform? | Shared header, theme, tokens, components and chart styling. |
| Every control meaningful? | Each filter changes the data shown; no decorative toggles. Old radio-switched market series replaced by two always-visible charts. |
| Works at laptop widths? | Reviewed at 1280 and 1440 px; no horizontal overflow at 500 px (narrowest the review browser allowed). |
| Detailed data still reachable? | Every former table is present (tabs, expanders, *Advanced* sections). |

## 8. Verification

- `python3 -m ruff check .` — clean.
- `python3 -m pytest -q -m "not live_rates"` — see the PR for the run; new tests in
  `tests/unit/test_dashboard_redesign.py` (shared filters across pages, insights, Operations
  & Trust composition and no-collect-on-render, legacy redirects, no `pages/` folder,
  governance notices, scenario differences, grouped exclusions) and new chart tests.
- Browser review (Chrome via DevTools automation) against the real local database (quiet
  market) and a synthetic demo database with moves and a failing collector, at 1440 px
  light, 1280 px dark and 500 px. Screenshots: [`images/dashboard_redesign/`](images/dashboard_redesign/)
  (`before_*` are the pre-redesign console on the same data).

## 9. Limitations and deliberate omissions

- **Mortgage-only market views.** No product selector: deposits rank highest-is-best and need
  product-aware services; deposit data is collected but not yet exposed in market views.
- **No KPI deltas.** A "vs 7 days ago" delta would mix the freshness-filtered snapshot with
  the unfiltered history series and could show a move that did not happen.
- **Dashboard-wide filters are per session**, not encoded in the URL.
- **Event cards are not clickable**; the inspector uses a keyboard-accessible selectbox.
  Streamlit cannot make an arbitrary container a button without custom components.
- **Phone layout** stacks every column (long scroll); the top navigation folds into a menu.
- **Scheduler state** is still not observable; nothing claims a scheduler is active.
- **Charts are canvases/SVG** with tooltips and an adjacent table or caption as the text
  alternative; full screen-reader parity for chart content is not achievable in Vega-Lite.
