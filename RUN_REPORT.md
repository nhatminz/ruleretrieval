# Báo cáo chạy thực tế — 2026-11-02 (Asia/Ho_Chi_Minh)

Đã triển khai project Python chạy được, download dữ liệu thật, ingest toàn bộ một statute shard, tạo input/reference tách riêng, chạy agent/baselines và evaluator riêng. **Chưa có bằng chứng về độ chính xác pháp lý.** 30 candidate cases đều là silver tạm thời và còn `NEEDS_REVIEW_SMALL_MODEL`; không phải 30 case expert-verified. Không có exact-supported case trong corpus hiện tại.

## 1. Project và môi trường

```text
RuleRetrieval/
├── configs/                      models, datasets, database, harness, evaluation YAML
├── prompts/                      case_extraction, court_citation_filter, issue_rule_alignment, rule_agent
├── src/rule_retrieval/
│   ├── clients/                  OpenAI-compatible client + Transformers server
│   ├── ingestion/                download, Parquet streaming, statute hierarchy parser
│   ├── database/                 SQLAlchemy schema, content snapshot, immutable SQLite backup
│   ├── dataset_builder/          input, citations, aligner, resolver, leakage/integrity audits
│   ├── retrieval/                read-only BM25/FTS5 facade and backend interface
│   ├── temporal/                 dates and conservative interval validation
│   ├── harness/                  protocol, budgets, dispatch, checkpoints, egress guard
│   ├── evaluation/               private evaluator and issue-level metrics
│   ├── storage/                  stable IDs, atomic files and trajectories
│   └── cli.py
├── scripts/                      executable build/serve/run/evaluate/isolation/experiment scripts
├── tests/                        30 unit/integration tests
├── data/
│   ├── raw/{judgments,statutes}/  exactly one Parquet per corpus + manifests/schema inspection
│   ├── processed/                bounded source selection pool
│   ├── database/                 master DB + immutable snapshot
│   ├── benchmark/public_inputs/dataset_20261101T174604Z_df9186fe/
│   └── benchmark/private_references/dataset_20261101T174604Z_df9186fe/
├── outputs/                      ingestion/dataset/agent runs, proofs and examples
├── README.md
├── requirements.lock
├── pyproject.toml
├── .env.example
└── .gitignore
```

Python 3.12.12, RTX 3090 24 GB, driver 555.42.06, PyTorch 2.5.1+cu121, Transformers 4.51.3. vLLM không có sẵn trong môi trường ban đầu; đã chọn fallback CUDA 12.1 được kiểm tra thực tế, không nâng driver. Chưa kiểm thử vLLM. Một server Qwen local dùng chung cho các vai trò, qua Unix socket `outputs/inference.sock`; server hiện giới hạn 24.576 input tokens. Fingerprint checkpoint/config và package versions ở [environment.json](outputs/environment.json), environment lock ở [requirements.lock](requirements.lock).

## 2. Shards thật và provenance

| Corpus | Filename | Kích thước thực tế | Commit đã ghim |
|---|---|---:|---|
| judgments | `documents-00000-of-00016.parquet` | 304,629,852 bytes (290.52 MiB) | `8ace3afc9c974afd51e12dccbefd4b3ba54ce1ec` |
| statutes | `documents-00000-of-00096.parquet` | 57,350,927 bytes (54.69 MiB) | `705ade585384108ded5fc30f40a06fe110bdff22` |

Judgments: **100.000 rows**; statutes: **6.000 rows**. Đã kiểm tra HF repository/config documents, tải bằng `hf_hub_download`, đọc schema thật bằng PyArrow rồi triển khai adapters theo schema đó. Không tải embeddings/reduces hoặc shard thứ hai. Cache HF/Xet/Transformers đều đặt trong `data/`.

- Judgment SHA256: `333799843fd183132e7a20399194fd2e431ee328ea8f3796b80841fd240d419e`.
- Statute SHA256: `34306d66a3c4bc35d3d7edfb7029aef9b12532f148533ceefdcf2298c841fe25`.
- [Judgment manifest](data/raw/judgments/documents-00000-of-00016.parquet.manifest.json), [statute manifest](data/raw/statutes/documents-00000-of-00096.parquet.manifest.json) lưu download time, commit, expected/actual SHA256 và file size.
- [Download idempotence check](outputs/download_resume_check.json): lần chạy lại giữ nguyên cả hai Parquet, cùng mtime/kích thước, không tải lại file hợp lệ.

