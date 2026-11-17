"""Bonus: Lab 9 graders ported to Inspect AI agree with the pydantic-evals graders.
Skipped when inspect-ai is not installed (it is not in requirements-lab9.txt)."""
import json
from pathlib import Path

import pytest

pytest.importorskip("inspect_ai")
pytest.importorskip("pydantic_evals")


@pytest.fixture(autouse=True)
def _restore_shapes():
    """variants.register() adds lab9 shapes to lab8.shapes.SHAPES; undo it so Lab 8's contract
    tests (exactly four shapes) still see a clean registry in the same pytest process."""
    from lab8.shapes import SHAPES

    saved = dict(SHAPES)
    yield
    SHAPES.clear()
    SHAPES.update(saved)


def test_bonus_reply_amount_parsers_agree():
    from bonus.lab9_inspect import reply_amounts
    from lab9.graders import stated_amounts

    texts = ["Dạ, LogiX đã hoàn 35.000đ cho đơn O-107.", "LogiX đã hoàn phí 120.000đ cho đơn O-515 của anh/chị ạ.",
             "Dạ, đơn O-202 chưa đủ điều kiện hoàn theo chính sách hiện hành.", "hoàn 20,000 VND", "35k đồng",
             "đơn trễ 5 ngày, 1 đơn hàng", "Dạ, yêu cầu đã được chuyển cho chuyên viên xử lý."]
    for t in texts:
        assert reply_amounts(t) == stated_amounts(t), t


@pytest.mark.parametrize("shape", ["fixed", "seeded_regression"])
def test_bonus_inspect_matches_pydantic_evals_report(shape):
    from bonus.lab9_inspect import compare
    from lab9.eval import SPEC, load_spec

    report = Path(f"reports/eval_{shape}.json")
    if not report.is_file():
        pytest.skip(f"{report} not generated")
    spec = load_spec(SPEC)
    if json.loads(report.read_text(encoding="utf-8"))["manifest"]["metric_spec_version"] != spec["metric_spec_version"]:
        pytest.skip("report from another spec version")
    res = compare(shape, spec, report)
    assert res["missing_rows"] == 0 and res["cells"] == 70 * 7
    assert res["agree"] == res["cells"], res["diffs"][:5]
