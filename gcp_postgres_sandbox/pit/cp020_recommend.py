"""CP-020 — promotion recommendation (RECOMMENDATION ONLY).

Classifies experiments from BOTH validated datasets:
  A. Cross-sectional PIT DMV factors (cp012_backtests.json)
  B. BTC single-asset time-series factors (factor_research.json)

Classes: PROMOTION_CANDIDATE / NEEDS_MORE_VALIDATION / REJECTED.

  DMV rules:      material (|mean IC|>=0.05 and CI excludes 0) AND FDR-significant
  BTC-factor rules: material (|IC|>=0.10) AND FDR-significant AND OOS IC same sign
                   (a factor whose out-of-sample IC flips sign is NOT robust -> not a candidate)

Reads report/cp011-phase-d/*.json. Writes cp020_recommendation.json. NO DB/cache/
canonical writes — recommendation only; promotion needs separate approval.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent / "report" / "cp011-phase-d"
DMV_REPORT = ROOT / "cp012_backtests.json"
FACTOR_REPORT = ROOT / "factor_research.json"
OUT = ROOT / "cp020_recommendation.json"
MIN_N_DATES = 30
IC_MATERIAL_DMV = 0.05
IC_MATERIAL_FACTOR = 0.10


def _dmv_classify(r: dict) -> tuple[str, list[str]]:
    n_dates = r.get("n_dates") or 0
    if n_dates < 1 or not r.get("n_pairs"):
        return "REJECTED", ["NO_DATA"]
    if n_dates < MIN_N_DATES:
        return "NEEDS_MORE_VALIDATION", [f"SMALL_SAMPLE n_dates={n_dates}"]
    if not r.get("bh_significant", False):
        return "NEEDS_MORE_VALIDATION", ["NOT_FDR_SIGNIFICANT"]
    if not r.get("material", False):
        return "NEEDS_MORE_VALIDATION", ["NOT_MATERIAL"]
    return "PROMOTION_CANDIDATE", [
        "MATERIAL", "FDR_SIG",
        f"IC={r['ic_pearson_mean']:+.3f} CI=[{r['ci95_low']:+.3f},{r['ci95_high']:+.3f}]",
    ]


def _factor_classify(r: dict) -> tuple[str, list[str]]:
    n = r.get("n") or 0
    if n < MIN_N_DATES:
        return "NEEDS_MORE_VALIDATION", [f"SMALL_SAMPLE n={n}"]
    if not r.get("bh_significant", False):
        return "NEEDS_MORE_VALIDATION", ["NOT_FDR_SIGNIFICANT"]
    if not r.get("material", False):
        return "NEEDS_MORE_VALIDATION", ["NOT_MATERIAL"]
    ic = r.get("ic_pearson")
    oos = r.get("out_of_sample_ic", {}).get("ic_pearson")
    if ic is None or oos is None or (ic > 0) != (oos > 0):
        return "NEEDS_MORE_VALIDATION", ["OOS_SIGN_FLIP"]
    return "PROMOTION_CANDIDATE", [
        "MATERIAL", "FDR_SIG", "OOS_CONSISTENT",
        f"IC={ic:+.3f} CI=[{r['ci95_low']:+.3f},{r['ci95_high']:+.3f}]",
    ]


def main() -> int:
    dmv = json.loads(DMV_REPORT.read_text(encoding="utf-8"))
    factors = json.loads(FACTOR_REPORT.read_text(encoding="utf-8"))

    out: dict = {
        "source": {"dmv": str(DMV_REPORT), "btc_factors": str(FACTOR_REPORT)},
        "classification_rules": {
            "DMV_cross_sectional": "material (|IC|>=0.05, CI excludes 0) AND FDR-significant",
            "BTC_time_series": "material (|IC|>=0.10) AND FDR-significant AND OOS same sign",
        },
        "dmv_factors": [],
        "btc_factors": [],
        "summary": {},
    }

    def _summarize(lst):
        counts = {"PROMOTION_CANDIDATE": 0, "NEEDS_MORE_VALIDATION": 0, "REJECTED": 0}
        for e in lst:
            counts[e["class"]] += 1
        return counts

    # DMV cross-sectional
    for r in dmv["experiments"]:
        cls, reasons = _dmv_classify(r)
        out["dmv_factors"].append({
            "factor": r["factor"], "horizon": r["horizon"], "n_pairs": r.get("n_pairs"),
            "n_dates": r.get("n_dates"), "ic_pearson_mean": r.get("ic_pearson_mean"),
            "ci95_low": r.get("ci95_low"), "ci95_high": r.get("ci95_high"),
            "p_value": r.get("p_value"), "bh_significant": r.get("bh_significant"),
            "material": r.get("material"), "class": cls, "reasons": reasons,
        })

    # BTC time-series factors
    for r in factors["results"]:
        if not r.get("ic_pearson"):
            continue
        cls, reasons = _factor_classify(r)
        out["btc_factors"].append({
            "factor": r["factor"], "horizon": r["horizon"], "n": r.get("n"),
            "ic_pearson": r.get("ic_pearson"), "ic_spearman": r.get("ic_spearman"),
            "ci95_low": r.get("ci95_low"), "ci95_high": r.get("ci95_high"),
            "p_value": r.get("p_value"), "bh_significant": r.get("bh_significant"),
            "material": r.get("material"),
            "oos_ic": r.get("out_of_sample_ic", {}).get("ic_pearson"),
            "class": cls, "reasons": reasons,
        })

    out["summary"] = {
        "dmv_cross_sectional": _summarize(out["dmv_factors"]),
        "btc_time_series": _summarize(out["btc_factors"]),
    }
    out["promotion_candidates"] = [
        e for e in out["dmv_factors"] + out["btc_factors"] if e["class"] == "PROMOTION_CANDIDATE"]
    out["note"] = (
        "RECOMMENDATION ONLY. No materialization / no RDS/cache/canonical change. "
        "DMV cross-sectional: 1 candidate (d_pct_cvar h30, risk intelligence not alpha). "
        "BTC time-series: mvrv h7/h30 are strong, OOS-consistent, FDR-significant "
        "(IC -0.52 / -0.68) — the strongest signal in the suite. Promotion into any "
        "production signal (e.g. CP-018) requires separate explicit approval."
    )
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")

    print("=== CP-020 RECOMMENDATION (recommendation only) ===")
    print("--- BTC time-series factors ---")
    for e in out["btc_factors"]:
        print(f"  {e['class']:22s} {e['factor']:18s} h{e['horizon']:2d} ic={e['ic_pearson'] and round(e['ic_pearson'],3)} -> {e['reasons']}")
    print("--- DMV cross-sectional (candidates only) ---")
    for e in out["dmv_factors"]:
        if e["class"] == "PROMOTION_CANDIDATE":
            print(f"  {e['class']:22s} {e['factor']:18s} h{e['horizon']:2d} ic={e['ic_pearson_mean'] and round(e['ic_pearson_mean'],3)} -> {e['reasons']}")
    print("summary:", out["summary"])
    print(f"saved -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
