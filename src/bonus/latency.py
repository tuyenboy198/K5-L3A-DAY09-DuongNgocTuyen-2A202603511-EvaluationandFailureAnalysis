"""Simulated latency: node durations from latency.yaml and the critical path of a DAG.

No time.sleep and no wall clock: the same (seed, claim_id, node) always gets the same
duration, so sequential and parallel runs differ only in how nodes depend on each other.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

import yaml

_CONFIG = yaml.safe_load(Path(__file__).with_name("latency.yaml").read_text(encoding="utf-8"))


def node_ms(node_id: str, *, seed: int, claim_id: str) -> float:
    base = float(_CONFIG["node_ms"][node_id])
    unit = int(hashlib.sha256(f"{seed}|{claim_id}|{node_id}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return base * (1 + float(_CONFIG["jitter"]) * (2 * unit - 1))


def durations(nodes: list[dict[str, Any]], *, seed: int, claim_id: str) -> dict[str, float]:
    return {node["id"]: node_ms(node["id"], seed=seed, claim_id=claim_id) for node in nodes}


def critical_path_ms(nodes: list[dict[str, Any]], ms: dict[str, float]) -> float:
    """Longest path through the DAG. `nodes` must be in topological order (as traced)."""
    finish: dict[str, float] = {}
    for node in nodes:
        finish[node["id"]] = ms[node["id"]] + max((finish[dep] for dep in node["deps"]), default=0.0)
    return max(finish.values(), default=0.0)


def p95(values: list[float]) -> float:
    """Nearest-rank 95th percentile."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]
