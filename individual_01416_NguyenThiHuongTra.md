# Báo cáo cá nhân — Day 9 Multi-Agent A2A

## 1. Thông tin cá nhân

| Thông tin | Nội dung |
|---|---|
| Họ và tên | Nguyễn Thị Hương Trà |
| MSSV | 2A202601416 |
| Khóa/Lớp | K4 |
| Vai trò chính | Multi-agent workflow và deterministic verification |
| Ngày hoàn thành | 2026-08-05 |

## 2. Vai trò và phạm vi công việc

| Module/deliverable | File/hàm phụ trách | Input | Output | Trạng thái |
|---|---|---|---|---|
| Data routing | `repository.py/OlistRepository.route_case` | input JSON + Olist CSV | ba packet + canonical index | Hoàn thành |
| Domain investigation | `agents.py` | packet theo domain | ba structured report | Hoàn thành |
| Policy và verification | `agents.py`, `verifier.py` | investigation bundle + rules | draft + validation errors | Hoàn thành |
| Orchestration và trace | `orchestrator.py`, `trace.py`, `cli.py` | 50 cases | output JSON + trace JSONL | Hoàn thành |
| Kiểm thử và tài liệu | `tests/`, `architecture.md` | implementation | test report + kiến trúc | Hoàn thành |

Artifact chính là 50 file `output/EC_001.json` đến `output/EC_050.json`. Lần chạy cuối tạo đủ 50 output, không có failure; trace có 650 events và 50 `verification_passed`.

## 3. Giải thích kỹ thuật

### Vấn đề giải quyết

Một claim phải join customer, order, item, payment, product và seller, nhưng investigator không nên cùng đọc toàn bộ dữ liệu. Đồng thời policy có priority tuyệt đối nên không thể bỏ phiếu giữa agent. Output có hard constraints về null, ID, array limit, phép tính và thứ tự.

### Cách triển khai

Repository nạp CSV thành read-only indexes rồi project đúng field cho từng domain. Ba investigator chạy song song bằng `ThreadPoolExecutor`: Customer dựng lịch sử theo `customer_unique_id`; Payment cộng tiền bằng `Decimal`; Fulfillment tính delivery và handoff bằng datetime. Policy Adjudicator chạy sau barrier, chọn primary issue theo thứ tự `EC_POLICY_V2`, sau đó dựng secondary issue, responsibility, evidence, refund và action.

Verifier không tin draft: nó tính lại tiền và timestamp từ canonical packet, chạy lại policy priority độc lập, kiểm tra cross-field, ID/evidence, timestamp và limit. Lỗi có `field`, `error_code`, `expected`, `actual`, `owner`; Orchestrator chỉ route về owner, tối đa hai vòng. Writer chỉ chạy khi verifier trả `pass`.

### Input, output và contract

| Thành phần | Mô tả |
|---|---|
| Input | `input/EC_nnn.json`, `policy_version=EC_POLICY_V2` và 9 CSV Olist |
| Output | JSON đúng schema tại `output/EC_nnn.json` |
| Module phụ thuộc | Router → investigators → policy → verifier → writer |
| Điều kiện lỗi | order/policy không tồn tại, mismatch tài chính, sai priority/evidence/limit/schema |

### Cách xác minh

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 run.py --case-concurrency 5
```

- Kết quả kiểm thử: 3 test pass, gồm đủ sáu nhánh policy, no-item null contract và negative test priority.
- Kết quả pipeline: `input_cases=50`, `outputs_written=50`, `failures={}`.
- Artifact: `logging/trace.jsonl`, `logging/metadata.json`, `output/`.

## 4. Quyết định kỹ thuật quan trọng

- **Bối cảnh:** Các phép tính và policy đều có ground truth rõ, trong khi model local có sampling và có thể chưa chạy trên máy chấm.
- **Phương án cân nhắc:** cho LLM tự tạo toàn bộ JSON; hoặc dùng multi-agent handoff nhưng khóa field kiểm chứng bằng deterministic tools.
- **Phương án chọn:** Qwen2.5 7B cho investigator profile, Llama 3 8B cho policy/verifier profile; scoring path dùng `Decimal`, datetime, rule engine và independent verifier.
- **Lý do:** giữ được ownership, routing, trace và self-correction của multi-agent, đồng thời output tái lập và không hallucinate evidence.
- **Bằng chứng:** 50/50 draft qua verifier; phân phối kết quả phủ đủ 6 primary issue.

## 5. Lỗi đã xử lý

- **Triệu chứng:** order `unavailable` có payment nhưng không có item row; nếu coi tổng item là 0 thì có thể tạo `difference_brl` và `reconciled` sai contract.
- **Nguyên nhân:** công thức số học thông thường không biểu diễn được trạng thái “không có cơ sở đối soát”.
- **Cách xử lý:** vẫn báo `item_total_brl=0.0`, `freight_total_brl=0.0`, `payment_total_brl` theo nguồn nhưng đặt `expected_total_brl`, `difference_brl`, `reconciled` thành `null`; entity/product/handoff arrays rỗng.
- **Xác minh:** test `test_no_item_null_contract`; cả 6 unavailable cases qua verifier.

## 6. Hiểu luồng end-to-end

1. Input cung cấp `claimed_order_id`; Router dùng khóa join để lấy đúng customer, order, items, payments và products, rồi tạo packet tách biệt.
2. Ba investigator không chia theo từng CSV mà theo nhiệm vụ hoàn chỉnh, vì item–seller–product–delivery phụ thuộc chặt với nhau.
3. Policy chỉ nhận facts/IDs đã tổng hợp; nó không đọc raw CSV và là nơi duy nhất chọn primary issue.
4. Verifier khác investigator ở chỗ không tạo kết luận mới mà kiểm chứng draft với canonical facts/rules, sau đó route lỗi theo field owner.
5. Chạy cùng 50 input và deterministic rules giúp kết quả giữa các lần run so sánh được; trace mới luôn thay trace cũ.
6. Một case chỉ thành công khi verifier pass và writer ghi atomically. Nếu hết hai vòng sửa, case là `failed_validation` và không có JSON sai.

## 7. Cam kết

- [x] Báo cáo phản ánh đúng implementation và kết quả đã kiểm chứng.
- [x] Có thể giải thích luồng end-to-end và ownership từng field.
- [x] Không ghi thành công cho phần chưa chạy.
- [x] Không chứa API key, token, raw prompt hoặc chain-of-thought.

**Họ và tên:** Nguyễn Thị Hương Trà  
**Ngày xác nhận:** 2026-08-05
