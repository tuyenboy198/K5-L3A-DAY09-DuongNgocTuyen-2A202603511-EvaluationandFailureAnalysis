"""Regression test for the trace case in docs/trace_case.md: seeded_regression C-002@11.

First wrong step = event 3 `model_override {"delay_days": 5}`: the ticket's "trễ 5 ngày" replaced the
order row's delay_days=2 (FM-03 ticket_trust). DbFactKept must flag it on the buggy variant and stay
green on `fixed`; the ledger tells the same story (20.000đ written where the label says 0).
"""
import pytest

pytestmark = pytest.mark.student


def _grade(shape: str, claim: str, seed: int):
    from pydantic_evals.evaluators import EvaluatorContext

    from lab9 import graders, trace_case

    case = trace_case.investigate(shape, claim, seed)
    ctx = EvaluatorContext(name=f"{shape}-{claim}@{seed}", inputs={}, metadata=None,
                           expected_output=case["label"], output=case["trial"], duration=0.0,
                           _span_tree=None, attributes={}, metrics={})
    return case, {name: cls().evaluate(ctx).value for name, cls in graders.GRADERS.items()}


def test_lab9_trace_case_c002_first_wrong_step_is_model_override():
    case, scores = _grade("seeded_regression", "C-002", 11)
    kinds = [e.get("kind") for e in case["trial"]["events"]]
    first = kinds.index("model_override")
    assert kinds[:first] == ["handoff", "investigator"]          # steps 1-2 read the DB correctly
    assert case["trial"]["events"][first]["payload"] == {"delay_days": 5}
    assert scores["db_fact_kept"] is False                       # step-level grader catches it
    assert scores["refund_amount_correct"] is False              # ledger 20000 vs label 0
    assert scores["pass_rate"] is False


def test_lab9_trace_case_c002_fixed_control_passes():
    case, scores = _grade("fixed", "C-002", 11)
    assert "model_override" not in [e.get("kind") for e in case["trial"]["events"]]
    assert scores["db_fact_kept"] is True
    assert scores["refund_amount_correct"] is True
    assert scores["pass_rate"] is True
    assert case["trial"]["snapshot"]["refund_vnd"] == 0
