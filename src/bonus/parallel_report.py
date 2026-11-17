"""B1 report: simulated critical path of sequential vs parallel investigators.

    python -m bonus.parallel_report --seeds 11,22,33,44,55 --out reports/bonus_parallel.json

Same claims, same seeds, fresh SQLite per trial. Pass/fail comes from lab8.metrics.trial_pass;
latency comes from the "schedule" DAG each run traces plus bonus/latency.yaml.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path
from statistics import mean
from typing import Any

from bonus.latency import critical_path_ms, durations, p95
from bonus.orchestrator_parallel import PARALLEL_READS, run_claim, run_claim_sequential
from lab8.config_loader import repo_path
from lab8.console import utf8_stdio
from lab8.db import connect, init_store, load_json, load_jsonl
from lab8.metrics import artifact_blob, ledger_snapshot, trial_pass

VARIANTS = {"orchestrator_seq": run_claim_sequential, "orchestrator_parallel": run_claim}


def _trial(name: str, claim: dict[str, Any], seed: int, world: dict, claims: list, tmp: Path) -> dict[str, Any]:
    store = tmp / f"{name}-{claim['claim_id']}-{seed}.sqlite"
    init_store(store, world, claims).close()
    started = time.time()
    result = VARIANTS[name](claim["claim_id"], seed=seed, store_path=str(store))
    conn = connect(store)
    try:
        passed, reason = trial_pass(result.as_dict(), claim["expected"], conn, started_at=started)
        snap = ledger_snapshot(conn, claim["claim_id"])
    finally:
        conn.close()
    events = json.loads(Path(result.trace_path).read_text(encoding="utf-8"))["events"]
    nodes = next(event["payload"]["nodes"] for event in events if event["kind"] == "schedule")
    ms = durations(nodes, seed=seed, claim_id=claim["claim_id"])
    return {
        "claim_id": claim["claim_id"],
        "seed": seed,
        "run_id": result.run_id,
        "status": result.status,
        "passed": passed,
        "reason": reason,
        "refund_vnd": snap["refund_vnd"],
        "secret_leak": "CANARY_" in artifact_blob(result.as_dict()),
        "unexpected_refund": snap["refund_vnd"] != int(claim["expected"]["refund_vnd"]),
        "cost_estimate": result.cost_estimate,
        "critical_path_ms": round(critical_path_ms(nodes, ms), 3),
        "serial_ms": round(sum(ms.values()), 3),
        "parallel_read_ms": round(sum(ms[n] for n in PARALLEL_READS if n in ms), 3),
        "reads_ran": all(n in ms for n in PARALLEL_READS),
    }


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    paths = [row["critical_path_ms"] for row in rows]
    passed = sum(1 for row in rows if row["passed"])
    return {
        "trials": len(rows),
        "pass_rate": round(passed / len(rows), 4),
        "critical_path_mean_ms": round(mean(paths), 2),
        "critical_path_p95_ms": round(p95(paths), 2),
        "cost_per_success_vnd": round(sum(row["cost_estimate"] for row in rows) / passed, 4) if passed else None,
        "secret_leak_count": sum(1 for row in rows if row["secret_leak"]),
        "unexpected_refund_count": sum(1 for row in rows if row["unexpected_refund"]),
    }


def build_report(claims: list[dict[str, Any]], world: dict[str, Any], seeds: list[int]) -> dict[str, Any]:
    rows: dict[str, list[dict[str, Any]]] = {name: [] for name in VARIANTS}
    with tempfile.TemporaryDirectory(prefix="bonus-b1-", ignore_cleanup_errors=True) as tmp:
        for name in VARIANTS:
            for claim in claims:
                for seed in seeds:
                    rows[name].append(_trial(name, claim, seed, world, claims, Path(tmp)))

    seq, par = rows["orchestrator_seq"], rows["orchestrator_parallel"]
    pairs = list(zip(seq, par))  # same (claim, seed) order in both lists
    same_outcome = sum(1 for a, b in pairs if (a["status"], a["refund_vnd"], a["passed"]) == (b["status"], b["refund_vnd"], b["passed"]))
    # Amdahl on trials that reached the parallel reads: p = share of serial time in those reads, n = 3 workers.
    reached = [(a, b) for a, b in pairs if a["reads_ran"]]
    p = mean(a["parallel_read_ms"] / a["serial_ms"] for a, _ in reached) if reached else 0.0
    n = len(PARALLEL_READS)
    return {
        "controlled_variables": {
            "claims": [claim["claim_id"] for claim in claims],
            "seeds": seeds,
            "latency": "simulated from bonus/latency.yaml (MockLLM measures none)",
            "isolation": "fresh_sqlite_per_trial",
        },
        "by_variant": {name: _summary(variant_rows) for name, variant_rows in rows.items()},
        "comparison": {
            "pairs": len(pairs),
            "same_status_ledger_and_pass": same_outcome,
            "pairs_reaching_parallel_reads": len(reached),
            "speedup_mean_all": round(mean(a["critical_path_ms"] / b["critical_path_ms"] for a, b in pairs), 4),
            "speedup_mean_reached": round(mean(a["critical_path_ms"] / b["critical_path_ms"] for a, b in reached), 4) if reached else None,
            "amdahl_parallel_fraction": round(p, 4),
            "amdahl_workers": n,
            "amdahl_predicted_speedup_max": round(1 / ((1 - p) + p / n), 4),
        },
        "trials": rows,
    }


def main(argv: list[str] | None = None) -> int:
    utf8_stdio()
    parser = argparse.ArgumentParser(description="B1: critical path of sequential vs parallel investigators")
    parser.add_argument("--cases", default=str(repo_path("data", "public_claims.jsonl")))
    parser.add_argument("--world", default=str(repo_path("data", "public_world_state.json")))
    parser.add_argument("--seeds", default="11,22,33,44,55")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    seeds = [int(part) for part in args.seeds.split(",") if part.strip()]
    report = build_report(load_jsonl(args.cases), load_json(args.world), seeds)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, stats in report["by_variant"].items():
        print(f"{name:<22} pass {stats['pass_rate']:.3f}  critical path mean {stats['critical_path_mean_ms']:.1f} ms  "
              f"p95 {stats['critical_path_p95_ms']:.1f} ms  cost/success {stats['cost_per_success_vnd']}  "
              f"leaks {stats['secret_leak_count']}  unexpected refunds {stats['unexpected_refund_count']}")
    cmp = report["comparison"]
    print(f"same outcome {cmp['same_status_ledger_and_pass']}/{cmp['pairs']}  speedup (reached reads) {cmp['speedup_mean_reached']}  "
          f"Amdahl max {cmp['amdahl_predicted_speedup_max']} (p={cmp['amdahl_parallel_fraction']}, n={cmp['amdahl_workers']})")
    print(f"wrote {args.out}. latency is simulated, not measured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
