# Lessons from your past tasks

Each line is a mistake you made in this repository and was corrected in
review. Read them before answering: the same mistake again is a failed task.

## Editing blocks
- Write the bare path after SEARCH (`frontend/x.js`), never `path=…`, quotes, or the example `path/to/file` (#176, #177).
- A SEARCH must match exactly one place. `@layer skin {` appears 24 times in app.css: to add CSS at the end of a file use an APPEND block (#188, #190).
- Every file the task names as new must be created with an empty SEARCH. #188 imported `nozoom.js` without writing it: the whole app would have been blank.
- Never leave placeholder or example files in the answer (#178, #182).

## Tests
- When you change a behaviour, search `backend/tests/` for the function you changed and update the old tests that assert the old behaviour (#189 broke `test_system.py`).
- Test data must look real: strategies do not trade on candles with high == low == close and constant volume. Give candles a range and varying volume (#200).
- Assert what the task measured, not a guess: if the task gives measured numbers, use them.

## Stored results
- If a change alters numbers computed from saved data, bump the version constant that triggers the rebuild (`SYSTEM_VERSION` in alert_track.py) and fetch enough data for it (#189).

## Logic
- Per-kind settings stay per kind: gem alerts are judged with the gem system, not the meme one (#178).
- "One at a time" in a task means one at a time: dropping whole blocks of a list can overshoot (#182).

## Frontend
- No trade, no data: show "—", never a green `0%` (#190, #191, #201). A null percent is not zero.
- The initial state must match what is on screen: a pre-selected chip means the list is already sorted by it (#201).
- A function you call must be in scope where you call it. app.js is one big closure with inner functions: check where it is defined before calling it from a new place (#203, `setArea`).
- If the page's own boot later loads something over your change (the main site opens the saved ticker, BTC), hook your code into that boot step instead of running before it (#203).
- `window.open` must run synchronously inside the tap. After an `await`, iOS blocks it: open the window first, set its URL afterwards (#203).
- Phone inputs under 16px make iOS zoom the page; keep form fields at 16px or more (#188).
- Icons must read on the dark background: thin dark-brown strokes vanish; use the gold fill or light strokes of at least 2px (#211).
- A CSS grid needs one column per child: `.pf-cash` had 5 children in 4 columns and the last button wrapped onto its own row (#214).
- Before deleting a constant or function, search the file for every use of it: #216 removed `NAMES` while `onClick` still read it.
- Every function you call must be imported: #190 called `strategyTrade` in detail.js without importing it, so the token page broke.
- "Never show X" means every place X can reach the screen, including labels passed through (`al.system.label` printed "Trend HF" in the event list, #216).
- `threading.Lock` is not re-entrant: never call a function that takes the same lock (e.g. `profiles.get`) while holding it — the tests hang forever (#226).
- #226 repeated #190: `hideToken` used in detail.js without an import. Before finishing, list every function you call in each file you touched and check its import line.
- A test file listed in `Files:` with "create" must be created: #245 changed main.py and skipped `test_dex_spot_chart.py`, so nothing proved the fix. No test = unfinished task.
- Read what a function returns before using it: `radar.run_spot` returns `(result, events, position)` and `BacktestResult` has no `.events`; #242 failed twice on `result.events`. Check the `return` line of each function you call, not just its name.
- Use only names you can see in the code you are given: #249 imported `GECKO_NETWORKS` from alert_track (it lives in `app.sniper.scanner`) and #250 patched `profiles._PROFILES` (it is `_STORE_FILE`). When the task names an attribute or a module, use exactly that; never guess one.
- `pytest.mark.asyncio` does not work here (no pytest-asyncio): test coroutines with `asyncio.run(...)` inside a plain test function (#249).
- A new module-level name used in a file needs its import in that file: #250 called `profiles.wanted_narratives()` in discovery.py without importing `profiles` (ruff F821).
- A DataFrame index is not a Series: `df.index` has no `.iloc`; use `df.index[i]` (a Timestamp: `.timestamp()`), #249.
- Before using a stdlib module in a file, check the file's import lines: main.py has no `import time` (#252 failed on `time.monotonic()`). Add the import when it is missing.
- Edit only files you were given: #250 sent a SEARCH block for tests/test_onchain_discovery.py, which was not in the task, and it matched nothing.
- #252 repeated #245: the test file the task asked for was not created. The worker now fails the task when it is missing.
- #259: `onchain_narratives` used in main.py without its import, and old tests that asserted the old $3M / one-year limits left unchanged. When a task changes a limit, search the tests for the old number and update them.
- #256: `strategies.rug_bar` used in system.py without importing `strategies`, the main.py part of the task (three `spot_only(...)` calls) skipped, and `asyncio.run()` around a plain function. Do every numbered point of the task; check each call is to an async function before wrapping it.
- #260 passed without its test (a worker bug, now fixed) and wrote a fixed "holders top10 n/a%" instead of real data. Never write a placeholder value into a log line: use a field you can see, or leave the part out.
- #269: `onchain_judge` used in main.py without its import (third time the same miss: #256, #259); the test named `projects_db` where the file imports it as `db`; the fake search ignored `status`. Read the import lines of every file you touch, tests included.
- #266: checked the rug signals one type after another and took the first TYPE that ever fired, not the first BAR: a later candle beat an earlier crash. When several conditions compete in time, take the earliest bar. Test data must be able to show the case (open == close can never make a -50% candle).
- #269: `judge.py` imported `leaderboard` at module level while `leaderboard` imports `judge` and ranks its saved entries at import time: the server crashed on start (partially initialized module). Never import a module that imports yours back; import it inside the function that needs it.
- #286: reset `bars_fetched` whenever bars were missing, so a dead pool (answers [] forever) was fetched again every tick. A retry that clears a "done" flag must be bounded: mark it (`bars_refetched`) and test the empty answer.
- Path filters on substrings ("test" in path) drop real code like attestation.rs or builder.go: match whole folder names and file-name patterns. A background step that retries on failure must remember the attempt, or it hits the same item (and GitHub's hourly limit) every tick. (#291)
- #300: invented an owner quote and a future date ("Lorenzo, 30/09") in comments, and replaced a whole tab instead of swapping only the table the task named (score, board and market cap disappeared). Quote only words given in the issue, and change only the part of a view the task names.
- A new module-level variable must not share a name with a `const` declared later in the same function: reading it earlier in that function throws (temporal dead zone). Check the whole function for the name before adding a global (#297, `chosen` in leaderboard `render`).
- #306: called `priority.backtests_first()` in three modules without importing `priority` (NameError), and the test called `alert_track.record(item)` without `kind` and `now`. Every new name used in a file needs its import in that file; read the signature of a function before calling it in a test.
- #311: re-rendering animated bars with innerHTML on every poll kills the CSS transition (new nodes start at their final value). Build the DOM once, then update transform/text in place.
- Changing a function's signature breaks the tests that monkeypatch it: grep `tests/` for fakes of that function (`def fake_...`, `monkeypatch.setattr(mod, "name"`) and update them too (#314).
- #322: F821 again (used backtest_lab in main.py without importing it; same as #306). Before using a module name, grep the file's imports; if absent, import it locally inside the function. Ruff in the worker caught it, the retry did not fix it.
- #324: a test looping over every tool called them with no args and crashed on tools with required params. When a test iterates a registry, build the call from each entry's own spec (required params), and skip the ones that write.
- #459: the token page's state is `current = { id, e }` but the click handler read `current.chain`; the store file was never loaded at import (results lost on every restart) and its path came from a private helper of another module instead of `storage.data_file`; the test used `set_standing` (an UPDATE) on a row that did not exist. Read the shape of the globals you use, load what you save, and create test rows with the module's insert function (`projects_db.upsert` + `record`).
- #462: moved a local (`pending`) into a new helper and left another use of it in the same function (no-undef); particles with a birth delay were drawn from frame one (the delay was added to their life instead of skipping them until born); the render loop stopped when the first batch died, so the next batch never drew; one strike line at 50% of a multi-line point. When you extract code, grep the old function for every name you moved; a scheduled item is skipped until its time, and a loop that others feed must restart when they add to it.
- #464: an empty quote counted as evidence (`"" in text` is always true), the evidence was cut at 6000 chars with the "Checked sources" at the end (cut first), a missing status still defaulted to "confirmed", the app probe read only the entry bundle (the address sat in a lazy chunk) and counted an SPA's HTML 200 as a live API, and no tests or page part were written. Test a validator with the empty input, put what the model must quote at the top, and run a probe on the real case named in the issue before calling it done.
- #467: changed the judge SYSTEM prompt (added Discord) without bumping `PROMPT_VERSION`, so no project was re-judged with the new data. Any edit to a prompt that has a version constant bumps it, and the issue says so.
- #469: the incremental update re-read all 5 pages every day (no stop at the last post already read), and without a DeepSeek key it still fetched pages on every judge run; tests shared the module cache across cases and asserted a "new posts" call on a fixture with none. Pass the last seen id down so paging stops there, check the key before any fetch, reset every module cache a test touches, and make a fixture actually contain what the assert expects.
- #475: used SOCIAL_POINT/CODE_POINT/VOLUME_POINT without defining them, quoted a "Cross-check" line from `found` that nothing ever wrote there, and pasted the same rule text into two prompts. Rule: run the tests you list before answering; every name you use must be defined in the diff or already exist (grep it, and grep before adding a new one: TEAM_POINT already existed); a quote must be built by the same helper that writes the evidence line; a rule shared by several prompts goes in one constant (judge.CHECK_RULES).
