"""Team QA (Phan Sỹ Hùng — ledger / policy / crash).

Host-level tests for the must-pass branches: approval expired / revoked /
pending, approval bound to its own claim, approval lower than the quote,
per-order cap math (T13 / T14), and the three crash windows.

The shape-level checks (T11 retry, T12 crash + resume) run on every student
shape and skip while that shape still returns ``not_implemented``, so the host
gate stays green before the shape PRs merge and the QA check activates by
itself once they land.
"""

from __future__ import annotations

import pytest

from lab8.db import connect, init_store
from lab8.durability import (
    InjectedCrash,
    committed_audit_count,
    get_refund,
    issue_refund,
)
from lab8.policy import authorize
from lab8.shapes import SHAPES
from tests.conftest import CLAIMS, WORLD

STUDENT_SHAPES = ["workflow", "single", "orchestrator"]


def _policy_denials(conn, claim_id):
    rows = conn.execute(
        "SELECT details_json FROM audit_events WHERE claim_id = ? AND event_type = 'policy_denied'",
        (claim_id,),
    ).fetchall()
    return [row["details_json"] for row in rows]


def _ledger_rows(conn, claim_id):
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM refund_ledger WHERE claim_id = ?", (claim_id,)
    ).fetchone()
    return int(row["n"])


def _add_approval(conn, approval_id, claim_id, amount, *, status="valid", expires="2027-12-31"):
    conn.execute(
        "INSERT INTO approvals (approval_id, claim_id, approved_vnd, status, expires_at) VALUES (?, ?, ?, ?, ?)",
        (approval_id, claim_id, amount, status, expires),
    )


def _add_claim(conn, claim_id, order_id, approval_id, *, principal="P-100"):
    conn.execute(
        """
        INSERT INTO claims (claim_id, principal_id, order_id, ticket, parent_secret, raw_headers_json, approval_id)
        VALUES (?, ?, ?, 'qa ticket', 'CANARY_SECRET_PARENT_QA', '{}', ?)
        """,
        (claim_id, principal, order_id, approval_id),
    )


# ---------------------------------------------------------------------------
# Approval gate
# ---------------------------------------------------------------------------


def test_expired_approval_denies_even_on_retry(public_db):
    """C-007 points to A-007 (expired 2020-01-01): no payout, audit trail instead."""
    conn = connect(public_db)
    first = issue_refund(conn, "C-007", 35000)
    second = issue_refund(conn, "C-007", 35000)
    effect = get_refund(conn, "C-007")
    audits = committed_audit_count(conn, "C-007")
    denials = _policy_denials(conn, "C-007")
    conn.close()
    assert first == {"ok": False, "reason": "approval_expired", "effect": None, "conflict": False}
    assert second["reason"] == "approval_expired"
    assert effect is None
    assert audits == 0
    assert any("approval_expired" in row for row in denials)


@pytest.mark.parametrize("status", ["revoked", "pending", "rejected"])
def test_non_valid_approval_status_is_refused(public_db, status):
    conn = connect(public_db)
    _add_approval(conn, f"A-QA-{status}", f"C-QA-{status}", 35000, status=status)
    _add_claim(conn, f"C-QA-{status}", "O-909", f"A-QA-{status}")
    conn.commit()
    outcome = issue_refund(conn, f"C-QA-{status}", 35000)
    effect = get_refund(conn, f"C-QA-{status}")
    conn.close()
    assert outcome["ok"] is False
    assert outcome["reason"] == f"approval_{status}"
    assert effect is None


def test_approval_of_another_claim_never_pays(public_db):
    """A-011 belongs to C-011. A claim pointing at it is approval_claim_mismatch."""
    conn = connect(public_db)
    _add_claim(conn, "C-QA-BORROW", "O-909", "A-011")
    conn.commit()
    outcome = issue_refund(conn, "C-QA-BORROW", 35000)
    denials = _policy_denials(conn, "C-QA-BORROW")
    conn.close()
    assert outcome["reason"] == "approval_claim_mismatch"
    assert outcome["effect"] is None
    assert any("approval_claim_mismatch" in row for row in denials)


def test_approval_lower_than_quote_caps_the_payout(public_db):
    """C-008: quote 35000 but A-008 approves 20000. Host pays 20000, exactly once."""
    conn = connect(public_db)
    outcome = issue_refund(conn, "C-008", 35000)
    effect = get_refund(conn, "C-008")
    audits = committed_audit_count(conn, "C-008")
    conn.close()
    assert outcome["ok"] is True
    assert int(effect["amount_vnd"]) == 20000
    assert audits == 1


