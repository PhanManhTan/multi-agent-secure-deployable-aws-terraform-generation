# Multi-Agent Terraform Generation

Multi-agent pipeline sinh Terraform IaC từ mô tả ngôn ngữ tự nhiên, dùng LangGraph + NVIDIA NIM hoặc DeepSeek.

## Yêu Cầu Chung

- Python 3.11+
- Git
- Terraform CLI
- Checkov
- AWS CLI v2 và AWS credentials nếu chạy bước deploy/apply thật
- API key cho NVIDIA NIM hoặc DeepSeek

## Cài Đặt Trên Ubuntu

Ví dụ bên dưới phù hợp với Ubuntu 22.04/24.04.

**1. Cài system packages**

```bash
sudo apt update
sudo apt install -y git curl unzip gnupg software-properties-common
sudo apt install -y python3.11 python3.11-venv python3.11-dev
```

Nếu Ubuntu của bạn không có gói `python3.11`, hãy cài Python 3.11 từ deadsnakes PPA hoặc dùng phiên bản Ubuntu mới hơn.

**2. Clone repo**

```bash
git clone https://github.com/noseyug/multi-agent-secure-deployable-aws-terraform-generation.git
cd multi-agent-secure-deployable-aws-terraform-generation
```

**3. Tạo `.venv` trước**

Tạo và activate `.venv` ngay sau khi clone repo. Tất cả lệnh Python trong phần sau nên chạy bên trong environment này.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
```

Kiểm tra Python đang trỏ vào `.venv`:

```bash
which python
python --version
```

**4. Cài Python dependencies**

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

**5. Cài Terraform**

```bash
wget -O- https://apt.releases.hashicorp.com/gpg \
  | gpg --dearmor \
  | sudo tee /usr/share/keyrings/hashicorp-archive-keyring.gpg >/dev/null

echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/hashicorp.list

sudo apt update
sudo apt install -y terraform
```

**6. Cài OPA nếu muốn chạy stage Rego intent**

```bash
curl -L -o opa https://openpolicyagent.org/downloads/latest/opa_linux_amd64_static
chmod +x opa
sudo mv opa /usr/local/bin/opa
```

**7. Cài AWS CLI v2**

```bash
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip awscliv2.zip
sudo ./aws/install
rm -rf aws awscliv2.zip
```

**8. Tạo file cấu hình**

```bash
cp .env.example .env
```

Mở `.env` và điền API key:

```env
LLM_PROVIDER=nvidia
NVIDIA_API_KEY=nvapi-...
```

Hoặc dùng DeepSeek:

```env
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_MODEL=deepseek-chat
```

**9. Kiểm tra toolchain**

```bash
source .venv/bin/activate
python --version
terraform version
checkov --version
aws --version
```

Tất cả các lệnh trên cần chạy được trước khi chạy pipeline. Riêng `opa version` chỉ bắt buộc nếu bạn muốn stage Rego intent chạy thật:

```bash
opa version
```

## Cài Đặt Trên Windows

**1. Clone repo**

```bat
git clone https://github.com/noseyug/multi-agent-secure-deployable-aws-terraform-generation.git
cd multi-agent-secure-deployable-aws-terraform-generation
```

**2. Chạy setup**

```bat
setup.bat
```

Hoặc dùng PowerShell:

```powershell
.\setup.ps1
```

Script Windows tự động:

- Tạo virtual environment `.venv`
- Cài Python dependencies
- Tải `terraform.exe` vào `bin\`
- Tải và cài AWS CLI v2 vào `bin\awscli\` nếu có quyền Administrator
- Tạo file `.env` từ `.env.example`

**3. Điền API keys vào `.env`**

```env
NVIDIA_API_KEY=nvapi-...
```

## Sử Dụng

### Activate virtual environment

Ubuntu:

```bash
source .venv/bin/activate
```

Windows:

```bat
.venv\Scripts\activate
```

### Chạy một prompt

`main.py` cần một prompt nếu không dùng `--batch`.

```bash
python main.py "Create an S3 bucket with versioning and server-side encryption"
```

Lưu Terraform HCL ra file:

```bash
python main.py "Create a private VPC with two subnets" --output infra.tf
```

Destroy resources từ file Terraform đã lưu:

```bash
python main.py --destroy infra.tf
```

### Chạy benchmark pipeline trên dataset

Mặc định `benchmark_pipeline.py` dùng `dataset/data-dev.csv` để chạy nhanh. Khi cần đánh giá benchmark lớn hơn, truyền `--csv dataset/data-filtered.csv`.

```bash
# Chạy toàn bộ dataset mặc định nhỏ
python benchmark_pipeline.py

