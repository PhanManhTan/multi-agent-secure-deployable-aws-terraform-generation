# Multi-Agent Terraform Generation

Multi-agent pipeline sinh Terraform IaC tu mo ta ngon ngu tu nhien, dung LangGraph + NVIDIA NIM hoac DeepSeek.

## Yeu cau chung

- Python 3.11+
- Git
- Terraform CLI
- Checkov
- AWS CLI v2 va AWS credentials neu chay buoc deploy/apply that
- API key cho NVIDIA NIM hoac DeepSeek

## Cai dat tren Ubuntu

Vi du ben duoi phu hop voi Ubuntu 22.04/24.04.

**1. Cai system packages**

```bash
sudo apt update
sudo apt install -y git curl unzip gnupg software-properties-common
sudo apt install -y python3.11 python3.11-venv python3.11-dev
```

Neu Ubuntu cua ban khong co goi `python3.11`, hay cai Python 3.11 tu deadsnakes PPA hoac dung phien ban Ubuntu moi hon.

**2. Clone repo**

```bash
git clone https://github.com/noseyug/multi-agent-secure-deployable-aws-terraform-generation.git
cd multi-agent-secure-deployable-aws-terraform-generation
```

**3. Tao virtual environment va cai Python dependencies**

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

**4. Cai Terraform**

```bash
wget -O- https://apt.releases.hashicorp.com/gpg \
  | gpg --dearmor \
  | sudo tee /usr/share/keyrings/hashicorp-archive-keyring.gpg >/dev/null

echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/hashicorp.list

sudo apt update
sudo apt install -y terraform
```

**5. Cai OPA neu muon chay stage Rego intent**

```bash
curl -L -o opa https://openpolicyagent.org/downloads/latest/opa_linux_amd64_static
chmod +x opa
sudo mv opa /usr/local/bin/opa
```

**6. Cai AWS CLI v2**

```bash
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip awscliv2.zip
sudo ./aws/install
rm -rf aws awscliv2.zip
```

**7. Tao file cau hinh**

```bash
cp .env.example .env
```

Mo `.env` va dien API key:

```env
LLM_PROVIDER=nvidia
NVIDIA_API_KEY=nvapi-...
```

Hoac dung DeepSeek:

```env
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_MODEL=deepseek-chat
```

**8. Kiem tra toolchain**

```bash
source .venv/bin/activate
python --version
terraform version
checkov --version
aws --version
```

Tat ca cac lenh tren can chay duoc truoc khi chay pipeline. Rieng `opa version`
chi bat buoc neu ban muon stage Rego intent chay that:

```bash
opa version
```

## Cai dat tren Windows

**1. Clone repo**

```bat
git clone https://github.com/noseyug/multi-agent-secure-deployable-aws-terraform-generation.git
cd multi-agent-secure-deployable-aws-terraform-generation
```

**2. Chay setup**

```bat
setup.bat
```

Hoac dung PowerShell:

```powershell
.\setup.ps1
```

Script Windows tu dong:

- Tao virtual environment `.venv`
- Cai Python dependencies
- Tai `terraform.exe` vao `bin\`
- Tai va cai AWS CLI v2 vao `bin\awscli\` can quyen Administrator
- Tao file `.env` tu `.env.example`

**3. Dien API keys vao `.env`**

```env
NVIDIA_API_KEY=nvapi-...
```

## Su dung

### Activate virtual environment

Ubuntu:

```bash
source .venv/bin/activate
```

Windows:

```bat
.venv\Scripts\activate
```

### Chay mot prompt

`main.py` can mot prompt neu khong dung `--batch`.

```bash
python main.py "Create an S3 bucket with versioning and server-side encryption"
```

Luu Terraform HCL ra file:

```bash
python main.py "Create a private VPC with two subnets" --output infra.tf
```

Destroy resources tu file Terraform da luu:

```bash
python main.py --destroy infra.tf
```

### Chay test pipeline tren dataset

Mac dinh `test_pipeline.py` dung `dataset/data-dev.csv` de chay nhanh. Khi can
danh gia benchmark lon hon, truyen `--csv dataset/data-filtered.csv`.

```bash
# Chay toan bo dataset mac dinh nho
python test_pipeline.py

# Chay benchmark filtered 174 cases
python test_pipeline.py --csv dataset/data-filtered.csv --cases 0-173 --workers 4 --out reviews/pipeline_results_filtered_full.json

# Gioi han so case
python test_pipeline.py --limit 5

# Chon case cu the
python test_pipeline.py --cases 0 3 7-10

# Chon case tren dataset khac
python test_pipeline.py --csv dataset/data-filtered.csv --cases 50 59 81

# Bo qua A2 security
python test_pipeline.py --no-secu

# Bo qua Rego intent
python test_pipeline.py --no-rego

# Dung sau A4, khong deploy len AWS
python test_pipeline.py --no-deploy

# Giu lai resources sau apply, khong auto-destroy
python test_pipeline.py --no-destroy

# Chay song song nhieu case
python test_pipeline.py --workers 3

# Luu ket qua ra file khac
python test_pipeline.py --out reviews/my_results.json
```

Ket qua mac dinh duoc luu vao `reviews/pipeline_results.json`.

Phan tich nhanh ket qua benchmark:

```bash
python dataset/analyze_results.py reviews/pipeline_results.json

# Neu result duoc tao tu dataset/data-filtered.csv thi truyen cung CSV
python dataset/analyze_results.py reviews/pipeline_results_filtered_full.json --csv dataset/data-filtered.csv

