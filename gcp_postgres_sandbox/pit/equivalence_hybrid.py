"""CP-011 Phase D Stage 2B — hybrid (full-osc + chunked-remainder) equivalence.

Proves the run_v2 Stage 2B mechanism is byte-identical to a monolithic build:

  - oscillators (ADX) computed once on the FULL frame (required because
    calculate_adx uses global shifts -> cross-slug contamination at boundaries)
  - all other families computed per chunk (with bitcoin benchmark in every chunk)
  - bitcoin output rows emitted once, by chunk 0

Compares monolithic vs hybrid over every output column on a 6-asset frozen
subset (bitcoin + long/mid/delisted/sparse), 2 chunks.

Output: report/cp011-phase-d/hybrid_equivalence.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from pit.run_v2 import _emit_chunk, _compute_osc_bins, _OUT_COLS

OUT = Path(__file__).resolve().parent.parent.parent / "report" / "cp011-phase-d"
SNAP = Path(__file__).resolve().parent / "frozen_sample_30.parquet"

SUBSET = ["bitcoin", "ethereum", "litecoin", "dogecoin", "vgx-token", "kinic"]


def _canonical(df: pd.DataFrame) -> pd.DataFrame:
    return df.sort_values(["slug", "date"]).reset_index(drop=True)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    df = pd.read_parquet(SNAP)
    sub = df[df["slug"].isin(SUBSET)].reset_index(drop=True)
    print(f"subset: {sub['slug'].nunique()} slugs, {len(sub)} rows", flush=True)

    osc_full = _compute_osc_bins(sub)
    print(f"osc pass (full frame): {len(osc_full)} rows", flush=True)

    # monolithic
    mono = _canonical(_emit_chunk(sub, osc_full, "eq", "PIT_APPROX", include_btc=True))
    print(f"monolithic: {len(mono)} output rows", flush=True)

    # hybrid (2 chunks over non-bitcoin; bitcoin benchmark + osc in every chunk)
    non_btc = sorted(s for s in sub["slug"].unique() if s != "bitcoin")
    mid = -(-len(non_btc) // 2)
    chunks = [non_btc[:mid], non_btc[mid:]]
    parts = []
    for i, chunk_slugs in enumerate(chunks):
        comp = sub[sub["slug"].isin(chunk_slugs + ["bitcoin"])].reset_index(drop=True)
        osc_chunk = osc_full[osc_full["slug"].isin(chunk_slugs + ["bitcoin"])]
        out = _emit_chunk(comp, osc_chunk, "eq", "PIT_APPROX", include_btc=(i == 0))
        parts.append(out)
        print(f"  chunk {i}: {len(chunk_slugs)} slugs -> {len(out)} rows", flush=True)
    hybrid = _canonical(pd.concat(parts, ignore_index=True))
    print(f"hybrid: {len(hybrid)} output rows", flush=True)

    rep = {"subset": SUBSET, "mono_rows": int(len(mono)), "hybrid_rows": int(len(hybrid))}

    if len(mono) != len(hybrid):
        rep["pass"] = False
        rep["reason"] = "row count mismatch"
        with (OUT / "hybrid_equivalence.json").open("w", encoding="utf-8") as f:
            json.dump(rep, f, indent=2)
        print(json.dumps(rep, indent=2))
        return 1

    key_m = set(zip(mono["slug"], mono["date"]))
    key_c = set(zip(hybrid["slug"], hybrid["date"]))
    dup_m = int(mono.duplicated(subset=["slug", "date"]).sum())
    dup_c = int(hybrid.duplicated(subset=["slug", "date"]).sum())
    comp = mono.merge(hybrid, on=["slug", "date"], suffixes=("_m", "_c"),
                      how="outer", validate="one_to_one")
    cell_diffs: dict[str, int] = {}
    for c in _OUT_COLS:
        if c in ("slug", "date"):
            continue
        a, b = comp[f"{c}_m"], comp[f"{c}_c"]
        if c == "incomplete":
            mism = int((a.fillna(False) != b.fillna(False)).sum())
        else:
            na_ok = bool((a.isna() & b.isna()).all())
            if na_ok:
                mism = 0
            else:
                mism = int((a.fillna(np.inf) != b.fillna(np.inf)).sum())
        if mism:
            cell_diffs[c] = mism
    rep.update({
        "keys_equal": key_m == key_c,
        "key_uniqueness": dup_m == 0 and dup_c == 0,
        "cell_diffs": cell_diffs,
        "pass": bool(key_m == key_c and dup_m == 0 and dup_c == 0 and not cell_diffs),
    })

    with (OUT / "hybrid_equivalence.json").open("w", encoding="utf-8") as f:
        json.dump(rep, f, indent=2)
    print(json.dumps(rep, indent=2))
    print(f"saved -> {OUT / 'hybrid_equivalence.json'}")
    return 0 if rep["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
