# Vietnamese Legal Rule Retrieval

Project Python chạy độc lập để đo mức độ truy xuất quy phạm khớp với **căn cứ Tòa đã áp dụng**. Input đã có facts, event dates và legal issues. Project không dùng pipeline issue spotting. Nhãn là `COURT_APPLIED` **silver**, chưa được chuyên gia xác minh. Qwen 1.5B dùng để kiểm tra luồng phần mềm; không chứng minh độ chính xác pháp lý.

## Cài đặt

Python >=3.11; môi trường đã chạy dùng Python 3.12, RTX 3090, driver 555.42.06 và PyTorch 2.5.1/CUDA 12.1. Máy ban đầu chưa có vLLM. Backend Transformers tránh nâng driver/CUDA và chỉ nạp một instance model cho mọi vai trò.

```bash
cd /mnt/hdd/nhatminh/RuleRetrieval
./scripts/setup.sh --model
source .venv/bin/activate
export RULE_RETRIEVAL_ROOT="$PWD"
export RULE_RETRIEVAL_MODEL_PATH=/mnt/hdd/nhatminh/SpecDecode/models/Qwen2.5-1.5B-Instruct
```

Đường dẫn checkpoint ở trên chỉ là ví dụ cho máy development. Business logic không hardcode đường dẫn này. Khi chuyển máy, đặt `RULE_RETRIEVAL_ROOT`, checkpoint và endpoint tương ứng. Các đường dẫn tương đối và SQLite URL được resolve theo root, không theo working directory. `.env.example` là mẫu; export các biến cần dùng trong shell (CLI không tự đọc `.env`). Không chạy đồng thời nhiều trình cài vào cùng venv.

Cache Hugging Face, Xet, datasets và Transformers đều bị đặt dưới `data/hf_cache/`; Parquet tải về nằm dưới `data/raw/`. Cache cài package trong scripts cũng nằm trên ổ project để tránh làm đầy ổ hệ thống.

`requirements.lock` ghi environment đã kiểm thử. Để tái tạo chính xác trên Linux/Python 3.12, cài bằng `uv pip install --python .venv/bin/python --index-strategy unsafe-best-match -r requirements.lock`, rồi `uv pip install --python .venv/bin/python --no-deps -e .`. CUDA wheel cần driver tương thích; cài backend khác khi chuyển phần cứng được ghi thành environment/run mới.

## Host một model

TCP cho phát triển:

```bash
./scripts/serve_local_model.sh --host 127.0.0.1 --port 8000
```

Hoặc Unix socket để chạy harness với OS network isolation, dùng cùng một socket cho offline builders và agent:

```bash
./scripts/serve_local_model.sh --uds "$PWD/outputs/inference.sock"
# Terminal khác:
export RULE_RETRIEVAL_INFERENCE_UDS="$PWD/outputs/inference.sock"
```

Server hỗ trợ `/health`, `/v1/models`, `/v1/chat/completions`. JSON mode của Transformers là chỉ dẫn cho model, không phải constrained decoding; validators phía controller vẫn bắt buộc. Backend này dùng JSON actions; không giả vờ hỗ trợ native function calling. Giới hạn context mặc định 24.576 input tokens; quá giới hạn trả HTTP 400, không âm thầm truncate. Có thể cấu hình `--max-input-tokens` nếu đủ VRAM. Requests chạy tuần tự qua lock để tránh nạp nhiều model hoặc inference đồng thời gây OOM.

Nếu máy khác đã có vLLM tương thích, có thể dùng:

```bash
RULE_RETRIEVAL_SERVER_BACKEND=vllm ./scripts/serve_local_model.sh --max-model-len 16384
```

Để dùng remote inference, bỏ biến UDS, đặt `RULE_RETRIEVAL_BASE_URL`, `RULE_RETRIEVAL_MODEL_ID`, tùy chọn `RULE_RETRIEVAL_API_KEY`. Timeout, retry, temperature, token limit, native tools và context budget ở `configs/models.yaml`. API key không ghi vào manifest. Các vai trò extraction/alignment/agent dùng cùng endpoint mặc định; không cần host ba model.

