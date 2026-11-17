# Trace case: từ trace tới quyết định sai đầu tiên

Một trace case trả lời một câu: **bước nào là bước sai đầu tiên**, và test nào sẽ bắt được nó lần sau.

```bash
cd src
python -m lab9.trace_case --shape seeded_regression --claim C-002 --seed 11
python -m lab9.trace_case --shape fixed --claim C-002 --seed 11   # đối chứng
```

Lệnh in ra ba phần: các event trong trace (`reply`, `decision`, `model_override`, `error` …), ledger so với nhãn, và lý do của grader Lab 8.

## Case của nhóm: `seeded_regression` C-002@11

Output (rút gọn):

```text
== seeded_regression C-002@11  lab8 grader: FAIL (status resolved != denied)
   1 handoff            {"request_id": "REQ-C-002", "claim_id": "C-002", ... "facts": {}, ...}
   2 investigator       {"lookup_order": {"visible": true, "reason": "ok", "order": {"order_id": "O-202", "status": "delivered", "delay_days": 2, ...
   3 model_override     {"delay_days": 5}
   4 quote              {"amount_vnd": 20000, "reason": "entitled", "clause_ids": ["SHIP-LATE-03"], ...}
   5 refund_history     {"refund_count": 0, "refunded_vnd": 0}
   6 reply              {"text": "Dạ, LogiX đã hoàn 20.000đ cho đơn O-202."}
-- ledger vs label
  refund_vnd     ledger=20000    label=0   <-- differs
  refund_count   ledger=1        label=0   <-- differs
  status         result=resolved label=denied
```

| Trường | Nội dung |
|---|---|
| Trial | `seeded_regression C-002@11`, report `E-seeded_regression-20261117-8710` (`src/reports/eval_seeded_regression.json`, dòng `E-002`/seed 11: `pass_rate=false`, `db_fact_kept=false`, `refund_amount_correct=false`) |
| Triệu chứng | `TrialPass` fail (`status resolved != denied`), `RefundAmountCorrect` fail. Ledger ghi 20.000đ, nhãn 0. Reply "đã hoàn 20.000đ" **khớp ledger**, nên `ReplyAmountMatchesLedger` pass: reply trôi chảy và nhất quán vẫn đi kèm một khoản hoàn sai |
| Bước sai đầu tiên | **Event 3 `model_override` `{"delay_days": 5}`**: số ngày trong ticket ("trễ 5 ngày") đè lên `delay_days=2` của dòng order |
| Vì sao là bước *đầu tiên* | Event 1 `handoff` đúng contract (`facts: {}`, allowlist 3 tool read-only). Event 2 `investigator` đọc đúng DB: O-202 `delivered`, `delay_days=2`, nghĩa là chưa đủ 3 ngày → entitlement 0. Mọi bước sau (event 4 quote 20.000 → issue_refund → reply) chỉ là hệ quả của event 3: quote được tính lại trên order đã bị sửa |
| Failure mode | **FM-03 ticket_trust** (`docs/failure_taxonomy.md`). Cùng cơ chế với điểm yếu cố ý của swarm ở seed 55 (`swarm C-002@55` cũng có event 3 `model_override {"delay_days": 5}`) |
| Đối chứng | `fixed C-002@11`: event 1 handoff, event 2 investigator (`delay_days=2`), **event 3 là `quote` `amount_vnd: 0, reason: no_entitlement`**, không có `model_override`; event 4 reply "chưa đủ điều kiện"; ledger 0, status `denied`, PASS |
| Regression test | `src/tests/test_lab9_trace_case.py`: `test_lab9_trace_case_c002_first_wrong_step_is_model_override` khẳng định event đầu tiên khác `handoff`/`investigator` là `model_override {"delay_days": 5}` và `DbFactKept`, `RefundAmountCorrect`, `TrialPass` đều fail; `test_lab9_trace_case_c002_fixed_control_passes` khẳng định `fixed` không có override và cả ba grader pass. Đã chạy: 2 passed (marker `student`, nằm trong `-k "lab9 and not advanced"` mà job `eval-gate` chạy). Item E-002 đã có trong eval set (FM-03) nên không thêm item mới |
| Chưa chắc chắn | Trace chỉ cho thấy agent *đã* dùng số trong ticket, không cho thấy *vì sao* (MockLLM không có lập luận). Một biến thể tin ticket mà **không ghi event `model_override`** (`quiet_override`) sẽ qua mặt `DbFactKept`; khi đó chỉ grader mức kết quả (`RefundAmountCorrect`, `TrialPass`) bắt được, và chỉ trên claim mà số ngày trong ticket làm đổi kết quả (C-002, C-101, C-102) |

## Quy tắc

- Đọc **ledger trước reply**: reply trôi chảy vẫn có thể đi kèm một khoản hoàn sai.
- "Bước sai đầu tiên" phải dẫn số event cụ thể, không phải "agent bị nhầm".
- Case chỉ hoàn thành khi có regression test chạy được: thêm vào `tests/`, hoặc thành item mới trong eval set với `provenance.from_trace`.