# Phan tich file benchmark 174 cases hien tai neu file co trong workspace
python dataset/analyze_results.py result_full_174.json --csv dataset/data-filtered.csv
```

Kiem tra nhanh cac rule/parser/classifier noi bo truoc khi ton LLM/AWS:

```bash
python -m unittest tests/test_static_rules.py
python -m py_compile agents/architecture.py agents/deployment.py agents/validation.py core/terraform.py test_pipeline.py dataset/analyze_results.py
```

Sau A4 validation pass, test pipeline se them `[data]` eval de so generated HCL
da validate/plan duoc voi dataset:

- cot `Resource`/`esource`: tach required resource va helper/data source. Required
  resource dung de tinh `dataset_resource_ok`; helper nhu `aws_iam_policy_document`,
  `aws_availability_zones`, `archive_file` chi ghi warning/coverage rieng.
- cot `Reference output`: tinh coverage voi Terraform mau de tham khao, nhung
  khong block deploy vi code khac reference van co the dung.
- cot `Intent`: duoc record vao JSON result de reviewer doc.
  Mot so literal ro rang trong `Prompt`/`Intent` nhu `lambda.js`,
  `custom_ttl_attribute`, `password1`, `cron(...)`, `BucketOwner`, `log/`
  duoc kiem tra boi `intent_literal_match`. Day la check tinh, khong thay the
  Rego/semantic review, nhung giup bat loi "dung resource nhung sai literal".

Stage Rego chay sau A4 validation va truoc A5 deploy. Day la stage danh gia
intent rieng, khong phai A2 security. Stage nay lay cot `Rego intent` trong
dataset, chay `terraform plan -out=tfplan`, `terraform show -json tfplan`, roi
dung `opa eval` de kiem tra cac rule entrypoint pho bien nhu `valid`,
`is_configuration_valid`, `has_valid_resources`, hoac cac rule khai bao
`default <rule> = false`. Rego uu tien rule tong (`valid`, `allow`,
`is_configuration_valid`, `has_valid_resources`, `valid_configuration`) truoc;
neu khong co rule tong thi moi fallback sang rule con va record `entrypoint_type`.

Kết quả `[data]`, `[rego]`, và `[deploy]` được record riêng. Nếu A4 validation
pass thì pipeline vẫn chạy A5 deploy để đánh giá deployability, kể cả khi
Resource/Rego fail. JSON result có `final_eval` để phân biệt các loại thành công
và thất bại.

### Ý nghĩa các thuộc tính đánh giá

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

Khi đánh giá chất lượng code sinh ra, ưu tiên đọc `Code predeploy`,
`Deployable code` và `Adjusted code-success`. `Strict end-to-end` vẫn hữu ích
cho benchmark, nhưng có thể fail vì Rego dataset quá chặt hoặc AWS account bị
giới hạn, không nhất thiết là lỗi generated Terraform.

A2 security van chi dung CKV/Checkov nhu cu.

### Kết quả benchmark hiện tại

Kết quả mới nhất trong workspace được lưu ở `result_full_174.json`, chạy trên
`dataset/data-filtered.csv` với 174 cases. Tóm tắt từ
`python dataset/analyze_results.py result_full_174.json --csv dataset/data-filtered.csv`:

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
- Benchmark-only Rego: 41 case, code đã qua các gate thực dụng nhưng Rego/dataset
  có check quá cụ thể hoặc conflict với deployability.
- AWS environment/quota: 7 case, bị chặn bởi subscription/quota/permission của
  account hoặc region, không nên tính là lỗi sinh Terraform.

Nhóm ưu tiên tiếp theo:

- A4 validation/schema repair: cases 18, 27, 31, 32, 42, 45, 61, 68, 82, 84,
  115, 120, 126, 130, 132, 134, 139, 141, 158, 166.
- A5 deployability repair: cases 0, 21, 47, 56, 60, 64, 79, 101, 117, 121,
  165, 167.
- A1/A3 intent coverage: cases 28, 29, 50, 74, 76, 80, 114, 116, 122.
- A1 architecture templates: cases 78, 161, 162.
- Dataset/Rego audit: 37 case được analyzer gán owner `benchmark_dataset_rego_audit`.

Luu y: `main.py --batch` hien dang tro toi `dataset/data-dev-fast.csv`. Neu file nay khong ton tai trong repo cua ban, hay dung `test_pipeline.py` nhu tren hoac tao file dataset tuong ung.

## Bien moi truong quan trong

File `.env.example` co cac bien mac dinh. Cac bien thuong can sua:

- `LLM_PROVIDER`: `nvidia` hoac `deepseek`
- `NVIDIA_API_KEY`: API key khi dung NVIDIA NIM
- `NVIDIA_MODEL`: model NVIDIA NIM
- `DEEPSEEK_API_KEY`: API key khi dung DeepSeek
- `DEEPSEEK_MODEL`: model DeepSeek
- `TF_PLAN_TIMEOUT`: timeout cho `terraform plan`
- `CHECKOV_BIN`: duong dan toi Checkov neu `checkov` khong nam trong PATH

Neu chay deploy/apply that len AWS, can cau hinh AWS credentials truoc:

```bash
aws configure
```

Hoac export bien moi truong:

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=us-east-1
```

## Cau truc

```text
agents/      # 5 agents: architecture, security, engineering, validation, deployment
core/        # LLM, state, terraform wrapper, parsers
prompts/     # System prompts cho tung agent
graph.py     # LangGraph pipeline
main.py      # Entry point
```