## 3. Temporal statute database

| Thành phần | Số lượng |
|---|---:|
| Source records xử lý | 6.000 |
| Legal documents lưu được | 5.993 |
| Documents có hierarchy Điều | 5.026 |
| Documents chưa parse hierarchy, vẫn lưu nguyên văn | 967 |
| Records thiếu toàn văn tiếng Việt, có error log | 7 |
| Provisions | 336.307 |
| Provision versions | 336.307 |
| REFERS_TO có source evidence | 1.929 |
| Versions có document effective date nhưng history chưa xác minh | 171,794 |
| Versions có temporal status UNKNOWN | 164,513 |
| Versions có verified historical interval | 0 |

Không suy ra effective date từ issue date. Không tạo AMENDS/REPLACES/REPEALS thiếu chứng cứ. Một observed version cho mỗi provision hiện có không có nghĩa đó là nội dung áp dụng đúng cho mọi thời điểm.

Snapshot: `DB_9649fa8c1926b6d8a9bb73ca8ae3bd0be72510d80ab58be7bd88edad41545884`. Master DB khoảng 1,3 GiB, frozen backup riêng khoảng 1,3 GiB. Snapshot file journal DELETE/mode 0444 đã được xác minh cùng logical content ID, giúp runtime mount chỉ đọc không cần WAL/SHM. [DB stats](outputs/database_stats.json), [frozen artifact](outputs/frozen_database.json), [ingestion log](outputs/ingestion_runs/ingest_20261101T170458Z_1227e11c/stats.json).

## 4. Dataset, labels và corpus coverage

- Quét một judgment shard: 100.000 rows; reservoir theo domain/year có 496 ứng viên tiềm năng.
- Thử **91 cases**; xuất **30 candidates**, **30 issues**, **83 source-only facts**, **53 verified date occurrences**. Một issue được chọn/case ở v1; không coi là trích xuất đầy đủ mọi issue của bản án.
- **0 exact-supported cases (0%)**; **4 provision-supported cases (13,33%)**, còn version unknown. Không đổi tên 4 case này thành exact/gold support.
- 634 citation occurrences: 387 `COURT_APPLIED`, 147 `PARTY_CITED`, 98 `COURT_DISCUSSION`, 2 `PROSECUTOR_PROPOSED`, theo heuristic cần review.
- 30 primary applied citations đã align, tạo 30 issue-rule edges. **357 applied occurrences chưa được gán** vào issue được chọn, giữ `UNRESOLVED_ALIGNMENT`; không ép gán. Alignment coverage trên applied occurrences = 7.75%. Những citations này có thể thuộc vấn đề khác; chưa adjudicate đủ để phân biệt tất cả irrelevant/unresolved.
- Primary resolutions: **26 NOT_IN_CORPUS**, **4 PROVISION_MATCH_VERSION_UNKNOWN**, **0 EXACT_VERSION_MATCH**. Primary citation resolution rate ở cấp provision = 13.33%.
- Loại 61 cases: {"UNRESOLVED_ALIGNMENT": 31, "QUESTION_NOT_SUPPORTED_BY_TRIGGER_FACTS_AND_COURT_CONTEXT": 18, "LEAKAGE_RISK": 10, "INPUT_SEMANTIC_SUPPORT_NEEDS_REVIEW": 1, "SOURCE_LOCATOR_OR_ROLE_NEEDS_REVIEW": 1}.
- Leakage audit failure rate trên attempts: 10.99%. Không giữ case fail leakage trong public benchmark.

[Dataset stats](outputs/dataset_runs/dataset_20261101T174604Z_df9186fe/stats.json), [public index](data/benchmark/public_inputs/dataset_20261101T174604Z_df9186fe/index.json), [private index](data/benchmark/private_references/dataset_20261101T174604Z_df9186fe/index.json). Final mechanical integrity audit [PASS](outputs/dataset_runs/dataset_20261101T174604Z_df9186fe/integrity_audit.json): 30 cases, 83 facts, 53 events, 30 references khớp source spans, không phát hiện leakage theo các checks hiện có. Đây không phải semantic/expert validation.