## Download và inspect hai shards

```bash
./scripts/download_data.sh
# Chọn shard khác một cách tường minh, vẫn chỉ tải 1 shard cho lệnh đó:
./scripts/download_data.sh --kind judgments --shard documents-00000-of-00016.parquet
python -m rule_retrieval.cli inspect data/raw/statutes/documents-00000-of-00096.parquet
```

Config ghim commit cụ thể của [judgments](https://huggingface.co/datasets/tmquan/cbba-toaan-gov-vn) và [statutes](https://huggingface.co/datasets/tmquan/thuvienphapluat-vn-vbpl). Downloader kiểm tra config `documents` từ HF repository trước, xác minh kích thước/SHA256 LFS, lưu revision, filename, UTC download time và digest vào manifest cạnh file. File hợp lệ không tải lại; HF Hub hỗ trợ tiếp tục download dang dở, kèm retry ở application. Không dùng `snapshot_download`, không tải embeddings/reduces và không tự mở rộng corpus khi coverage thấp. `.schema.json` chứa schema và một sample nguồn; đó là artifact offline, không cho agent truy cập.

## Build database

```bash
./scripts/build_database.sh
python -m rule_retrieval.cli database-stats
python -m rule_retrieval.cli freeze-database  # idempotent; build-database also does this automatically
python -m rule_retrieval.cli search 'nghĩa vụ hoàn trả khoản vay' --top-k 5
# Sau khi bị ngắt, lấy RUN_ID ở outputs/ingestion_runs/:
./scripts/build_database.sh --resume INGEST_RUN_ID
```

PyArrow đọc từng batch. SQLite/SQLAlchemy lưu `LegalDocument → Provision → ProvisionVersion`, raw Vietnamese text, metadata, source spans, quan hệ có bằng chứng và ingestion audit. Parser tách Điều/Khoản/Điểm, giữ đường dẫn Chương/Mục nếu có; văn bản không có cấu trúc Điều vẫn lưu nguyên văn và được liệt kê trong `unparsed.jsonl`. Locator lặp trong phụ lục hoặc trích dẫn sửa đổi giữ ID riêng, không tự coi là lịch sử sửa đổi đã xác minh. Index FTS5 dùng BM25 với truy vấn parameterized. SQLite dùng WAL; mỗi văn bản được commit trước checkpoint. Checkpoint ingestion dùng atomic replace, không fsync thêm một file JSON cho mỗi văn bản; SQLite `synchronous=NORMAL` cung cấp độ bền của transaction, còn mất điện/OS crash vẫn cần kiểm tra lại nguồn/checkpoint. Có thể chạy lại ingestion idempotently để phục hồi.

Sau ingestion, SQLite backup tạo `data/database/snapshots/<DB_ID>.sqlite`, chuyển bản sao sang journal DELETE, xác minh lại content snapshot ID rồi đặt file mode 0444. Runtime mặc định tự chọn file này theo public dataset index. Nó không cần tạo WAL/SHM trên mount chỉ đọc và không thay đổi khi master DB được ingest tiếp. Không copy riêng file `.sqlite` đang ở WAL mode bằng `cp`; dùng lệnh freeze để lấy đầy đủ các transaction đã commit. URL DB được override tường minh vẫn có ưu tiên; với SQLite custom, có thể trỏ URL trực tiếp vào file frozen.

Ngày ban hành và ngày hiệu lực là hai trường khác nhau. Chỉ đọc ngày hiệu lực được cung cấp; thiếu hoặc sai định dạng thì `UNKNOWN`. Một ngày hiệu lực cấp document chỉ tạo `DOCUMENT_DATE_ONLY`; nội dung provision chưa có lịch sử xác minh vẫn `PARSED_UNVERIFIED`. Không có quy tắc tự nâng trạng thái thành `VERIFIED_INTERVAL`, không suy ra hiệu lực mọi thời điểm và không tạo AMENDS/REPEALS/REPLACES từ tên văn bản. Quan hệ `REFERS_TO` chỉ có khi citation thật xuất hiện trong raw text và resolve duy nhất sang văn bản nội bộ.

## Build benchmark

```bash
./scripts/build_dataset.sh --target 30 --max-attempts 150
./scripts/build_dataset.sh --target 30 --max-attempts 150 --resume DATASET_RUN_ID
```

Có thể chuẩn bị extraction/alignment trong lúc ingestion còn chạy, rồi đóng băng DB lúc finalize. Preparation chưa xuất input/reference có thể chạy agent:

```bash
./scripts/build_dataset.sh --target 30 --prepare-only
# Sau khi ingestion hoàn tất:
./scripts/build_dataset.sh --target 30 --resume DATASET_RUN_ID
```

Ba component tách riêng:

- `CaseInputBuilder`: chọn các đoạn narrative an toàn, LLM đọc tình tiết và nhận định để viết câu hỏi. Python liên kết câu hỏi với các đoạn facts/nhận định thật bằng overlap từ, kiểm tra source spans và chọn ngày được viết tường minh. Không sinh facts/dates bằng LLM. Đây là heuristic silver cần semantic review; overlap không chứng minh entailment. Prompt bounded ở facts/nhận định, không đưa toàn bộ một bản án dài vào model rồi cắt ngầm.
- `CourtCitationLabelBuilder`: dùng `citations`, `citations_law`, locator và source text để phân loại từng occurrence. Mặc định `configs/datasets.yaml → builder.citation_role_mode: source_evidence`: `COURT_APPLIED` cần dấu hiệu áp dụng trong phần của chính Tòa, loại voice của đương sự/Viện kiểm sát và citation bị bác. Đây là heuristic silver, không tự coi là verified. Chế độ `llm_corroborated` bổ sung một lượt model classification, vẫn giữ source gate. Các trường `parent_acts` gốc được giữ trong provenance nguồn; chúng không tự trở thành applied labels. Dataset spans không được tin nếu không khớp raw markdown. Các citation thiếu tên/ID văn bản không đủ điều kiện thành primary reference.
- `IssueRuleAligner`: LLM chọn citation liên quan theo từng issue, hỗ trợ many-to-many; kiểm tra context nguồn và loại conflict chức năng quan sát được (ví dụ luật tố tụng bị gán substantive). Không ép alignment khi thiếu chứng cứ. Những citation không gán được lưu `UNRESOLVED_ALIGNMENT`.

Leakage audit chạy cả khi build và khi agent nạp input: chặn Điều/Khoản/Điểm kèm số, reference IDs/private fields, căn cứ có citation, câu tuyên xử/quyết định và passage overlap. Narrative có án sơ thẩm được trích lại vẫn có nguy cơ; các câu dạng disposition bị loại bảo thủ. Case không qua audit bị loại và ghi nguyên nhân. Kiểm tra bằng regex/source spans không thể bảo đảm đã tìm mọi semantic leakage; mọi record từ model nhỏ vẫn mang `NEEDS_REVIEW_SMALL_MODEL` trong private metadata.

Input và reference được xuất vào hai cây riêng:

```text
data/benchmark/public_inputs/<dataset_run>/CASE_*.json
data/benchmark/private_references/<dataset_run>/CASE_*.json
```

Reference dir mode 0700, reference files 0600. Agent không mở nhánh private. Mỗi issue được giữ trong candidate set phải có ít nhất một applied reference được align; reference không có trong DB vẫn được giữ để thống kê coverage. Resolver đòi đúng văn bản, locator và phiên bản; tên luật/năm mơ hồ không ép chọn. `EXACT_VERSION_MATCH` chỉ khi interval và nội dung đã xác minh. `PROVISION_MATCH_VERSION_UNKNOWN`, `DOCUMENT_ONLY_MATCH`, `NOT_IN_CORPUS`, `AMBIGUOUS_MATCH` được báo riêng. `corpus_supported_cases` yêu cầu toàn bộ primary references exact; `provision_supported_cases` là thống kê bổ sung, không gọi là exact support.

Tuyển chọn dùng reservoir hash ổn định theo domain/year rồi round-robin, không tải thêm shard. Source year có thể sai; chưa được dùng như event date. Có length filter và việc yêu cầu sections/citations nên selection có bias. Nếu chưa có 30 record qua kiểm tra sau `max-attempts`, báo số thật và các lý do. Corpus-supported subset còn thiên lệch về luật đã có trong shard và có metadata kiểm chứng. Không thay nhãn theo output agent để cải thiện metric.

## Agent, trajectories và resume

```bash
./scripts/run_agent.sh --dataset-run DATASET_RUN_ID --limit 5
./scripts/run_agent.sh --dataset-run DATASET_RUN_ID --limit 5 --resume AGENT_RUN_ID
# Chọn tường minh cùng các case cho agent và baseline, không sửa public index:
./scripts/run_agent.sh --dataset-run DATASET_RUN_ID --case-id CASE_ID_1 --case-id CASE_ID_2 --limit 2
# Bảo vệ bằng OS namespace, cần Unix socket và Bubblewrap:
./scripts/run_agent_isolated.sh --dataset-run DATASET_RUN_ID --limit 5
./scripts/check_isolation.sh
```

Model chỉ có bốn read-only statute tools và `submit_rules`. Python enforce whitelist, Pydantic argument schema (extra fields bị từ chối), search budget, total-call budget, max results, max rules/issue và max turns. Zero-search nghĩa là không thể search; model có thể abstain. Không có eval, arbitrary SQL, HTTP/browser hay shell dispatcher. Query FTS được tokenize/quote và SQL parameterized. Runtime database connection dùng SQLite `mode=ro` + `query_only`, PostgreSQL dùng transaction read-only.

Final IDs phải tồn tại và đã được tool trả về trong chính case đó. Evidence phải là exact substring model đã thấy. Search/history responses có thể rút gọn content và đánh dấu rõ; `get_provision` đọc đầy đủ. Model có thể abstain, hoặc kết thúc với `MODEL_ERROR`, `CONTEXT_LIMIT`, `TURN_LIMIT`; lỗi không được đổi thành success. Temporal status được controller tính lại từ event ID mà model chọn, không tin nhãn chắc chắn do model tự khai. Không dùng mặc định judgment date cho tất cả quy phạm. Transitional/retroactivity/more-favorable exceptions không được tự dựng; unknown hoặc out-of-interval được đánh dấu rõ.

Khi context gần đầy, harness ghi một sự kiện compaction và chỉ giữ public input cùng các đoạn tool output đã quan sát, không sinh tóm tắt reasoning. Từ đó chỉ cho `submit_rules`. Model vẫn phải tự submit hợp lệ hoặc abstain; harness không tự biến lỗi thành prediction thành công. Nếu input/excerpts vẫn quá lớn thì kết thúc `CONTEXT_LIMIT`. Feedback lỗi chứa IDs thực sự đã truy xuất và một schema abstention hợp lệ; không chứa oracle.

`run_agent.sh` có Python socket audit guard chỉ cho inference origin và không dùng proxy môi trường/redirect. Đây là defense in depth, không phải sandbox cho arbitrary Python. `run_agent_isolated.sh` thực sự unshare network, ẩn toàn bộ project data rồi chỉ mount read-only DB/public inputs, ẩn offline traces/private references và cho ghi riêng `outputs/agent_runs`. Model server liên lạc qua socket đã mount; Internet TCP bị chặn ở kernel. Không chạy download/build/evaluation trong sandbox này. Máy development đã kiểm tra Bubblewrap/network namespaces; nếu máy khác không hỗ trợ, script báo lỗi, không giả vờ có OS isolation. Nếu có bản sao private data bên ngoài project, phải bảo vệ riêng; đây không phải sandbox filesystem toàn hệ điều hành.

Mỗi run có model/config, prompt hashes/version, code version, input hashes và DB snapshot. Mỗi turn log model-visible messages, server-provided response, tool calls/responses, errors, token usage, latency, budgets và final predictions. Không dựng hidden reasoning. Per-case atomic files là nguồn chính; JSONL aggregate được rebuild để không duplicate khi resume. Không resume nếu public inputs, DB snapshot, prompts, model settings hoặc budgets đã đổi. Counter được checkpoint/fsync **trước** khi thực thi read tool; ngắt giữa tool call và response không hoàn lại budget hoặc tạo kết quả giả. Trước khi có response, resume thông báo attempt đã charge nhưng không có kết quả. Inference đang dở có thể được thử lại và trajectory ghi request trước đó. Atomic sample files/logs dùng flush/close và rename; không fsync mọi artifact trên HDD. Đây là phục hồi khi process bị ngắt; sau power loss/OS crash cần audit/re-ingest từ nguồn.

## Evaluation và baselines

```bash
./scripts/run_evaluation.sh AGENT_RUN_ID --dataset-run DATASET_RUN_ID
python -m rule_retrieval.cli audit-dataset DATASET_RUN_ID
./scripts/run_agent.sh --dataset-run DATASET_RUN_ID --mode direct --limit 30
./scripts/run_agent.sh --dataset-run DATASET_RUN_ID --search-budget 0 --limit 5
./scripts/run_agent.sh --dataset-run DATASET_RUN_ID --search-budget 2 --limit 5
./scripts/run_agent.sh --dataset-run DATASET_RUN_ID --temporal exact --limit 5
./scripts/run_experiments.sh DATASET_RUN_ID --limit 5
```

Evaluator chạy riêng sau agent; đây là thành phần duy nhất của execution/evaluation workflow mở private references. Outputs ở `outputs/agent_runs/<run_id>/evaluation/metrics.json` và `per_case_results.jsonl`.

Rule metrics dùng **provision identity**, gồm micro/macro P/R/F1, recall@k, MRR/nDCG@k, exact issue set match. Version accuracy được chấm riêng **chỉ khi có exact-version reference**; không biến unknown version thành temporal correctness. Unsupported references vẫn ở all-candidate denominator (kèm ceiling/missing-corpus breakdown), còn exact-supported-only không phạt luật ngoài DB. Không có case exact-supported thì metrics là `null`, không phải 0% hoặc 100%. Báo cáo phân biệt số case đã chạy với toàn bộ candidate pool.

Candidate ranking của agent là các search results gộp theo lần xuất hiện đầu ở cấp case; công cụ không nhận issue ID nên đây **không phải** issue-specific ranking. MRR/nDCG được gắn nhãn attribution này để tránh diễn giải quá mức. `none` bỏ temporal metadata khỏi tool output; `exact` chỉ trả verified intervals; `uncertainty_aware` giữ unknown và cả ứng viên cần bằng chứng ngoại lệ thời gian. Exact mode có thể không trả kết quả khi corpus chưa xác minh lịch sử; đó là kết quả thật. Backend interface có sẵn cho dense/hybrid; yêu cầu backend chưa cài sẽ báo `NotImplementedError`, không giả làm dense bằng BM25.

E1: BM25 có sẵn, Dense/Hybrid optional. E2: budgets 0/2/4. E3: none/exact/uncertainty-aware. E4: direct BM25 vs LLM tools. Các lệnh lưu run ID riêng, dùng cùng public index/DB/model config. Script experiments chạy cấu hình, evaluator được gọi riêng theo từng run ID. Không có kết luận agent hơn baseline nếu chưa đo.

## Tests

```bash
pytest --junitxml=outputs/tests.xml
```

Tests bao gồm reference isolation, leakage, whitelist/injection, budgets/zero-search, retrieval ID/quote grounding, temporal boundaries/unknown metadata, document-aware resolution, hierarchy/source spans, read-only DB, atomic private storage, dataset integrity và checkpoint/resume. Có test crash giữa budget reservation và tool response để bảo đảm resume không hoàn lại lượt gọi. Fixtures tổng hợp chỉ tồn tại trong temporary test directories; không được trộn vào benchmark từ HF.

## PostgreSQL

```bash
uv pip install --python .venv/bin/python '.[postgres]'
export RULE_RETRIEVAL_DATABASE_URL='postgresql+psycopg:///rule_retrieval?host=/var/run/postgresql'
./scripts/build_database.sh
```

SQLAlchemy schema/ingestion hỗ trợ PostgreSQL; retrieval dùng native `to_tsvector('simple',...)` và `ts_rank_cd` thay FTS5. Backend PostgreSQL vì vậy **không phải cùng thuật toán BM25 SQLite**; khi so sánh thí nghiệm phải cố định DB backend. Migration thực hiện bằng re-ingest cùng shard/manifest, không copy trực tiếp SQLite file. PostgreSQL chưa được integration-test trên máy development này. Unix socket PostgreSQL phải được mount/allowlist riêng nếu dùng OS sandbox; script Bubblewrap hiện được kiểm thử cho SQLite + inference UDS. TCP PostgreSQL cần deployment allowlist riêng, không được mở egress tùy ý.

## Cấu trúc và hạn chế

```text
configs/                  datasets, DB, model, harness, evaluation
prompts/                  extraction, roles, alignment, retrieval agent
src/rule_retrieval/
  clients/                OpenAI-compatible client và local model server
  ingestion/              HF download/Parquet stream/statute parser
  database/               SQLAlchemy schema, snapshot, initialization
  dataset_builder/        input, labels, aligner, resolver, leakage audit
  retrieval/              read-only backend interface và FTS5
  temporal/               dates, conservative interval checks
  harness/                protocol, controller, runtime, egress guard
  evaluation/             private standalone evaluator và metrics
  storage/                stable IDs, atomic JSON, append-only logs
  cli.py
scripts/                  executable CLI wrappers, isolation, experiments
tests/
data/{raw,processed,database,benchmark,hf_cache}/
outputs/{ingestion_runs,dataset_runs,agent_runs}/
```

Một shard luật có thể thiếu hầu hết bộ luật Tòa dùng. Metadata hiệu lực và lịch sử sửa đổi chưa đủ để xác minh phiên bản; không tự backfill bằng ngày ban hành. Regex parsing của OCR có thể nhầm các điều được trích trong văn bản sửa đổi; tất cả giữ trạng thái unverified. Extraction/roles/alignment bằng model nhỏ và lexical evidence guards vẫn có thể sai ngữ nghĩa; cần model mạnh hơn/review trước khi dùng cho nghiên cứu kết luận pháp lý. Chưa có expert adjudication, dense retrieval hoặc validated historical reconstruction. Bổ sung coverage về sau cần chọn thêm shard **tường minh**, ghim provenance và tạo DB/dataset run mới; không lặng lẽ đổi benchmark cũ.

Prompt mặc định của v1 chọn một câu hỏi cho mỗi case để smoke test; schema, aligner, dispatcher và evaluator hỗ trợ nhiều issues và quan hệ many-to-many. Chưa coi extraction này là danh sách đầy đủ mọi vấn đề/quy phạm của một bản án. Các applied citations không gán được giữ riêng thay vì ép vào issue đang chọn.

Kết quả chạy thực tế và ví dụ input/reference/prediction tách riêng được ghi ở `RUN_REPORT.md` sau smoke test.
