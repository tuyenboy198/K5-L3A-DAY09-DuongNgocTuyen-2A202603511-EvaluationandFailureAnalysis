# Judge audit

Chạy: `python -m lab9.judge --csv <file> --audit 5`

Lớp dương (positive) = `FAIL`: việc của judge là **tìm ra** reply hỏng. Số liệu dưới đây là output của `lab9.judge` (replay, không cần key).

| Tiêu chí | n | κ | Precision | Recall | Đạt FLOORS (κ≥0.6, P≥0.8, R≥0.7)? | Quyết định |
|---|---|---|---|---|---|---|
| tone_ok | 100 | 0.757 | 0.857 | 0.800 | **Đạt** (exit 0) | **dùng** (chỉ cho tone, không cho tiền) |
| helpful | 100 | 0.375 | 1.000 | 0.300 | **Không đạt**: κ < 0.6 và R < 0.7 (exit 1) | **loại** |

Ma trận nhầm lẫn:

| | tp | fp | fn | tn | Tỉ lệ FAIL quan sát → hiệu chỉnh Rogan-Gladen |
|---|---|---|---|---|---|
| tone_ok | 24 | 4 | 6 | 66 | 0.28 → 0.30 |
| helpful | 9 | 0 | 21 | 70 | 0.09 → 0.30 |

## ≥5 case audit thủ công

Tone (10 bất đồng, audit cả 10) và helpful (21 bất đồng, audit 4 đại diện).

| # | Case | Judge nói | Người nói | Ai đúng? | Ghi chú / sửa rubric |
|---|---|---|---|---|---|
| 1 | H-008 "Em đã hoàn tiền cho anh rồi nha, anh check giúp em." | PASS | FAIL | Người | Xưng "em" + "nha" quá suồng sã với khách khiếu nại. **Rubric:** thêm luật xưng hô "LogiX ↔ anh/chị", cấm "em/bạn/nha/nhé" trong kênh CS |
| 2 | H-017 "Dạ, bạn ơi LogiX đã hoàn 25.000đ … rồi nha." | PASS | FAIL | Người | Cùng lỗi xưng hô ("bạn ơi"). Luật ở #1 bắt được |
| 3 | H-029 "Dạ, vui lòng chờ. … không cần nhắn thêm." | PASS | FAIL | Người | Judge chỉ thấy "Dạ, vui lòng" lịch sự bề mặt. **Rubric:** câu mệnh lệnh/cắt ngang khách ("không cần nhắn thêm") = FAIL dù có kính ngữ |
| 4 | H-042 "Dạ, đơn O-808 đã hoàn 120.000đ. Tiền về trong 3–5 ngày ạ." | FAIL | PASS | Người | Judge phạt vì "thiếu xin lỗi". **Rubric:** xin lỗi không bắt buộc khi đã giải quyết xong; ngắn gọn, đủ ý = PASS |
| 5 | H-051 "Dạ, LogiX đã refund 90.000đ …" | FAIL | PASS | Người | CS LogiX cho phép từ "refund". **Rubric:** whitelist thuật ngữ được phép (refund, ship, COD) |
| 6 | H-063 "… đã hoàn 20.000đ và sẽ hoàn thêm cho anh/chị ạ." | PASS | FAIL | Người | Hứa "sẽ hoàn thêm" ngoài kết quả hệ thống. Đây là lỗi **nội dung** (lời hứa về tiền), không phải tone. **Rubric:** tách ra, lời hứa về tiền chấm bằng code (FM-08 `reply_amount_matches`), không giao cho judge tone |
| 7 | H-070 "… đã hoàn 90.000đ cho đơn O-909 ạ 😂😂" | PASS | FAIL | Người | Emoji cười trong ngữ cảnh khiếu nại. **Rubric:** cấm emoji trong reply khiếu nại |
| 8 | H-081 "Dạ, đơn O-107 chưa đủ điều kiện hoàn theo chính sách hiện hành ạ." | FAIL | PASS | **Judge** (theo note) | Note: nhãn người sai do đọc nhầm ticket. **Hành động:** sửa nhãn trong bản CSV tiếp theo (không sửa file public đang dùng làm tham chiếu); khi sửa, tp tăng 1, fp giảm 1, P tone tăng |
| 9 | H-088 "Dạ, dạ, LogiX đã hoàn 40.000đ …" | FAIL | PASS | Người (tranh cãi) | Lặp "Dạ" do gõ lỗi: khó chịu nhẹ, không xúc phạm. **Rubric:** lỗi gõ nhỏ không đổi nghĩa = PASS; ghi rõ ví dụ |
| 10 | H-094 "Da, LogiX da hoan 40.000đ cho don O-341, xin loi …" | PASS | FAIL | Người | Mất dấu tiếng Việt (dịch máy). **Rubric:** reply không dấu = FAIL |
| 11 | HL-015 "Dạ, LogiX đã hoàn 55.000đ cho đơn O-112 ạ." (ledger 40.000) | PASS | FAIL | Người | Judge không thấy ledger nên không thể biết số sai. Số tiền phải chấm bằng code, không phải judge |
| 12 | HL-043 "… rất tiếc … xin lỗi … đã hoàn 35.000đ cho đơn O-202 …" (ledger 20.000) | PASS | FAIL | Người | Reply dài, nhiều câu xin lỗi → judge "helpful" bị lời văn đánh lừa (verbosity bias) trong khi số tiền sai |
| 13 | HL-047 "Ok bạn, đơn O-515 xử lý rồi nhé." | PASS | FAIL | Người | Không nói kết quả, không nói số tiền: vô ích cho khách. Judge vẫn PASS |
| 14 | HL-098 "… xin lỗi … đã hoàn 120.000đ cho đơn O-808 …" (ledger 90.000) | PASS | FAIL | Người | Cùng mẫu với #12 |

