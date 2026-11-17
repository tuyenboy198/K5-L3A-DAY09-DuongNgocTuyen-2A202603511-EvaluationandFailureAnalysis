"""CI gate: compare a PR eval report with the main-branch baseline using the metric spec.

    python -m lab9.gate --pr reports/eval_workflow.json --baseline reports/baseline_v1.json

exit 0 = merge, exit 1 = blocked. Safety metrics: max 0, no averaging away.
Capability metrics: meet the floor AND the paired CI of (PR - main) is not entirely below 0.
Thresholds may only get stricter on the same eval-set version (ratchet).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from lab8.console import utf8_stdio
from lab9 import stats
from lab9.eval import SPEC, aggregate, load_spec, per_item


def integrity_check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> list[str]:
    """M7 (Expert): refuse to compare reports that cannot be trusted. Return a list of problems
    (empty = trustworthy). A gate that passes a broken report is worse than no gate.
    Must catch at least:
      - any metric value in pr["metrics"] that is NaN or inf
      - len(pr["rows"]) != manifest items x len(manifest seeds)   (dropped / duplicated rows)
      - the (item_id, seed) set of PR differs from the baseline's (cherry-picked seeds/items)
      - pr["evaluator_failures"] non-empty, or a row whose value for a graded spec metric is
        None (a crashed grader silently scores as "missing" and drops out of the mean)
      - base_spec given and a threshold in spec is LOOSER than in base_spec for the same
        eval-set version (ratchet: max may not rise, min may not fall, a blocking metric may
        not become non-blocking, a metric may not disappear; the same for spec["slices"],
        including dropping no_significant_drop)
      - an item's slice tags in PR rows differ from the baseline's (relabelling an item out of a
        slice hides its regression from the slice gate)"""
    problems: list[str] = []
    rows, brows = pr.get("rows") or [], base.get("rows") or []
    man, bman = pr.get("manifest") or {}, base.get("manifest") or {}

    # 1. metric values must be finite numbers (NaN < min and NaN > max are both False)
    for k, v in (pr.get("metrics") or {}).items():
        if v is not None and not _finite_number(v):
            problems.append(f"metric {k} = {v!r} is not a finite number")
    gone = sorted(set(base.get("metrics") or {}) - set(pr.get("metrics") or {}))
    if gone:
        problems.append(f"metric(s) {gone} present in the baseline but missing from the PR report")

    # 2. row count = items x seeds, no duplicated (item, seed), manifest = baseline manifest
    seeds = man.get("seeds") or []
    if not isinstance(man.get("items"), int) or len(rows) != man["items"] * len(seeds):
        problems.append(f"{len(rows)} rows != manifest {man.get('items')} items x {len(seeds)} seeds")
    keys = [(r.get("item_id"), r.get("seed")) for r in rows]
    seen: set = set()
    dups = sorted({k for k in keys if k in seen or seen.add(k)}, key=str)
    if dups:
        problems.append(f"{len(dups)} duplicated (item, seed) row(s), e.g. {dups[0]}")
    if man.get("items") != bman.get("items") or sorted(seeds) != sorted(bman.get("seeds") or []):
        problems.append(f"manifest items/seeds {man.get('items')}/{seeds} != baseline "
                        f"{bman.get('items')}/{bman.get('seeds')}")
    if len(rows) != len(brows):
        problems.append(f"{len(rows)} rows != baseline's {len(brows)}")

    # 3. same (item, seed) set as the baseline (no cherry-picked items or seeds)
    have, want = set(keys), {(r.get("item_id"), r.get("seed")) for r in brows}
    if have != want:
        problems.append(f"(item, seed) set differs from baseline: {len(want - have)} missing, "
                        f"{len(have - want)} extra")

    # 4. graders must not have crashed or gone silent
    if "evaluator_failures" not in pr:
        problems.append("report has no evaluator_failures field")
    elif pr["evaluator_failures"]:
        problems.append(f"{len(pr['evaluator_failures'])} evaluator failure(s), e.g. {pr['evaluator_failures'][0]}")
    for m in _graded_metrics(spec, brows):
        bad = [r for r in rows if not isinstance(r.get(m), bool)]
        if bad:
            problems.append(f"{len(bad)} row(s) with no boolean value for graded metric {m} "
                            f"(e.g. {bad[0].get('item_id')}@{bad[0].get('seed')})")

    # 5. a row cannot contradict itself: Lab 8's trial_pass already requires the right ledger amount,
    #    the right status (+ policy_denied audit), no foreign ledger row and no forbidden substring.
    for m, bad_value in IMPLIED_BY_PASS.items():
        n = sum(1 for r in rows if r.get("pass_rate") is True and r.get(m) is bad_value)
        if n:
            problems.append(f"{n} row(s) pass pass_rate but {m}={bad_value} (hand-edited rows?)")

    # 6. headline metrics must be what the rows say (no hand-edited aggregate)
    if not problems:
        problems += _metrics_match_rows(pr, spec)

    # 7. slice tags come from the label; relabelling an item hides it from the slice gate
    btags = {r.get("item_id"): sorted(r.get("slices") or []) for r in brows}
    moved = sorted({r.get("item_id") for r in rows
                    if r.get("item_id") in btags and sorted(r.get("slices") or []) != btags[r.get("item_id")]}, key=str)
    if moved:
        problems.append(f"slice tags differ from baseline for {len(moved)} item(s), e.g. {moved[0]}")

    # 8. thresholds themselves must be numbers; ratchet against the spec on main
    problems += _threshold_sanity(spec)
    if base_spec is not None:
        problems += _ratchet(spec, base_spec)
    return problems


# Lab 8's trial_pass checks ledger amount, status + policy_denied audit, foreign ledger rows and
# forbidden substrings, so a row with pass_rate True cannot carry these values.
IMPLIED_BY_PASS = {"refund_amount_correct": False, "decision_correct": False,
                   "approval_bypass": True, "secret_leak": True}


def _finite_number(v: Any) -> bool:
    if isinstance(v, bool):
        return True
    return isinstance(v, (int, float)) and math.isfinite(v)


def _graded_metrics(spec: dict, brows: list[dict]) -> list[str]:
    """Spec metrics that are per-row grader columns (present in the baseline's rows)."""
    from lab9.graders import GRADERS
    ids = [m["id"] for m in spec.get("metrics") or []] + [s.get("metric") for s in spec.get("slices") or []]
    cols = set(brows[0]) if brows else set()
    return sorted({m for m in ids if m in GRADERS and (m in cols or not brows)})


def _metrics_match_rows(pr: dict, spec: dict) -> list[str]:
    rows = pr.get("rows") or []
    if not rows:
        return ["report has no rows"]
    has_cost = all("cost" in r for r in rows)
    ids = [m for m in (pr.get("metrics") or {}) if m in rows[0] and m != "cost"]
    try:
        recomputed = aggregate([{"cost": 0.0, **r} for r in rows], ids, k_values=spec.get("pass_k", []))
    except Exception as e:  # noqa: BLE001 -- a report we cannot recompute is a report we cannot trust
        return [f"cannot recompute metrics from rows: {e}"]
    out = []
    for k, v in (pr.get("metrics") or {}).items():
        if k not in recomputed or (k == "cost_per_success" and not has_cost):
            continue
        r = recomputed[k]
        if (r is None) != (v is None) or (r is not None and abs(float(v) - float(r)) > 1e-3):
            out.append(f"metric {k} = {v} but the rows give {r}")
    return out


def _threshold_sanity(spec: dict) -> list[str]:
    out = []
    for m in [*(spec.get("metrics") or []), *(spec.get("slices") or [])]:
        th = m.get("threshold", m)
        for key in ("min", "max", "max_increase"):
            if key in th and not _finite_number(th[key]):
                out.append(f"{m.get('id') or m.get('name')}: threshold {key}={th[key]!r} is not a finite number")
    return out


def _looser(base_th: dict, th: dict) -> list[str]:
    out = []
    for key, worse in (("max", lambda new, old: new > old), ("max_increase", lambda new, old: new > old),
                       ("min", lambda new, old: new < old)):
        if key in base_th:
            if key not in th or not _finite_number(th[key]):
                out.append(f"{key} removed")
            elif worse(th[key], base_th[key]):
                out.append(f"{key} {base_th[key]} -> {th[key]}")
    if base_th.get("no_significant_drop") and not th.get("no_significant_drop"):
        out.append("no_significant_drop dropped")
    return out


def _ratchet(spec: dict, base_spec: dict) -> list[str]:
    """Same eval set => thresholds may only get stricter. The PR is compared with the same baseline
    (same eval_set_sha), so a bumped eval_set_version in the PR spec does not reset the ratchet."""
    out = []
    for key in ("eval_set", "labels", "adversarial_pack", "eval_set_version"):
        if base_spec.get(key) != spec.get(key):
            out.append(f"ratchet: {key} {base_spec.get(key)!r} -> {spec.get(key)!r} on the same baseline")
    if sorted(spec.get("seeds") or []) != sorted(base_spec.get("seeds") or []):
        out.append(f"ratchet: seeds {base_spec.get('seeds')} -> {spec.get('seeds')}")
    if (spec.get("bootstrap") or {}) != (base_spec.get("bootstrap") or {}):
        out.append(f"ratchet: bootstrap settings changed {base_spec.get('bootstrap')} -> {spec.get('bootstrap')}")
    missing_k = sorted(set(base_spec.get("pass_k") or []) - set(spec.get("pass_k") or []))
    if missing_k:
        out.append(f"ratchet: pass_k {missing_k} removed")
    new = {m["id"]: m for m in spec.get("metrics") or []}
    for bm in base_spec.get("metrics") or []:
        m = new.get(bm["id"])
        if m is None:
            out.append(f"ratchet: metric {bm['id']} removed")
            continue
        if bm.get("blocking") and not m.get("blocking"):
            out.append(f"ratchet: metric {bm['id']} no longer blocking")
        if bm.get("kind") != m.get("kind"):
            out.append(f"ratchet: metric {bm['id']} kind {bm.get('kind')} -> {m.get('kind')}")
        out += [f"ratchet: metric {bm['id']} {x}" for x in _looser(bm.get("threshold") or {}, m.get("threshold") or {})]
    new_sl = {(s.get("name"), s.get("metric")): s for s in spec.get("slices") or []}
    for bs in base_spec.get("slices") or []:
        sid = f"{bs.get('name')}/{bs.get('metric')}"
        sl = new_sl.get((bs.get("name"), bs.get("metric")))
        if sl is None:
            out.append(f"ratchet: slice {sid} removed")
            continue
        if bs.get("blocking") and not sl.get("blocking"):
            out.append(f"ratchet: slice {sid} no longer blocking")
        out += [f"ratchet: slice {sid} {x}" for x in _looser(bs, sl)]
    new_ir = {r.get("metric"): r for r in spec.get("item_regression") or []}
    for br in base_spec.get("item_regression") or []:
        r = new_ir.get(br.get("metric"))
        if r is None:
            out.append(f"ratchet: item_regression {br.get('metric')} removed")
            continue
        if br.get("blocking") and not r.get("blocking"):
            out.append(f"ratchet: item_regression {br.get('metric')} no longer blocking")
        cap, bcap = r.get("max_regressed_items", 0), br.get("max_regressed_items", 0)
        if not _finite_number(cap) or cap > bcap:
            out.append(f"ratchet: item_regression {br.get('metric')} max_regressed_items {bcap} -> {cap}")
    return out


def slice_check(pr: dict, base: dict, spec: dict) -> tuple[list[str], list[str]]:
    """M5 (Advanced): an average can hide a regression on a small, important slice.
    For each entry of spec["slices"] ({"name", "metric", "min"?, "no_significant_drop"?, "blocking"}):
      - value = mean of per_item(pr, metric, slice_name=name)
      - fail if below "min", or if no_significant_drop and the paired CI over the slice's items
        lies entirely below 0
    Return (blocked ids like "slice:money_movement/pass_rate", report lines)."""
    blocked, lines = [], []
    bs = spec.get("bootstrap", {})
    for sl in spec.get("slices") or []:
        name, metric = sl["name"], sl["metric"]
        sid = f"slice:{name}/{metric}"
        a, b = per_item(pr, metric, slice_name=name), per_item(base, metric, slice_name=name)
        verdict, note, val = "ok", "", None
        if not a:
            verdict = "missing"                       # a slice with no rows cannot vouch for anything
        else:
            val = round(sum(a.values()) / len(a), 4)
            if "min" in sl and val < sl["min"]:
                verdict = "fail"
            if "max" in sl and val > sl["max"]:
                verdict = "fail"
            if sl.get("no_significant_drop"):
                keys = sorted(set(a) & set(b))
                if keys:
                    d, lo, hi = stats.paired_bootstrap([a[k] for k in keys], [b[k] for k in keys],
                                                       n_resamples=bs.get("n_resamples", 1000), rng=bs.get("rng", 2027))
                    note = f"Δ {d:+.3f} CI [{lo:+.3f}, {hi:+.3f}]"
                    if hi < 0:
                        verdict = "fail"
        if verdict != "ok" and sl.get("blocking"):
            blocked.append(sid)
        th = {k: sl[k] for k in ("min", "max", "no_significant_drop") if k in sl}
        label = {"ok": "ok", "missing": "MISSING"}.get(verdict, "BLOCK" if sl.get("blocking") else "warn")
        lines.append(f"  {sid:<34} {val!s:<8} n={len(a):<3} {json.dumps(th):<36} {note:<28} {label}")
    return blocked, lines


def check(pr: dict, base: dict, spec: dict, base_spec: dict | None = None) -> tuple[list[str], list[str]]:
    if pr["manifest"]["eval_set_sha"] != base["manifest"]["eval_set_sha"]:
        return ["eval_set_sha"], ["  EVAL SET differs from the baseline's: re-run the baseline on the same eval set"]
    problems = integrity_check(pr, base, spec, base_spec)
    if problems:
        return ["integrity"], [f"  INTEGRITY {p}" for p in problems]
    blocked, lines = [], []
    bs = spec.get("bootstrap", {})
    for m in spec["metrics"]:
        mid, th, val = m["id"], m.get("threshold", {}), pr["metrics"].get(m["id"])
        verdict, note = "ok", ""
        if val is None:
            verdict = "missing"
        elif "max" in th and val > th["max"]:
            verdict = "fail"
        elif "min" in th and val < th["min"]:
            verdict = "fail"
        elif "max_increase" in th and base["metrics"].get(mid):
            if val > base["metrics"][mid] * (1 + th["max_increase"]):
                verdict = "fail"
        if th.get("no_significant_drop") and val is not None:
            a, b = per_item(pr, mid), per_item(base, mid)
            keys = sorted(set(a) & set(b))
            d, lo, hi = stats.paired_bootstrap([a[k] for k in keys], [b[k] for k in keys],
                                               n_resamples=bs.get("n_resamples", 1000), rng=bs.get("rng", 2027))
            note = f"Δ {d:+.3f} CI [{lo:+.3f}, {hi:+.3f}]"
            if hi < 0:
                verdict = "fail"
        if verdict != "ok" and m.get("blocking"):
            blocked.append(mid)
        label = {"ok": "ok", "missing": "MISSING"}.get(verdict, "BLOCK" if m.get("blocking") else "warn")
        lines.append(f"  {mid:<22} {val!s:<8} {json.dumps(th):<40} {note:<28} {label}")
    if spec.get("slices"):
        sb, sl = slice_check(pr, base, spec)
        blocked += sb
        lines += sl
    if spec.get("item_regression"):
        ib, il = item_regression_check(pr, base, spec)
        blocked += ib
        lines += il
    return blocked, lines


def item_regression_check(pr: dict, base: dict, spec: dict) -> tuple[list[str], list[str]]:
    """Paired, per item: an item whose seed-mean on `metric` is LOWER in the PR than in the baseline
    has regressed. A mean (or a slice mean) can stay flat when a PR fixes the baseline's failures on
    some items and breaks others; this check does not net them out. The allowed count must sit
    outside the A/A band: on MockLLM the A/A delta is exactly 0, so max_regressed_items is 0. With a
    real LLM, set it from the A/A distribution of regressed-item counts on two seed sets."""
    blocked, lines = [], []
    for rule in spec.get("item_regression") or []:
        metric, cap = rule["metric"], rule.get("max_regressed_items", 0)
        a, b = per_item(pr, metric), per_item(base, metric)
        worse = sorted(k for k in set(a) & set(b) if a[k] < b[k] - 1e-9)
        rid = f"items:{metric}"
        fail = len(worse) > cap
        if fail and rule.get("blocking"):
            blocked.append(rid)
        label = ("BLOCK" if rule.get("blocking") else "warn") if fail else "ok"
        shown = ", ".join(f"{k} {b[k]:.1f}->{a[k]:.1f}" for k in worse[:4]) + (" …" if len(worse) > 4 else "")
        lines.append(f"  {rid:<34} {len(worse)} regressed item(s), max {cap}  {shown:<40} {label}")
    return blocked, lines


def main(argv: list[str] | None = None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser(prog="lab9.gate")
    ap.add_argument("--pr", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--spec", default=str(SPEC))
    ap.add_argument("--base-spec", help="metric spec on main, for the ratchet check (M7)")
    args = ap.parse_args(argv)
    pr = json.loads(Path(args.pr).read_text(encoding="utf-8"))
    base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    base_spec = load_spec(Path(args.base_spec)) if args.base_spec else None
    blocked, lines = check(pr, base, load_spec(Path(args.spec)), base_spec)
    print(f"gate: PR {pr['run_id']} vs main {base['run_id']} · eval set {pr['manifest']['eval_set_sha']}")
    print("\n".join(lines))
    if blocked:
        print(f"exit 1 — blocked: {', '.join(blocked)}")
        return 1
    print("exit 0 — merge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
