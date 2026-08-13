# CP-011 Phase D ??? Stage 1 Gate: Network-Blocked Status

**Status:** STAGE 1 PREPARED, NOT EXECUTED ??? **external network blocker** (AWS RDS unreachable).
**Branch:** `feat/cp011-phase-d-hybrid-rebuild` (local) ?? main `c5cbfb8` ?? CP-009 frozen `e5d76a3`
No shadow or canonical writes performed.

## Network blocker (evidence)

At run time Python hostname resolution to the AWS RDS endpoint and TCP connectivity to AWS
both fail, while public internet works:

| Check | Result |
|---|---|
| `socket.getaddrinfo(DBHOST)` | Errno 11004 (resolver down) |
| `nslookup DBHOST` | OK (system DNS works; resolved IP used) |
| TCP 34.206.62.227:5432 (RDS) | **WinError 10051 ??? unreachable network** |
| TCP to a us-east-1 AWS IP:443 | WinError 10051 ??? unreachable |
| mempool.space (public) | OK |

The network path to AWS (us-east-1) is down; this blocks ALL Stage 1 reads of the existing
`FE_*_SIGNALS` tables (equivalence, coverage partition) and every Stage 2 write. Earlier in
this session the same RDS was reachable (preflight + canonical checksums succeeded), so this
is an intermittent environment outage, not a code/DB problem.

## What is ready (committed locally)

- **`pit/regenerate.py`** ??? Core-4 signal regeneration from raw OHLCV via the repo TA
  functions, with the missing generators satisfied (Supertrend, Bollinger, Aroon added so
  the declared bins are produced); only declared `SIGNAL_FAMILIES` bins selected.
- **`pit/stage1_gate.py`** ??? the Stage-1 gate harness:
  - `methodology_freeze()` documents the frozen indicator parameters + bin definitions
    (`cp011_pit_v1`).
  - `compare_frames()` regenerates vs existing `FE_*_SIGNALS` per (slug,date,signal) with
    exact-match / both-null / mismatch / one-sided-null classification.
  - Stratified equivalence sample: bitcoin, ethereum, litecoin, dogecoin, ripple, cardano,
    polkadot, chainlink, uniswap, aave, sushi, yearn-finance (long/medium-history covered).
  - Scaffolding for the coverage partition + ???30 regeneration-required-slug benchmark.
- **`pit/db.py`** ??? shared connection helper with DNS fallback (nslookup ??? IP + documented
  non-verifying SSL for read-only research) to survive the resolver outage.

## Remaining Stage 1 work (blocked on network)

1.2 Run equivalence ??? classify exact vs warm-up/NULL differences; STOP if material unexplained
    differences.
1.3 Build the exact coverage partition (covered / regen-required / warm-up-ineligible /
    incomplete-with-reason) from the DB.
1.4 Benchmark ???30 regeneration-required slugs; report median/p90/projected total; STOP if > 6 h.
1.x Stage 2 full hybrid rebuild (gated on Stage 1 passing).

## Next

Retry Stage 1 when the AWS network path recovers. No shadow schema created; canonical
tables + Phase C dry-run schema untouched; nothing pushed/PR'd/merged.