Case selection có bias do domain/year, document length, đủ sections/citations và model extraction. Metadata year có outliers; chưa được dùng như event date. Pilot provision-covered còn có bias do corpus coverage. Không download thêm shard để che coverage thấp. Một số input stages được cache từ các lượt debug; origin và prompt/model traces được giữ trong stage provenance, các nhãn source/roles/alignment đã dựng lại theo policy của run cuối. Các draft/probe runs khác không được dùng làm benchmark/evaluation chính ở đây.

## 5. Tests, isolation và resume

**30 tests pass**; Python compile và cú pháp tất cả shell scripts pass. [JUnit](outputs/tests.xml).

Các checks bao gồm private-reference isolation, leakage/court conclusions, tool whitelist, argument injection, budget/zero-search, grounding ID/quote, temporal intervals/unknown/invalid metadata, grouped citations/points, hierarchy spans, frozen SQLite snapshots, dataset integrity, checkpoints, crash giữa tool reservation/response và context compaction. Test fixtures chỉ nằm trong temporary test directories, không trộn vào HF benchmark.

[OS isolation check](outputs/isolation_check.json): Internet TCP bị kernel chặn; inference UDS vẫn hoạt động; database thực sự đọc được và không ghi được; raw judgments, private labels, processed sources, offline traces và project reports đều bị ẩn. Agent chỉ có statute tools và final submit action.

[Trajectory isolation check](outputs/trajectory_isolation_check.json): đã kiểm tra 100 model requests của các agent runs, không thấy private label fields trong controller-supplied contexts hoặc tool dispatch ngoài whitelist. Không dựng hidden reasoning.

[Resume check](outputs/resume_check.json): exit 0, trajectories không đổi, predictions aggregate không đổi, vẫn đúng 5 unique rows. Ingestion đã thực sự tiếp tục qua nhiều lần ngắt; dataset preparation/finalization có checkpoints. Tool budget được charge/checkpoint trước dispatch, không hoàn lại khi mất response. Frozen snapshot khắc phục lỗi SQLite WAL trên mount chỉ đọc từng gặp trong lần thử sandbox đầu.

## 6. Runs và evaluation độc lập

| Run | Cases | Controller COMPLETED | All-evaluated micro F1 | Search/tool counts | Run ID |
|---|---:|---:|---:|---:|---|
| agent_smoke | 5 | 4/5 | 0.000 | 9 / 33 | `agent_20261101T181108Z_39f6ffc0` |
| bm25_all_candidates | 30 | 30/30 | 0.000 | 30 / 30 | `bm25_20261101T181108Z_5dd7ec74` |
| bm25_same_five | 5 | 5/5 | 0.000 | 5 / 5 | `bm25_20261101T181726Z_0e6f3428` |
| zero_search | 5 | 5/5 | 0.000 | 0 / 4 | `agent_20261101T181518Z_58bad311` |
| agent_provision_pilot | 4 | 2/4 | 0.000 | 9 / 33 | `agent_20261101T182739Z_3d95da16` |
| bm25_provision_pilot | 4 | 4/4 | 0.000 | 4 / 4 | `bm25_20261101T182739Z_736d40a8` |

**COMPLETED là submit hợp lệ, không phải truy xuất luật đúng.** Agent smoke 5 case có 4 explicit abstentions và 1 TURN_LIMIT; không trả rule hợp lệ nào. Pilot trên 4 case có provision trong corpus có 2 abstentions và 2 TURN_LIMIT; cũng không trả rule nào. Zero-search 5 case submit abstention, với **0 search calls thực thi**. Các yêu cầu vượt budget/đọc thêm khi context chỉ còn dành cho final đã bị chặn và lưu dưới violations.

