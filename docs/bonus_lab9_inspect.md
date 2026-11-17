# Bonus Lab 9 — port grader sang Inspect AI và so khớp

## Làm gì
Viết lại 7 grader của Lab 9 thành `@scorer` của **Inspect AI 0.3.290**, chạy cùng eval set, cùng nhãn, cùng seed, cùng shape. Sau đó so **từng ô (item, seed, metric)** với report của pydantic-evals đã commit.

- Code: `src/bonus/lab9_inspect.py`. Test: `src/tests/test_bonus_lab9_inspect.py` (3 test, tự skip nếu chưa cài inspect-ai).
- Cài: `pip install -r requirements-bonus.txt`. File này tách riêng để job CI `eval-gate` không phải cài inspect-ai.
- Mỗi `(item, seed)` là một `Sample`. Nhãn nằm trong `target`, không nằm trong `input`. Một `@solver` chạy shape trên SQLite mới. Model là `mockllm/model` có sẵn của Inspect: không cần key, và không có lời gọi model nào được chấm, vì agent dùng MockLLM của Lab 8.
- Các scorer được **viết lại từ định nghĩa**, không import `lab9.graders`. Grader FM-08 có regex đọc số tiền riêng. Nhờ vậy, nếu hai bên lệch nhau thì chắc chắn một trong hai bản cài đặt sai.
- Quy ước: Inspect dùng `CORRECT` = "trial ổn". Hai metric an toàn (`approval_bypass`, `secret_leak`) được đổi ngược lại khi so, vì cột của pydantic-evals lưu "có vi phạm".

## Lệnh tái lập
```bash
cd src
python -m bonus.lab9_inspect --shapes swarm,fixed,seeded_regression
python -m bonus.lab9_inspect --spec config/metric_spec_v2.yaml --shapes swarm,fixed,honest_refactor,flaky_regression
```

## Kết quả (`reports/bonus_inspect_v1.json`, `reports/bonus_inspect_v2.json`)

| Spec | Shape | Report pydantic-evals | Ô khớp |
|---|---|---|---|
| v1 | swarm | `E-swarm-20261117-8710` | 490/490 |
| v1 | fixed | `E-fixed-20261117-8710` | 490/490 |
| v1 | seeded_regression | `E-seeded_regression-20261117-8710` | 490/490 |
| v2 | swarm | `E-swarm-20261117-e357` | 980/980 |
| v2 | fixed | `E-fixed-20261117-e357` | 980/980 |
| v2 | honest_refactor | `E-honest_refactor-20261117-e357` | 980/980 |
| v2 | flaky_regression | `E-flaky_regression-20261117-e357` | 980/980 |
| | **Tổng** | | **5390/5390 (100%)** |

Mỗi report có 7 metric: pass_rate, secret_leak, approval_bypass, refund_amount_correct, db_fact_kept, reply_amount_matches, decision_correct. Không có dòng nào bị thiếu.

## Nhận xét
- Kết quả khớp 100% vì cả hai harness đọc cùng một `Trial` (ledger, trace, kết quả) do cùng host Lab 8 sinh ra. Phép so này kiểm **logic grader** chứ không kiểm agent: nó xác nhận grader FM-08 và `DecisionCorrect` không phụ thuộc vào cách cài đặt.
- Khác biệt về harness: pydantic-evals gọn hơn cho pipeline gate, vì `Dataset`/`Case`/`Evaluator` chạy đồng bộ và report tự viết. Inspect cho log `.eval` xem được bằng `inspect view`, có sẵn scorer metric (`mean`), và thay `mockllm/model` bằng model thật chỉ cần một tham số. Cái giá là phải chạy async, và log được đọc lười: phải đọc `log.samples` trước khi xoá thư mục log.
- Giới hạn: vẫn là MockLLM. Chưa port judge `tone_ok` sang `model_graded_qa` của Inspect, vì judge đang chạy ở chế độ replay từ CSV.