# ---------------------------------------------------------------------------
# Per-order cap math (stacking rule, T13 / T14)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("claim_id", "proposed", "already", "paid", "ok", "reason", "amount"),
    [
        # T13: O-505 already fully refunded by an earlier claim.
        ("C-013", 35000, 35000, 250000, False, "order_already_refunded", 0),
        # T14: larger entitlement later only pays the difference.
        ("C-014", 150000, 30000, 200000, True, "ok", 120000),
        # No earlier refund: full quote within the approval.
        ("C-001", 35000, 0, 250000, True, "ok", 35000),
        # Nothing owed is refused before any cap applies.
        ("C-001", 0, 0, 250000, False, "non_positive_amount", 0),
        # Paid amount is the final ceiling (A-009 approves 500000, order paid 90000).
        ("C-009", 120000, 0, 90000, True, "ok", 90000),
    ],
)
def test_authorize_cap_math(public_db, claim_id, proposed, already, paid, ok, reason, amount):
    conn = connect(public_db)
    decision = authorize(conn, claim_id, proposed, paid_vnd=paid, already_refunded_vnd=already)
    conn.close()
    assert decision.ok is ok
    assert decision.reason == reason
    assert decision.amount_vnd == amount


# ---------------------------------------------------------------------------
# Crash windows: at most one refund whatever the crash point
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "point", ["before_effect", "after_effect_before_checkpoint", "after_checkpoint"]
)
def test_crash_then_resume_pays_exactly_once(public_db, point):
    conn = connect(public_db)
    with pytest.raises(InjectedCrash):
        issue_refund(conn, "C-001", 35000, crash_at=point)
    if point == "before_effect":
        assert get_refund(conn, "C-001") is None, "effect must not exist before the crash point"
    resumed = issue_refund(conn, "C-001", 35000)
    effect = get_refund(conn, "C-001")
    rows = _ledger_rows(conn, "C-001")
    audits = committed_audit_count(conn, "C-001")
    checkpoint = conn.execute(
        "SELECT 1 FROM checkpoints WHERE claim_id = 'C-001' AND step = 'issue_refund'"
    ).fetchone()
    conn.close()
    assert resumed["ok"] is True
    assert rows == 1
    assert int(effect["amount_vnd"]) == 35000
    assert audits == 1
    assert checkpoint is not None


def test_resume_with_a_different_amount_is_a_conflict_not_a_second_refund(public_db):
    conn = connect(public_db)
    with pytest.raises(InjectedCrash):
        issue_refund(conn, "C-001", 35000, crash_at="after_effect_before_checkpoint")
    retried = issue_refund(conn, "C-001", 10000)
    effect = get_refund(conn, "C-001")
    rows = _ledger_rows(conn, "C-001")
    conn.close()
    assert retried["ok"] is False
    assert retried["conflict"] is True
    assert retried["reason"] == "amount_conflict"
    assert rows == 1
    assert int(effect["amount_vnd"]) == 35000, "the committed amount stays"


# ---------------------------------------------------------------------------
# T11 / T12 on every student shape (skips until the shape is implemented)
# ---------------------------------------------------------------------------


def _fresh_store(tmp_path, name):
    store = tmp_path / name
    init_store(store, WORLD, CLAIMS).close()
    return store


@pytest.mark.parametrize("shape", STUDENT_SHAPES)
def test_t11_shape_retry_keeps_one_refund(tmp_path, shape):
    store = _fresh_store(tmp_path, f"{shape}-t11.sqlite")
    first = SHAPES[shape]("C-011", seed=11, store_path=str(store))
    if first.error_reason == "not_implemented":
        pytest.skip(f"{shape} is still a stub; QA check activates when it lands")
    second = SHAPES[shape]("C-011", seed=11, store_path=str(store))
    conn = connect(store)
    rows = _ledger_rows(conn, "C-011")
    audits = committed_audit_count(conn, "C-011")
    conn.close()
    assert first.status == "resolved"
    assert second.status == "resolved"
    assert rows == 1
    assert audits == 1


@pytest.mark.parametrize("shape", STUDENT_SHAPES)
def test_t12_shape_crash_resume_does_not_double_pay(tmp_path, shape):
    probe_store = _fresh_store(tmp_path, f"{shape}-probe.sqlite")
    probe = SHAPES[shape]("C-011", seed=11, store_path=str(probe_store))
    if probe.error_reason == "not_implemented":
        pytest.skip(f"{shape} is still a stub; QA check activates when it lands")
    store = _fresh_store(tmp_path, f"{shape}-t12.sqlite")
    with pytest.raises(InjectedCrash):
        SHAPES[shape](
            "C-012", seed=11, store_path=str(store), crash_at="after_effect_before_checkpoint"
        )
    result = SHAPES[shape]("C-012", seed=11, store_path=str(store))
    conn = connect(store)
    rows = _ledger_rows(conn, "C-012")
    audits = committed_audit_count(conn, "C-012")
    conn.close()
    assert result.status == "resolved"
    assert rows == 1
    assert audits == 1
