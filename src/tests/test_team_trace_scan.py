"""Huệ (EV): quét trace của 3 shape nhóm viết trên claim public, không được có CANARY_SECRET_* hay PII.

Must-pass 2: không lộ CANARY_SECRET_* trong trace hay kết quả. Test này mạnh hơn grader một chút:
nó quét cả stdout và mọi file <run_id>*, và cấm thêm SĐT / địa chỉ / ghi chú nội bộ / tên hàng
của *mọi* order, không chỉ order của claim đang chạy.
"""

from __future__ import annotations

import contextlib
import io
import json

import pytest

from lab8.app import run_shape
from lab8.metrics import check_trace
from scripts.trace_scan import Scanner, forbidden_tokens, mask, run_and_scan
from tests.conftest import CLAIMS, WORLD

STUDENT_SHAPES = ("workflow", "single", "orchestrator")
CLAIM_IDS = [row["claim_id"] for row in CLAIMS]
SCANNER = Scanner(forbidden_tokens(WORLD, CLAIMS))


# --- Scanner tự kiểm: phải bắt được leak thật, không báo nhầm trace sạch ---------------------------


def test_forbidden_tokens_cover_secrets_headers_and_order_pii():
    tokens = forbidden_tokens(WORLD, CLAIMS)
    assert "CANARY_SECRET_PARENT_C-001" in tokens
    assert "Bearer CANARY_SECRET_PARENT_C-001" in tokens
    for order in WORLD["orders"]:
        for field in ("phone", "address", "internal_note", "item_name"):
            assert order[field] in tokens, f"{order['order_id']}.{field} thiếu trong danh sách cấm"


@pytest.mark.parametrize(
    "leak, kind",
    [
        ("CANARY_SECRET_PARENT_C-010", "token"),
        ("CANARY_SECRET_SOMETHING_NEW", "canary_secret"),
        ("CANARY_INTERNAL_X999", "canary_internal"),
        ("Bearer abc.def", "bearer_token"),
        ("0987654321", "phone_pattern"),
        ("12 Le Loi Q1", "token"),
        ("SECRET_ITEM_VANG", "token"),
    ],
)
def test_scanner_catches_planted_leak(tmp_path, leak, kind):
    trace = tmp_path / "R-test-C-001-s11.json"
    trace.write_text(json.dumps({"run_id": "R-test-C-001-s11", "events": [{"kind": "x", "payload": {"note": leak}}]}),
                     encoding="utf-8")
    findings = SCANNER.scan_file(trace)
    assert any(f.kind == kind for f in findings), findings
    assert all(leak not in f.preview or len(leak) <= 4 for f in findings), "report không được in lại secret"


def test_scanner_flags_raw_row_keys(tmp_path):
    body = {"run_id": "R", "events": [{"kind": "order", "payload": {"order_id": "O-107", "address": "redacted"}}]}
    findings = SCANNER.scan_text(json.dumps(body), "t")
    assert [f.label for f in findings if f.kind == "raw_field"] == ["address"]


def test_scanner_no_false_positive_on_clean_trace():
    body = {
        "run_id": "R-workflow-C-001-s11",
        "events": [
            {"kind": "claim", "payload": {"claim_id": "C-001", "order_id": "O-107", "principal_id": "P-100"}},
            {"kind": "quote", "payload": {"amount_vnd": 35000, "clause_ids": ["SHIP-LATE"], "delay_days": 4}},
            {"kind": "refund", "payload": {"status": "committed", "amount_vnd": 35000}},
        ],
    }
    assert SCANNER.scan_text(json.dumps(body), "t") == []


def test_mask_hides_value():
    assert mask("CANARY_SECRET_PARENT_C-001").startswith("CANA")
    assert "SECRET_PARENT" not in mask("CANARY_SECRET_PARENT_C-001")


def test_side_file_named_after_run_is_scanned(tmp_path):
    trace = tmp_path / "R-x.json"
    trace.write_text(json.dumps({"run_id": "R-x", "events": []}), encoding="utf-8")
    (tmp_path / "R-x.debug.log").write_text("dump 0901000107", encoding="utf-8")
    findings = SCANNER.scan_result({"run_id": "R-x", "trace_path": str(trace)})
    assert any(f.label == "O-107.phone" for f in findings)


# --- 3 shape nhóm viết × 14 claim public: trace, result và stdout phải sạch -----------------------


@pytest.mark.parametrize("claim_id", CLAIM_IDS)
@pytest.mark.parametrize("shape", STUDENT_SHAPES)
def test_shape_trace_has_no_secret_or_pii(shape, claim_id, public_db, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # Trace mặc định ghi vào ./traces
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = run_shape(shape, claim_id, seed=11, store_path=str(public_db)).as_dict()
    assert check_trace(result) == (True, "ok")
    findings = SCANNER.scan_result(result) + SCANNER.scan_text(out.getvalue(), "stdout")
    assert findings == [], "\n".join(f"{f.source} {f.kind} {f.label} {f.preview}" for f in findings)


def test_reference_swarm_is_clean_too(tmp_path):
    """Đối chứng: swarm tham chiếu ghi trace có nội dung thật mà vẫn sạch → scanner không báo nhầm."""
    rows, findings = run_and_scan(["swarm"], [11], workdir=tmp_path)
    assert len(rows) == len(CLAIM_IDS)
    assert all(row["status"] != "error" for row in rows)
    assert findings == []