BM25 full-candidate baseline trả 150 valid internal IDs, tất cả grounding/ID checks pass nhưng không trùng primary silver references; pilot BM25 trên 4 provision-covered cases cũng không hit reference trong top-5. Đây là kết quả yếu thực tế; không kết luận agent tốt hơn baseline. [So sánh kiểm tra cùng cases/hash/snapshot/model settings](outputs/comparison_checks.json) pass cho matched-five và provision-covered pilot. Model latency/tokens được lưu đầy đủ nhưng không suy ra kết luận hiệu năng tổng quát từ các lượt smoke nhỏ này.

**Exact-supported-only metrics = null** vì không có case phù hợp. Correct-version/wrong-version rates cũng **null**, không phải 0%/100%. Version-unknown provision matches chỉ được so ở cấp provision, không được tính là phiên bản đúng. All-candidate metrics giữ missing-corpus denominators/breakdown; supported-only không phạt luật ngoài corpus. 5 case đầu đều thiếu primary reference trong DB, nên đã chạy thêm pilot 4 case provision-covered để không dùng toàn bộ lượt smoke ban đầu làm một phép đo retrieval có thể đạt được.

Metrics đầy đủ, per-case outputs và trajectories:

- [Agent smoke metrics](outputs/agent_runs/agent_20261101T181108Z_39f6ffc0/evaluation/metrics.json).
- [BM25 30-case metrics](outputs/agent_runs/bm25_20261101T181108Z_5dd7ec74/evaluation/metrics.json).
- [Matched 5-case BM25 metrics](outputs/agent_runs/bm25_20261101T181726Z_0e6f3428/evaluation/metrics.json).
- [Zero-search metrics](outputs/agent_runs/agent_20261101T181518Z_58bad311/evaluation/metrics.json).
- [Agent provision pilot](outputs/agent_runs/agent_20261101T182739Z_3d95da16/evaluation/metrics.json), [BM25 provision pilot](outputs/agent_runs/bm25_20261101T182739Z_736d40a8/evaluation/metrics.json).

E1 có BM25 thực; dense/hybrid chỉ có interface và báo unsupported, không giả kết quả. E2 đã chạy 0 và 4 search-call settings; config 2 sẵn trong script. E3 có none/exact/uncertainty-aware và tests, chưa chạy đủ grid thật; corpus hiện không có verified interval. E4 đã chạy cả agent/direct trên cùng 5 cases và cùng 4 pilot cases. Script đầy đủ ở [run_experiments.sh](scripts/run_experiments.sh).

## 7. Ví dụ tách biệt, case `CASE_0f52b053888499615845c16a`

### Public input — đây là dữ liệu agent được phép nhận