# Chạy benchmark filtered 174 cases
python benchmark_pipeline.py --csv dataset/data-filtered.csv --cases 0-173 --workers 4 --out reviews/pipeline_results_filtered_full.json

# Giới hạn số case
python benchmark_pipeline.py --limit 5

# Chọn case cụ thể
python benchmark_pipeline.py --cases 0 3 7-10

# Chọn case trên dataset khác
python benchmark_pipeline.py --csv dataset/data-filtered.csv --cases 50 59 81

# Bỏ qua A2 security
python benchmark_pipeline.py --no-secu

# Bỏ qua Rego intent
python benchmark_pipeline.py --no-rego

# Dừng sau A4, không deploy lên AWS
python benchmark_pipeline.py --no-deploy

# Giữ lại resources sau apply, không auto-destroy
python benchmark_pipeline.py --no-destroy

# Chạy song song nhiều case
python benchmark_pipeline.py --workers 3

# Lưu kết quả ra file khác
python benchmark_pipeline.py --out reviews/my_results.json
```

Kết quả mặc định được lưu vào `reviews/pipeline_results.json`.

Phân tích nhanh kết quả benchmark:

```bash
python dataset/analyze_results.py reviews/pipeline_results.json

# Nếu result được tạo từ dataset/data-filtered.csv thì truyền cùng CSV
python dataset/analyze_results.py reviews/pipeline_results_filtered_full.json --csv dataset/data-filtered.csv

