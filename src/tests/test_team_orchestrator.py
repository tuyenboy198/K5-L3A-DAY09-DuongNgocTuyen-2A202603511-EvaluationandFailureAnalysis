"""Shape C: the handoff is a security boundary, not a debug convenience."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lab8.db import connect
from lab8.durability import InjectedCrash
from lab8.handoff import ALLOWED_FIELDS, READONLY_TOOLS
from lab8.metrics import ledger_snapshot
from lab8.shapes.orchestrator import run_claim
from tests.conftest import CLAIMS
from tests.test_order_cap_and_approval_binding import _add_approval, _add_claim, _denials


def test_handoff_is_sanitized_and_read_only(public_db):
    handoffs = 0
    for row in CLAIMS:
        result = run_claim(row["claim_id"], seed=11, store_path=str(public_db))
        blob = Path(result.trace_path).read_text(encoding="utf-8")
        assert "CANARY_SECRET_" not in blob, row["claim_id"]
        assert row["ticket"] not in blob, row["claim_id"]
        for event in json.loads(blob)["events"]:
            if event["kind"] == "handoff":
                handoffs += 1
                assert set(event["payload"]) <= ALLOWED_FIELDS
                assert set(event["payload"]["allowed_tools"]) <= READONLY_TOOLS
    assert handoffs > 0


@pytest.mark.parametrize("point", ["before_effect", "after_effect_before_checkpoint", "after_checkpoint"])
def test_every_crash_point_resumes_to_one_refund(public_db, point):
    with pytest.raises(InjectedCrash):
        run_claim("C-012", seed=11, store_path=str(public_db), crash_at=point)
    result = run_claim("C-012", seed=11, store_path=str(public_db))
    conn = connect(public_db)
    try:
        snap = ledger_snapshot(conn, "C-012")
    finally:
        conn.close()
    assert (result.status, result.proposal_amount) == ("resolved", 35000)
    assert (snap["refund_count"], snap["refund_vnd"], snap["committed_audit_count"]) == (1, 35000, 1)


def _deny_case(public_db, claim_id, order_id, approval_id, setup=None):
    conn = connect(public_db)
    if setup:
        setup(conn)
    _add_claim(conn, claim_id, order_id, approval_id)
    conn.commit()
    conn.close()
    result = run_claim(claim_id, seed=11, store_path=str(public_db))
    conn = connect(public_db)
    try:
        denials = _denials(conn, claim_id)
    finally:
        conn.close()
    assert result.status == "denied"
    assert result.proposal_amount > 0  # full quote, host denied it
    assert result.refund_effects == []
    assert "CANARY_SECRET_" not in Path(result.trace_path).read_text(encoding="utf-8")
    return denials


def test_approval_of_another_claim_is_denied(public_db):
    denials = _deny_case(public_db, "C-BORROW", "O-808", "A-001")  # A-001 belongs to C-001
    assert any("approval_claim_mismatch" in row for row in denials)


def test_revoked_approval_is_denied(public_db):
    setup = lambda conn: _add_approval(conn, "A-REVOKED", "C-RV", 35000, status="revoked")
    denials = _deny_case(public_db, "C-RV", "O-909", "A-REVOKED", setup)
    assert any("approval_revoked" in row for row in denials)