```json
{
  "case_id": "CASE_0f52b053888499615845c16a",
  "input": {
    "facts": [
      {
        "fact_id": "S20",
        "text": "Tại văn bản số 08/HPN-BTV ngày 15/4/2015 và số 11/CV-TL ngày \n21/3/2016 cũng như ý kiến tại các phiên hòa giải ngày 16/6/2015 và ngày \n21/3/2016, bị đơn là Hội LHPN thành phố Đ  có ý kiến đối với yêu cầu khởi kiện \ncủa bà Vũ Thị Kiều G như sau: \nTừ ngày 15/11/2006 đến ngày 31/12/2013, bà Vũ Thị Kiều G làm việc liên \ntục tại Hội LHPN thành phố Đ theo các hợp đồng lao động có thời hạn trong thời \ngian làm việc bà G hưởng đầy đủ quyền lợi của người lao động theo quy định của \npháp luật lao động.",
        "source_span": {
          "field": "markdown",
          "start": 4608,
          "end": 5105,
          "quote": "Tại văn bản số 08/HPN-BTV ngày 15/4/2015 và số 11/CV-TL ngày \n21/3/2016 cũng như ý kiến tại các phiên hòa giải ngày 16/6/2015 và ngày \n21/3/2016, bị đơn là Hội LHPN thành phố Đ  có ý kiến đối với yêu cầu khởi kiện \ncủa bà Vũ Thị Kiều G như sau: \nTừ ngày 15/11/2006 đến ngày 31/12/2013, bà Vũ Thị Kiều G làm việc liên \ntục tại Hội LHPN thành phố Đ theo các hợp đồng lao động có thời hạn trong thời \ngian làm việc bà G hưởng đầy đủ quyền lợi của người lao động theo quy định của \npháp luật lao động."
        }
      },
      {
        "fact_id": "S21",
        "text": "Ngày 20/02/2014, Thành ủy Đ ban hành Thông báo số 358-TB/TU về chủ \ntrương hợp đồng cán bộ năm 2014, Thông báo có nội dung: “Đối với 02 cán bộ \nhợp đồng làm việc tại cơ quan UBMT thành phố và Hội LHPN thành phố, sau khi \ncó thông báo kết quả thi tuyển công chức khối Đảng, Mặt trận, các đoàn thể của \ntỉnh năm 2014 thì chấm dứt hợp đồng”.",
        "source_span": {
          "field": "markdown",
          "start": 5107,
          "end": 5445,
          "quote": "Ngày 20/02/2014, Thành ủy Đ ban hành Thông báo số 358-TB/TU về chủ \ntrương hợp đồng cán bộ năm 2014, Thông báo có nội dung: “Đối với 02 cán bộ \nhợp đồng làm việc tại cơ quan UBMT thành phố và Hội LHPN thành phố, sau khi \ncó thông báo kết quả thi tuyển công chức khối Đảng, Mặt trận, các đoàn thể của \ntỉnh năm 2014 thì chấm dứt hợp đồng”."
        }
      },
      {
        "fact_id": "S23",
        "text": "Tại phiên họp, Chủ tịch Hội LHPN thành phố đã \ntrực tiếp thông báo chủ trương hợp đồng cán bộ của Hội LHPN thành phố năm \n2014 theo tinh thần Thông báo số 358-TB/TU, và thông báo việc chấm dứt hợp \nđồng lao động đối với bà Vũ Thị Kiều G, cụ thể: Hội LHPN thành phố sẽ chấm dứt \nhợp đồng lao động với bà G, sau khi có kết quả thi tuyển công chức.",
        "source_span": {
          "field": "markdown",
          "start": 5547,
          "end": 5892,
          "quote": "Tại phiên họp, Chủ tịch Hội LHPN thành phố đã \ntrực tiếp thông báo chủ trương hợp đồng cán bộ của Hội LHPN thành phố năm \n2014 theo tinh thần Thông báo số 358-TB/TU, và thông báo việc chấm dứt hợp \nđồng lao động đối với bà Vũ Thị Kiều G, cụ thể: Hội LHPN thành phố sẽ chấm dứt \nhợp đồng lao động với bà G, sau khi có kết quả thi tuyển công chức."
        }
      }
    ],
    "events": [
      {
        "event_id": "E1",
        "event_type": "other",
        "date": "2015-04-15",
        "date_evidence": "15/4/2015",
        "fact_id": "S20"
      },
      {
        "event_id": "E2",
        "event_type": "other",
        "date": "2016-03-21",
        "date_evidence": "21/3/2016",
        "fact_id": "S20"
      },
      {
        "event_id": "E3",
        "event_type": "filing_date",
        "date": "2015-06-16",
        "date_evidence": "16/6/2015",
        "fact_id": "S20"
      },
      {
        "event_id": "E4",
        "event_type": "filing_date",
        "date": "2016-03-21",
        "date_evidence": "21/3/2016",
        "fact_id": "S20"
      },
      {
        "event_id": "E5",
        "event_type": "filing_date",
        "date": "2006-11-15",
        "date_evidence": "15/11/2006",
        "fact_id": "S20"
      },
      {
        "event_id": "E6",
        "event_type": "filing_date",
        "date": "2013-12-31",
        "date_evidence": "31/12/2013",
        "fact_id": "S20"
      },
      {
        "event_id": "E7",
        "event_type": "other",
        "date": "2014-02-20",
        "date_evidence": "20/02/2014",
        "fact_id": "S21"
      }
    ],
    "legal_issues": [
      {
        "issue_id": "I1",
        "legal_question": "Bà Vũ Thị Kiều G làm việc liên tục tại Hội LHPN thành phố Đ từ ngày 15/11/2006 đến ngày 31/12/2013, theo các hợp đồng lao động có thời hạn trong thời gian làm việc bà G hưởng đầy đủ quyền lợi của người lao động theo quy định của pháp luật lao động. Ngày 20/02/2014, Thành ủy Đ ban hành Thông báo số 358-TB/TU về chủ trương hợp đồng cán bộ năm 2014, Thông báo có nội dung: ‘Đối với 02 cán bộ hợp đồng làm việc tại cơ quan UBMT thành phố và Hội LHPN thành phố, sau khi có thông báo kết quả thi tuyển công chức khối Đảng, Mặt trận, các đoàn thể của tỉnh năm 2014 thì chấm dứt hợp đồng’. Ngày 13/3/2014, Thường trực Hội LHPN thành phố Đ họp phiên mở rộng (bà Vũ Thị Kiều G có tham gia). Tại phiên họp, Chủ tịch Hội LHPN thành phố đã trực tiếp thông báo chủ trương hợp đồng cán bộ của Hội LHPN thành phố năm 2014 theo tinh thần Thông báo số 358-TB/TU, và thông báo việc chấm dứt hợp đồng lao động đối với bà Vũ Thị Kiều G, cụ thể: Hội LHPN thành phố sẽ chấm dứt hợp đồng lao động với bà G, sau khi có kết quả thi tuyển công chức. Ngày 31/3/2014, Hội đồng tuyển dụng Tỉnh ủy Q thông báo kết quả điểm kỳ thi tuyển công chức, ngày 21/4/2014, Hội LHPN thành phố ban hành Thông báo số 03/TB-BNV về việc chấm dứt hợp đồng lao động với bà G. Bà G không ký hợp đồng lao động 06 tháng kể từ tháng 01 đến tháng 6/2014. Vậy Hội LHPN thành phố Đ có vi phạm thời hạn báo trước khi chấm dứt hợp đồng lao động hay không? Nếu có, thì thời hạn báo trước là bao lâu? Nếu không, thì nguyên đơn có quyền khởi kiện theo quy định của pháp luật không?",
        "trigger_fact_ids": [
          "S21",
          "S23",
          "S20"
        ]
      }
    ],
    "date_uncertainties": []
  }
}
```

