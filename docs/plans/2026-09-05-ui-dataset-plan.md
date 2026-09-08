# SGO research workspace and dataset selection

## Assessment and design
The existing centered dark form gives configuration, input, and output similar visual weight. Dataset selection disappears after setup and a single global dataset cache can serve the wrong country. Keep the existing FastAPI/plain HTML architecture, templates, evaluation, calibration, counterfactuals, audits, and reports.

Use a warm paper/ink/pine editorial workspace: compact brand header, prominent title, restrained three-stage workflow indicator, wide input area and a narrower persistent panel configuration area. Stack at mobile widths. Dataset selection must always be available, with actual availability/count metadata rather than fabricated counts. Existing English product language remains.

## Implementation plan
**Goal:** Improve usability and visual hierarchy and make dataset selection control the actual evaluation population.
**Architecture:** Maintain existing FastAPI and browser JS; introduce explicit dataset identity and per-dataset paths/cache. Preserve legacy USA data discovery. No frontend framework or added build step.
**Tech stack:** Python, FastAPI, HTML, CSS, JavaScript, unittest.

1. Backend dataset behavior (`web/app.py`, possibly `scripts/persona_loader.py`, `tests/test_datasets.py`): write/run regression tests first. Config exposes available datasets, status/count, defaults. Setup validates IDs, separates country paths, verifies existing identity, rejects mismatches, and retains existing data on failed setup. Cohort config explicitly selects a dataset (or generated personas) and never silently falls back when an explicitly selected dataset is missing. Caches cannot cross countries/requests. Preserve omission compatibility. Provide actionable schema errors for unsupported datasets. Avoid real downloads or paid LLM calls during tests.
2. UI (`web/static/index.html`, optional `web/static/workspace.css`): apply the visual direction above, accessible labels/focus, responsive tables, semantic template buttons. Persistent dataset picker populated by API with readiness/count and an explicit load action. Capture dataset at run start and disable changing it during run/setup; restore controls on failure. Include selection in cohort requests and retain source context in result/log. Distinguish LLM-generated personas explicitly. Keep all existing flows working. Use truthful loading/error states.
3. Verification: run regression suite, Python and JS syntax checks; launch local server and inspect desktop/mobile in browser; exercise dataset switching/loading failures and a mocked evaluation flow without API spend. Review diff for correct provenance, stale state, error recovery, and regressions. Fix issues, then re-run affected checks.

## Acceptance checklist
- [x] Clear hierarchy and responsive workspace
- [x] Dataset selector always discoverable, correct status and explicit generation option
- [x] Selected country controls cohort request and backend data
- [x] No cross-country cache reuse or overwrite
- [x] Errors recover without losing inputs
- [x] Existing evaluation/results/calibration/audit/report controls retained
- [x] Tests and browser verification complete

User requested autonomous planning/review and a cheaper model for coding. Implementation is delegated to GPT-5.6-luna; primary agent retains design and review responsibility. Existing `.env.example` and `uv.lock` changes belong to the user and are out of scope.

## Review outcome and validation
Implementation was split into exclusive frontend and backend ownership after the initial pass. Both coding agents used GPT-5.6-luna; the primary agent reviewed requirements, code, screenshots, and browser behavior. Dataset identities now use metadata, caches are keyed by country and path, and setup preserves mismatched/incomplete folders. Correction after the September 6 live metadata retest: India uses configuration `default` and split `en_IN`, not configuration `en_IN`. Country-specific geography and native categorical values are used for filtering.

Final validation on September 5, 2026:
- Python unittest discovery: 21 tests passed.
- Node runtime regression: pipeline failure unlocks controls and avoids blocking alerts.
- Python compilation, JavaScript syntax, and whitespace checks passed.
- Chrome with local deterministic substitutes: template fill, unavailable-country preflight, setup failure, generated cohort evaluation, all-evaluators-fail recovery followed by successful retry, counterfactual results, CTR calibration, and all three audit probes passed.
- Desktop and 390px mobile screenshots inspected; result tables scroll within their containers.
- Local real-config preview: http://127.0.0.1:8011. Mock verification server used port 8012 and was kept separate from real data.

No new country datasets were downloaded and no paid LLM evaluation was run. Existing USA metadata reports 1,000,000 records. Default dataset folders are rediscovered after restart; an advanced custom folder must be selected again after a server restart. Incomplete existing folders are preserved and require choosing another empty folder. Existing pipeline rate limits remain unchanged; the offline mock server bypassed them for repeatable checks.
