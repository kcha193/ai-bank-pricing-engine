# Pricing Watch application overrides

This refines, and does not replace, Claude's generated `MASTER.md`. Read this file after the master, then the matching `pages/` override. Code tokens live in `dashboard/theme.py`; Streamlit's native light/dark values live in `.streamlit/config.toml`.

## Verified skill research

Used the installed `ui-ux-pro-max/scripts/search.py` on 2026-10-31:

| Query / domain | Verified result | Application |
| --- | --- | --- |
| banking pricing intelligence dashboard / design-system, variance 4, motion 2, density 8 | Data-Dense Dashboard; blue/amber; Dashboard Data typography | Compact comparative analytics with clear drill-down |
| analytics dashboard / product | Analytics Dashboard; drill-down analytics + comparative; no landing-page pattern | Overrides generated Enterprise Gateway, sales CTA and video hero |
| banking / color | Banking/Traditional Finance; navy, blue, slate surfaces | Keep lending blue/navy; deposits teal; amber reserved for review |
| financial dashboard / typography | Dashboard Data, Fira Code + Fira Sans | Sans for labels/prose, tabular mono for values; system fallbacks keep offline use working |
| ranking comparison / chart | Compare Categories; horizontal ranking, direct values, data-table alternative | Retain backend-ordered lollipops; never apply generic descending sort to loans |
| time series trend / chart | Trend Over Time; lines, distinct styles, raw data alternative | Step lines for advertised rates; tooltips, bank controls, missing-data gaps |
| horizontal scroll / ux | Responsive: fit content within viewport | Reflow cards and filters; tables scroll within their own viewport |
| navigation deep links / ux | Deep Linking | Keep native top navigation, legacy redirects and product query parameter |
| keyboard focus contrast / ux | Focus Appearance / Focus States | Visible focus, text alongside colour, no hover-only actions |

Rejected search matches: financial comparison initially returned OHLC/radar; these data have neither OHLC nor multiple attribute axes. A narrower trend/ranking query found the matches above. Fintech initially returned crypto gold/purple; the banking query supplied the appropriate palette. Generic dense-responsive UX results were off-topic; the horizontal-scroll query supplied the relevant guidance.

## Application rules

- One executive heading, short purpose line, compact product status and persistent native navigation.
- Sans headings and labels; tabular mono values. Prefer installed Fira faces, fall back to system fonts; no third-party font request required.
- 8/12/16/24/32px rhythm; 12px card radii. Cards group related information; no decorative hover lift or pointer on static cards.
- Primary blue `#1E40AF`; deposit teal `#0E8C8C`; readable body text inherited from native light/dark themes. Amber is a review signal, never the default action.
- Avoid hard-coded dark navy chart marks against dark backgrounds; use shared mid-tone chart series.
- Reflow metric grids to two columns on tablet, one on small mobile; filters and analytical columns stack. Native nav overflow remains usable on narrow screens.
- Minimum 44px button/selector targets; visible keyboard focus; reduced-motion support; no scroll reveal or GSAP dependency.
- Every chart has a title, unit, tooltip and textual/table alternative. Rate axes are intentionally non-zero; bps charts keep their own zero-based axes.
- All status words come from existing view models. All bank-page/error strings remain in literal dataframe cells; validated source links use `st.link_button`.

## References

[Streamlit chart rendering](https://docs.streamlit.io/develop/api-reference/charts/st.altair_chart), [Altair interaction parameters](https://altair-viz.github.io/user_guide/interactions/parameters.html), [WCAG 2.2](https://www.w3.org/TR/wcag/). These support the native chart integration, bounded interaction and accessibility checks; this is not a formal WCAG conformance claim.