### Private reference — chỉ offline builder/evaluator, không có trong agent context

```json
{
  "case_id": "CASE_0f52b053888499615845c16a",
  "references": [
    {
      "issue_id": "I1",
      "document_name": "Bộ luật lao động",
      "article": "38",
      "clause": null,
      "point": null,
      "court_citation": "Điều 38 để chấm dứt \nhợp đồng và phải báo trước cho bà Vũ Thị Kiều G biết trước ít nhất 45 ngày, theo \nđiểm a, khoản 2, Điều 38 của Bộ luật lao động",
      "court_source_span": {
        "field": "markdown",
        "start": 15187,
        "end": 15335,
        "quote": "Điều 38 để chấm dứt \nhợp đồng và phải báo trước cho bà Vũ Thị Kiều G biết trước ít nhất 45 ngày, theo \nđiểm a, khoản 2, Điều 38 của Bộ luật lao động"
      },
      "citation_role": "COURT_APPLIED",
      "provision_version_id": null,
      "resolution_status": "NOT_IN_CORPUS"
    }
  ],
  "label_quality": "SILVER",
  "verification_status": "NEEDS_REVIEW_SMALL_MODEL"
}
```

### Prediction do agent thực sự submit

```json
{
  "case_id": "CASE_0f52b053888499615845c16a",
  "status": "COMPLETED",
  "predictions": [
    {
      "issue_id": "I1",
      "rules": [],
      "abstain_reason": "No rule grounded in the internal retrieved evidence"
    }
  ],
  "usage": {
    "prompt_tokens": 42297,
    "completion_tokens": 2339,
    "total_tokens": 44636
  },
  "latency_seconds": 56.26866164896637
}
```

Files tách riêng: [public input](outputs/examples/public_input.json), [private reference](outputs/examples/private_reference.json), [agent prediction](outputs/examples/agent_prediction.json), [BM25 prediction](outputs/examples/bm25_prediction.json). Private example file mode 0600; cả examples và báo cáo này bị ẩn trong OS runtime allowlist. Không dùng những ví dụ private này để few-shot agent.

## 8. Commands chạy lại