# Phân tích file benchmark 174 cases hiện tại nếu file có trong workspace
python dataset/analyze_results.py result_full_174.json --csv dataset/data-filtered.csv
```

Kiểm tra nhanh các rule/parser/classifier nội bộ trước khi tốn LLM/AWS:

```bash
python -m unittest tests/test_static_rules.py
python -m py_compile agents/architecture.py agents/deployment.py agents/validation.py core/terraform.py benchmark_pipeline.py dataset/analyze_results.py
```

## Luồng Đánh Giá Benchmark

Sau A4 validation pass, benchmark pipeline thêm `[data]` eval để so generated HCL đã validate/plan được với dataset:

- Cột `Resource`/`esource`: tách required resource và helper/data source. Required resource dùng để tính `dataset_resource_ok`; helper như `aws_iam_policy_document`, `aws_availability_zones`, `archive_file` chỉ ghi warning/coverage riêng.
- Cột `Reference output`: tính coverage với Terraform mẫu để tham khảo, nhưng không block deploy vì code khác reference vẫn có thể đúng.
- Cột `Intent`: được record vào JSON result để reviewer đọc.
- Một số literal rõ ràng trong `Prompt`/`Intent` như `lambda.js`, `custom_ttl_attribute`, `password1`, `cron(...)`, `BucketOwner`, `log/` được kiểm tra bằng `intent_literal_match`. Đây là static check, không thay thế Rego/semantic review, nhưng giúp bắt lỗi “đúng resource nhưng sai literal”.

Stage Rego chạy sau A4 validation và trước A5 deploy. Đây là stage đánh giá intent riêng, không phải A2 security. Stage này lấy cột `Rego intent` trong dataset, chạy `terraform plan -out=tfplan`, `terraform show -json tfplan`, rồi dùng `opa eval` để kiểm tra các rule entrypoint phổ biến như `valid`, `is_configuration_valid`, `has_valid_resources`, hoặc các rule khai báo `default <rule> = false`.

Rego ưu tiên rule tổng (`valid`, `allow`, `is_configuration_valid`, `has_valid_resources`, `valid_configuration`) trước. Nếu không có rule tổng thì mới fallback sang rule con và record `entrypoint_type`.

Kết quả `[data]`, `[rego]`, và `[deploy]` được record riêng. Nếu A4 validation pass thì pipeline vẫn chạy A5 deploy để đánh giá deployability, kể cả khi Resource/Rego fail. JSON result có `final_eval` để phân biệt các loại thành công và thất bại.

## Ý Nghĩa Các Thuộc Tính Đánh Giá

Các thuộc tính quan trọng trong `final_eval`:

| Thuộc tính | Ý nghĩa |
| --- | --- |
| `dataset_resource_ok` | Generated Terraform có đủ resource bắt buộc theo cột `Resource`/`esource` của dataset. Helper/data source được tách riêng để không làm sai điểm resource chính. |
| `intent_literal_ok` | Các literal rõ ràng trong `Prompt`/`Intent` như `lambda.js`, `custom_ttl_attribute`, `password1`, `cron(...)`, `BucketOwner`, `log/` có xuất hiện đúng trong code. |
| `terraform_validation_ok` | A4 chạy Terraform validate/plan thành công. Đây là cổng kiểm tra cú pháp/schema/provider trước khi deploy. |
| `rego_intent_ok` | Code thỏa rule trong cột `Rego intent`. Đây là benchmark gate, không đồng nghĩa tuyệt đối với deployability vì một số Rego có check quá cụ thể. |
| `deploy_ok` | Terraform apply lên AWS và auto-destroy thành công. |
| `predeploy_strict_ok` | `terraform_validation_ok` + `dataset_resource_ok` + `rego_intent_ok` đều pass, chưa tính deploy AWS. |
| `end_to_end_strict_ok` | `predeploy_strict_ok` + `deploy_ok`. Đây là điểm strict benchmark nghiêm ngặt nhất. |
| `code_predeploy_ok` | Code pass Terraform validation, resource match và literal intent; chưa tính Rego và AWS deploy. |
| `deployable_code_ok` | Code validate được và deploy được trên AWS; dùng để đánh giá khả năng chạy thực tế, bỏ qua Rego benchmark. |
| `adjusted_code_success_ok` | Thành công thực dụng: code deploy được, hoặc chỉ bị chặn bởi môi trường AWS/quota. Đây là metric nên dùng khi đánh giá chất lượng code sinh ra. |
| `benchmark_only_rego_fail` | Rego fail nhưng code vẫn validate/resource/literal/deploy OK. Nên đưa vào nhóm audit dataset/Rego, không vội tính là lỗi code. |
| `deploy_environment_blocked` | Code qua các gate chính nhưng AWS account/region/quota/subscription chặn deploy. |

Các `failed_dimensions` thường gặp:

| Dimension | Ý nghĩa |
| --- | --- |
| `architecture` | A1 không sinh được architecture plan hợp lệ, thường là không có resource bắt buộc. |
| `engineering` | A3 không sinh được Terraform HCL dùng được. |
| `terraform_validation` | A4 validate/plan fail do cú pháp, schema provider, logic Terraform hoặc lỗi init/timeout. |
| `dataset_resource` | Thiếu resource bắt buộc theo dataset. |
| `intent_literal` | Thiếu literal rõ ràng trong prompt/intent. |
| `rego_intent` | Không pass Rego intent benchmark. |
| `aws_deploy` | Terraform apply AWS fail; cần phân biệt lỗi code với lỗi môi trường/quota. |

Khi đánh giá chất lượng code sinh ra, ưu tiên đọc `Code predeploy`, `Deployable code` và `Adjusted code-success`. `Strict end-to-end` vẫn hữu ích cho benchmark, nhưng có thể fail vì Rego dataset quá chặt hoặc AWS account bị giới hạn, không nhất thiết là lỗi generated Terraform.

A2 security vẫn chỉ dùng CKV/Checkov như cũ.

## Kết Quả Benchmark Hiện Tại

Kết quả mới nhất trong workspace được lưu ở `result_full_174.json`, chạy trên `dataset/data-filtered.csv` với 174 cases. Tóm tắt từ:

```bash
python dataset/analyze_results.py result_full_174.json --csv dataset/data-filtered.csv
```

| Chỉ số | Kết quả | Ghi chú |
| --- | ---: | --- |
| A1 architecture | 171/174 (98.3%) | 3 case architecture không có resource |
| A3 engineering | 171/174 (98.3%) | Các case A1 fail không sang A3 |
| A4 Terraform validate/plan | 151/174 (86.8%) | 16 SYNTAX, 2 INFRA, 2 LOGIC |
| Dataset resource match | 150/174 (86.2%) | 9 case thiếu resource theo dataset |
| Rego intent | 99/174 (56.9%) | Benchmark gate, nhiều rule Rego quá chặt |
| AWS deploy OK | 131/174 (75.3%) | Apply/destroy thành công trên AWS |
| Predeploy strict | 91/174 (52.3%) | A4 + Resource + Rego |
| Strict end-to-end | 81/174 (46.6%) | Predeploy strict + deploy OK |
| Code predeploy | 141/174 (81.0%) | A4 + Resource + Intent literal, chưa tính Rego/AWS |
| Deployable code | 122/174 (70.1%) | Code validate và deploy được trên AWS |
| Adjusted code-success | 129/174 (74.1%) | Bỏ qua benchmark-only Rego và AWS env/quota block |

Các `failed_dimensions` trong `result_full_174.json`:

- `rego_intent`: 60 cases
- `aws_deploy`: 28 cases
- `terraform_validation`: 20 cases
- `dataset_resource`: 9 cases
- `intent_literal`: 3 cases
- `architecture`: 3 cases

Phân loại theo hướng xử lý:

- Pipeline/code cần sửa: 45 case theo `adjusted_code_success_ok = false`.
- Benchmark-only Rego: 41 case, code đã qua các gate thực dụng nhưng Rego/dataset có check quá cụ thể hoặc conflict với deployability.
- AWS environment/quota: 7 case, bị chặn bởi subscription/quota/permission của account hoặc region, không nên tính là lỗi sinh Terraform.

Nhóm ưu tiên tiếp theo:

- A4 validation/schema repair: cases 18, 27, 31, 32, 42, 45, 61, 68, 82, 84, 115, 120, 126, 130, 132, 134, 139, 141, 158, 166.
- A5 deployability repair: cases 0, 21, 47, 56, 60, 64, 79, 101, 117, 121, 165, 167.
- A1/A3 intent coverage: cases 28, 29, 50, 74, 76, 80, 114, 116, 122.
- A1 architecture templates: cases 78, 161, 162.
- Dataset/Rego audit: 37 case được analyzer gán owner `benchmark_dataset_rego_audit`.

Lưu ý: `main.py --batch` hiện đang trỏ tới `dataset/data-dev-fast.csv`. Nếu file này không tồn tại trong repo của bạn, hãy dùng `benchmark_pipeline.py` như trên hoặc tạo file dataset tương ứng.

## Biến Môi Trường Quan Trọng

File `.env.example` có các biến mặc định. Các biến thường cần sửa:

- `LLM_PROVIDER`: `nvidia` hoặc `deepseek`
- `NVIDIA_API_KEY`: API key khi dùng NVIDIA NIM
- `NVIDIA_MODEL`: model NVIDIA NIM
- `DEEPSEEK_API_KEY`: API key khi dùng DeepSeek
- `DEEPSEEK_MODEL`: model DeepSeek
- `TF_PLAN_TIMEOUT`: timeout cho `terraform plan`
- `CHECKOV_BIN`: đường dẫn tới Checkov nếu `checkov` không nằm trong PATH

Nếu chạy deploy/apply thật lên AWS, cần cấu hình AWS credentials trước:

```bash
aws configure
```

Hoặc export biến môi trường:

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=us-east-1
```

## Cấu Trúc

```text
agents/      # 5 agents: architecture, security, engineering, validation, deployment
core/        # LLM, state, terraform wrapper, parsers
prompts/     # System prompts cho từng agent
graph.py     # LangGraph pipeline
main.py      # Entry point
```
