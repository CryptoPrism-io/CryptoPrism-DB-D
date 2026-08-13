"""CP-020 — promotion recommendation for the pit_dmv factor set (RECOMMENDATION ONLY).

Classifies every CP-012 experiment (PIT dmv factor x forward-return horizon) as:
  - PROMOTION_CANDIDATE   material (|mean IC|>=0.05 and CI excludes 0) AND FDR-significant
  - NEEDS_MORE_VALIDATION  FDR-sig but not material, or small sample
  - REJECTED               no data / null

Reads report/cp011-phase-d/cp012_backtests.json. Writes
report/cp011-phase-d/cp020_recommendation.json. NO DB/cache/canonical writes —
this authorizes a recommendation only; promoting anything into production (e.g.
CP-018) requires a separate explicit approval.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPORT = Path(__file__).resolve().parent.parent.parent / "report" / "cp011-phase-d" / "cp012_backtests.json"
OUT = Path(__file__).resolve().parent.parent.parent / "report" / "cp011-phase-d" / "cp020_recommendation.json"
MIN_N_DATES = 30
IC_MATERIAL = 0.05


def classify(r: dict) -> tuple[str, list[str]]:
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


def main() -> int:
    data = json.loads(REPORT.read_text(encoding="utf-8"))
    out = {
        "source": str(REPORT),
        "window": data.get("window"),
        "panel_rows": data.get("panel_rows"),
        "leakage_check": data.get("leakage_check"),
        "classification_rules": {
            "PROMOTION_CANDIDATE": "material (|IC|>=0.05 and CI excludes 0) AND FDR-significant",
            "NEEDS_MORE_VALIDATION": "FDR-sig but not material, or small sample",
            "REJECTED": "no data",
        },
        "experiments": [],
        "summary": {},
    }
    counts = {"PROMOTION_CANDIDATE": 0, "NEEDS_MORE_VALIDATION": 0, "REJECTED": 0}
    for r in data["experiments"]:
        cls, reasons = classify(r)
        counts[cls] += 1
        out["experiments"].append({
            "factor": r["factor"], "horizon": r["horizon"],
            "n_pairs": r["n_pairs"], "n_dates": r.get("n_dates"),
            "ic_pearson_mean": r.get("ic_pearson_mean"),
            "ic_spearman_mean": r.get("ic_spearman_mean"),
            "ci95_low": r.get("ci95_low"), "ci95_high": r.get("ci95_high"),
            "p_value": r.get("p_value"),
            "bh_significant": r.get("bh_significant"),
            "material": r.get("material"),
            "class": cls, "reasons": reasons,
        })
    out["summary"] = counts
    out["promotion_candidates"] = [
        e for e in out["experiments"] if e["class"] == "PROMOTION_CANDIDATE"]
    out["note"] = (
        "RECOMMENDATION ONLY. No materialization, no RDS/cache/canonical change. "
        "Factors are the PIT dmv scores/VaR/CVaR/metrics. A promotion candidate here "
        "means the factor shows material + FDR-robust predictive power on forward "
        "returns; promoting it into any production signal/strategy (e.g. CP-018) "
        "requires separate explicit approval. |IC| magnitudes are small (<=0.06)."
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")

    print("=== CP-020 RECOMMENDATION (recommendation only) ===")
    for e in out["experiments"]:
        print(f"  {e['class']:22s} {e['factor']:18s} h{e['horizon']:2d} "
              f"ic_p={e['ic_pearson_mean'] and round(e['ic_pearson_mean'],4)} -> {e['reasons']}")
    print("summary:", counts)
    print(f"saved -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
