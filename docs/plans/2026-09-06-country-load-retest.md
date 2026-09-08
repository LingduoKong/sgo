# Country loading retest — September 6, 2026

## Confirmed defect and fixes

The actual datasets library rejected the original India call with `ValueError: BuilderConfig 'en_IN' not found. Available: ['default']`. The Hub dataset card's usage example was insufficient to establish the live API contract. The earlier mocked download tests accepted the wrong configuration and therefore missed this defect.

India now requests configuration `default`, split `en_IN`. Dataset counts use that same split. Saved Hindi data cannot be reported as English: mismatched saved split metadata is rejected without modifying the folder. Singapore `planning_area` and Brazil `municipality` are now retained in profile locations.

## Verification scope

| Country | Live builder configuration / split | Public two-row samples: save, reload, filter, profile conversion |
| --- | --- | --- |
| USA | default / train — passed | Passed; existing local 1,000,000-row dataset also loaded successfully |
| Japan | default / train — passed | Passed |
| India | default / en_IN — passed after fix | Passed |
| Singapore | default / train — passed | Passed |
| Brazil | default / train — passed | Passed |
| France | default / train — passed | Passed |

Live checks used `datasets.load_dataset_builder` plus Hugging Face's public `/splits` and `/rows` APIs. Only two synthetic public rows per country were fetched. The setup integration probe substituted those real sample rows for the full network download, but used actual Arrow saving/loading, path registration, metadata checks, filtering, and profile conversion. It also verified cache isolation, existing-folder reuse, empty-folder setup, and country-mismatch rejection.

Fresh regression suite: 28 Python tests passed; Node UI runtime test, Python compilation, and diff whitespace checks passed. Full multi-GB downloads and paid LLM calls were not performed. Running web services were not restarted, so existing in-memory results remain intact; restart is required to use the corrected backend.

Temporary probe: `/private/tmp/sgo_country_probe.py`. Temporary results: `/private/tmp/sgo-country-probe-results.json`.

## Requested restart and UI verification

At the user's explicit request, stopped the confirmed project service on port 8000 (PID 47545) and started the updated backend on that same port (PID 62353). The real browser UI successfully loaded the local 1,000,000-row USA dataset, switched through all sources, blocked an unavailable India evaluation, and rejected reuse of the USA directory for India without changing existing data.

An isolated server on port 8012 used the actual setup endpoint, temporary directories, and two public real rows per country in place of full network downloads. Browser clicks loaded all six countries successfully. Each run showed locked controls during loading, correct count 2, and restored controls afterwards. A deliberately failed India save showed the error and allowed a successful retry. An India cohort proceeded through evaluation using deterministic LLM/evaluator substitutes; Full analysis rendered Markdown, Rendered/Source switching and Edit/Preview switching retained original text. A 390px viewport inspection showed analysis clientWidth and scrollWidth both 294px, with no horizontal overflow.

UI verification found and fixed a race: starting evaluation remained possible during dataset setup. Shared busy-state handling now locks the run button and advanced directory too, preserves locks across configuration refresh, and guards duplicate setup/evaluation calls. Runtime regression checks cover these cases. Final 28 Python tests and Node runtime checks passed. The isolated server was stopped; the real service remains on port 8000. No paid LLM calls or full new-country downloads were made by these UI tests.
