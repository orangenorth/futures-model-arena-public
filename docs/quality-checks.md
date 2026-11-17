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

## Verification on 2026-11-17

- Ruff and actionlint 1.7.7 pass. An undefined-name probe exits 1 with `F821`.
- The 12 selected canonical CTP helper cases and all four indicator/metric cases
  pass. Before this change, the Linux/Python 3.12 suite passed 1497 tests. A later
  full run passed 1496 and failed the existing
  `test_sub_minute_bars_still_reach_the_1h_indicators` case.
- A fixed clock reproduces the failure at `13:59:45 UTC` and passes at `13:59:05
  UTC`. The same failure is reproduced in the private repository's untouched main
  archive; both repositories share the relevant CTP code. The 7200 retained
  30-second bars can contain only 59 completed hourly buckets once the forming
  bucket is removed. The final full run allowed only loopback network access; the
  failure is unrelated to external connectivity or the removed duplicate functions.
- This is an unresolved existing regression, not a green-suite claim. Its fix
  needs separate review: `retention_floor` also participates in the market-feed
  signature and persisted history identity. The lint change does not alter that
  contract, weaken the assertion, freeze this test at a passing time, or skip it.
- Local Windows package/UI checks were not run on this Linux host. Hosted PR
  results must be checked on the submitted commit; a passing run alone cannot
  disprove the deterministic hourly-boundary failure above.