```bash
cd /mnt/hdd/nhatminh/RuleRetrieval
source .venv/bin/activate
export RULE_RETRIEVAL_ROOT="$PWD"
# Server hiện đang chạy bằng checkpoint local qua socket này:
export RULE_RETRIEVAL_INFERENCE_UDS="$PWD/outputs/inference.sock"

./scripts/download_data.sh
./scripts/build_database.sh --resume ingest_20261101T170458Z_1227e11c
python -m rule_retrieval.cli freeze-database
./scripts/build_dataset.sh --target 30 --max-attempts 180 --resume dataset_20261101T174604Z_df9186fe
python -m rule_retrieval.cli audit-dataset dataset_20261101T174604Z_df9186fe

./scripts/run_agent_isolated.sh --dataset-run dataset_20261101T174604Z_df9186fe --limit 5
./scripts/run_agent_isolated.sh --dataset-run dataset_20261101T174604Z_df9186fe --limit 5 --resume agent_20261101T181108Z_39f6ffc0
./scripts/run_evaluation.sh agent_20261101T181108Z_39f6ffc0 --dataset-run dataset_20261101T174604Z_df9186fe
./scripts/run_agent_isolated.sh --dataset-run dataset_20261101T174604Z_df9186fe --mode direct --limit 30

# Cùng 4 cases có provision trong corpus, vẫn version unknown:
./scripts/run_agent_isolated.sh --dataset-run dataset_20261101T174604Z_df9186fe --limit 4 --case-id CASE_e5b55c3834869ab387dbb241 --case-id CASE_b24f47232769fe8867d4b1b1 --case-id CASE_e4637ae01cc93dd3d57eaf07 --case-id CASE_341c97e91b83783eddfd9193
./scripts/run_agent_isolated.sh --dataset-run dataset_20261101T174604Z_df9186fe --mode direct --limit 4 --case-id CASE_e5b55c3834869ab387dbb241 --case-id CASE_b24f47232769fe8867d4b1b1 --case-id CASE_e4637ae01cc93dd3d57eaf07 --case-id CASE_341c97e91b83783eddfd9193

./scripts/check_isolation.sh
./scripts/run_experiments.sh dataset_20261101T174604Z_df9186fe --limit 5
pytest --junitxml=outputs/tests.xml
```

Nếu server chưa chạy, terminal riêng:

```bash
export RULE_RETRIEVAL_MODEL_PATH=/mnt/hdd/nhatminh/SpecDecode/models/Qwen2.5-1.5B-Instruct
./scripts/serve_local_model.sh --uds "$PWD/outputs/inference.sock" --max-input-tokens 24576
```

README có hướng dẫn cài environment, TCP/remote endpoint, đổi root, PostgreSQL, config budgets/temporal modes và các giới hạn của backend chưa kiểm thử.

## 9. Hạn chế còn lại

1. Corpus hiện chỉ có 4 cases với provision match và 0 cases có exact-version support. Cần chọn thêm shards luật một cách tường minh và bổ sung lịch sử/nguồn kiểm chứng phiên bản trước khi đo temporal correctness có ý nghĩa.
2. Tất cả labels cần review. Role extraction là source heuristic; alignment là Qwen + lexical guard, không chứng minh semantic entailment. 357 unresolved applied occurrences và chỉ một issue/case làm nhãn chưa đầy đủ để kết luận pháp lý.
3. Qwen 1.5B chưa đủ tốt cho agent retrieval/alignment: có malformed actions, tool errors, blocked budget attempts, abstentions và TURN_LIMIT. Không sửa ground truth theo model để tăng điểm.
4. OCR/grouping/quoted amendment text còn có thể parse sai; parsed statutes đều unverified. Không tự dựng lịch sử sửa đổi, transitional rules hay hồi tố có lợi.
5. Dense/hybrid, PostgreSQL integration, vLLM và toàn bộ experiment grid chưa đo thực tế. PostgreSQL URL/schema có đường mở rộng; SQLite + FTS5 + Transformers là stack đã chạy.

Machine-readable summary: [run_summary.json](outputs/run_summary.json). Đây là prototype có traceability và smoke test thật; chưa phải benchmark pháp lý đã được chuyên gia xác minh.
