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

`test_pipeline.py` dung `dataset/data-dev.csv`.

```bash
# Chay toan bo dataset
python test_pipeline.py

# Gioi han so row
python test_pipeline.py --limit 5

# Chon row cu the
python test_pipeline.py --cases 0 3 7-10

# Bo qua A2 security
python test_pipeline.py --no-secu

# Bo qua Rego intent
python test_pipeline.py --no-rego

# Dung sau A4, khong deploy len AWS
python test_pipeline.py --no-deploy

# Giu lai resources sau apply, khong auto-destroy
python test_pipeline.py --no-destroy

# Chay song song nhieu row
python test_pipeline.py --workers 3

# Luu ket qua ra file khac
python test_pipeline.py --out reviews/my_results.json
```

Ket qua mac dinh duoc luu vao `reviews/pipeline_results.json`.

Phan tich nhanh ket qua benchmark:

```bash
python dataset/analyze_results.py reviews/pipeline_results.json
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

Ket qua `[data]`, `[rego]`, va `[deploy]` duoc record rieng. Neu A4 validation
pass thi pipeline van chay A5 deploy de danh gia deployability, ke ca khi
Resource/Rego fail. JSON result co `final_eval` de phan biet:

- `dataset_resource_ok`: match cot `Resource`/`esource`
- `intent_literal_ok`: match cac literal ro rang trich tu `Prompt`/`Intent`
- `terraform_validation_ok`: A4 validate/plan pass
- `rego_intent_ok`: match cot `Rego intent`
- `deploy_ok`: apply/destroy AWS thanh cong
- `predeploy_strict_ok`: A4 + Resource + Rego deu pass
- `end_to_end_strict_ok`: predeploy strict + Deploy pass

A2 security van chi dung CKV/Checkov nhu cu.

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
