# PR correctness checks

The `Python correctness / Python fatal errors` job checks every Python file with
Ruff 0.14.5 and the repository's `ruff.toml` on PRs into `main`. To reproduce it
locally:

```sh
python -m pip install ruff==0.14.5
python -m ruff check .
python -m pytest tests/ -q
```

The initial rule set targets syntax errors, invalid comparisons and control flow,
and undefined or unbound names (`E9`, `F63`, `F7`, `F82`). It has no per-file
exceptions or baseline suppression. This is a correctness check, not a claim of
complete type safety or formatting enforcement. Python 3.11 is the minimum
supported language version.

The first scan found eight undefined-name errors in a stale copy of CTP helpers at
the end of `arena/market/indicators.py`. Those unused duplicates were removed;
contract naming and quote validation remain in `arena/market/ctp.py`, exercised by
`tests/test_ctp.py`. Repository imports, qualified references and dynamic lookup
sites do not use the duplicates. Indicator and metric regressions remain in
`tests/test_indicators_metrics.py`.

The existing complete pytest suite stays in its own workflow. This new workflow
does not edit the packaging and replay changes in the existing PR #1; both can be
reviewed independently.

No real provider credentials, external market connection or live trade is needed
for this check. Native Windows UI acceptance still requires Windows. Making a
check required for merging is a branch-protection decision; this change does not
alter those settings.

## CTP retention round trip

The boundary regression found a mismatch between live and restored history.
`TickAggregator` retains `max_bars` closed base bars and a separate forming bar,
so its full snapshot contains `max_bars + 1` rows. Persistence and cold seeding
previously trimmed that snapshot to `max_bars` rows. At `13:59:45 UTC`, the lost
row can remove the oldest closed hourly bucket and leave only 59 after the
engine correctly rejects the forming hour; EMA60 then disappears on restart.

Persistence, both current/legacy namespace reads, and cold seeding now preserve
the same closed-bar budget plus forming slot. `retention_floor`, effective
`max_bars`, market-feed signatures and namespaces remain unchanged. There is no
schema change or data migration. Old N-row histories remain readable with the
same competition and row values; a bar already removed by an older writer cannot
be recreated, and the history fills naturally as new ticks arrive.

The guarantee follows the existing resampling semantics: 60 closed hourly
buckets can include a partially observed oldest bucket or session gaps. It does
not claim 60 completely observed hours or relax forming/future-bar admission.

## Verification on 2026-11-17

- Ruff and actionlint 1.7.7 pass. An undefined-name probe exits 1 with `F821`.
- The strengthened regression feeds exactly N closed base bars plus one forming
  bar and asserts that occupancy before taking the real engine snapshot. It
  checks EMA60 and MACD before and after restart at `13:59:05`, `13:59:29`,
  `13:59:30`, `13:59:45`, `13:59:59`, and `14:00:00 UTC`, for 30/60-second bars.
  Saved/restored OHLCV and indicator values are compared exactly.
- On the old implementation the `13:59:45` case passed before shutdown and failed
  the real EMA60 assertion after restart. The expanded regression group produced
  16 failures and 5 passes before the fix; all 21 pass after it. This includes
  small-capacity cold/live seeding for 10/30/60/300-second bars and compatibility
  with an older writer's N-row database, configuration and competition identity.
- Full Linux/Python 3.12 offline suite: **1517 passed, 162 warnings**.
- Each repository passes all 161 CTP and market-feed identity regression cases.
  Local test commands allow only loopback network traffic; FakeSdk supplies CTP
  ticks, with no provider credentials or external market connection.
- Native Windows UI acceptance is not performed on this Linux host. Hosted
  checks must be assessed on the final submitted commit. No existing test is
  skipped or weakened to obtain these results.