## Kết luận

- **tone_ok: dùng được.** κ=0.757, P=0.857, R=0.800, đều trên sàn. Judge tone chỉ được dùng cho tone/xưng hô, theo dõi (không chặn release), và không chấm tiền hay approval. Trong 10 bất đồng có 6 case judge bỏ sót lỗi xưng hô/giọng điệu (fn) và 4 case judge phạt quá tay (fp), trong đó 1 case (H-081) là nhãn người sai. Cần sửa rubric như bảng trên rồi calibrate lại.
- **helpful: loại, không được dùng làm gate.** Ma trận nhầm lẫn: tp=9, fp=0, fn=21, tn=70. Precision 1.0 trông đẹp nhưng chỉ vì judge gần như không bao giờ nói FAIL: nó bỏ sót 21/30 reply hỏng (R=0.30), κ=0.375. Tỉ lệ FAIL judge thấy là 9%, Rogan-Gladen hiệu chỉnh ra 30%, tức judge báo **thấp hơn thực tế hơn 3 lần**. 18/21 case bỏ sót là reply nói **sai số tiền so với ledger** (ghi trong cột note), phần lớn bọc trong nhiều câu xin lỗi (verbosity bias). Judge không đọc ledger nên về cấu trúc không thể chấm tiêu chí này. 3 case còn lại là reply cụt "Ok bạn, đơn … xử lý rồi nhé" (HL-047, HL-091, HL-097). Phần "helpful = nói đúng số tiền" đã được thay bằng grader code `ReplyAmountMatchesLedger` (FM-08) và `RefundAmountCorrect`.
- Rủi ro còn lại: judge chỉ được kiểm trên dữ liệu replay đã công khai (100 dòng mỗi tiêu chí); rubric đã sửa phải được calibrate trên **nhãn người mới, held-out** trước khi tin. Một case nhãn người sai (H-081) cho thấy chính nhãn người cũng cần kiểm chéo bởi 2 người.
