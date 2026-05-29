#Requires -Version 5.1
<#
.SYNOPSIS
    Setup virtual environment for Multi-Agent Terraform Generation project.
.EXAMPLE
    .\setup.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$TF_VERSION  = "1.10.5"
$ProjectRoot = $PSScriptRoot

Write-Host "=== Multi-Agent Terraform Generation - Windows Setup ===" -ForegroundColor Cyan
Write-Host ""

# Check Python
try {
    $pyVersion = python --version 2>&1
    Write-Host "[OK] $pyVersion found" -ForegroundColor Green
} catch {
    Write-Host "[ERROR] Python not found. Install 3.11+ from https://www.python.org/downloads/" -ForegroundColor Red
    exit 1
}

# Create venv
if (Test-Path ".venv") {
    Write-Host "[SKIP] .venv already exists" -ForegroundColor Yellow
} else {
    Write-Host "[INFO] Creating virtual environment..." -ForegroundColor Cyan
    python -m venv .venv
    Write-Host "[OK] .venv created" -ForegroundColor Green
}

# Activate
& .\.venv\Scripts\Activate.ps1

# Install Python deps
Write-Host "[INFO] Installing Python dependencies..." -ForegroundColor Cyan
python -m pip install --upgrade pip --quiet
pip install -r requirements.txt
Write-Host "[OK] Python dependencies installed" -ForegroundColor Green

# Create bin/
if (-not (Test-Path "bin")) { New-Item -ItemType Directory -Path "bin" | Out-Null }

# Download Terraform
if (Test-Path "bin\terraform.exe") {
    Write-Host "[SKIP] Terraform already in bin\terraform.exe" -ForegroundColor Yellow
} else {
    Write-Host "[INFO] Downloading Terraform $TF_VERSION..." -ForegroundColor Cyan
    $tfUrl = "https://releases.hashicorp.com/terraform/$TF_VERSION/terraform_${TF_VERSION}_windows_amd64.zip"
    Invoke-WebRequest -Uri $tfUrl -OutFile "bin\terraform.zip"
    Expand-Archive -Path "bin\terraform.zip" -DestinationPath "bin" -Force
    Remove-Item "bin\terraform.zip"
    Write-Host "[OK] terraform.exe saved to bin\" -ForegroundColor Green
}

# Download and install AWS CLI v2
if (Test-Path "bin\aws.exe") {
    Write-Host "[SKIP] AWS CLI already in bin\aws.exe" -ForegroundColor Yellow
} else {
    Write-Host "[INFO] Downloading AWS CLI v2..." -ForegroundColor Cyan
    Invoke-WebRequest -Uri "https://awscli.amazonaws.com/AWSCLIV2.msi" -OutFile "bin\AWSCLIV2.msi"
    Write-Host "[INFO] Installing AWS CLI v2 silently (requires admin)..." -ForegroundColor Cyan
    $installDir = Join-Path $ProjectRoot "bin\awscli"
    Start-Process msiexec.exe -ArgumentList "/i `"bin\AWSCLIV2.msi`" /quiet /norestart INSTALLDIR=`"$installDir`"" -Wait
    Remove-Item "bin\AWSCLIV2.msi"
    # Create shim
    "@`"$installDir\aws.exe`" %*" | Out-File -FilePath "bin\aws.bat" -Encoding ASCII
    Write-Host "[OK] AWS CLI v2 installed to bin\awscli\" -ForegroundColor Green
}

# Patch Activate.ps1 to add bin\ to PATH
$activatePs1 = ".venv\Scripts\Activate.ps1"
$marker = "# matg-bin-path"
$activateContent = Get-Content $activatePs1 -Raw
if ($activateContent -notmatch [regex]::Escape($marker)) {
    $patch = "`n$marker`n`$env:PATH = `"$ProjectRoot\bin;$ProjectRoot\bin\awscli;`$env:PATH`"`n"
    Add-Content -Path $activatePs1 -Value $patch
    Write-Host "[OK] bin\ added to venv PATH" -ForegroundColor Green
} else {
    Write-Host "[SKIP] bin\ already in venv PATH" -ForegroundColor Yellow
}

# Copy .env
if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Write-Host "[OK] .env created from .env.example - fill in your API keys" -ForegroundColor Green
    }
} else {
    Write-Host "[SKIP] .env already exists" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Included:"
Write-Host "  - Python packages: langgraph, checkov, langchain, ..."
Write-Host "  - bin\terraform.exe  (Terraform $TF_VERSION)"
Write-Host "  - bin\awscli\aws.exe (AWS CLI v2)"
Write-Host ""
Write-Host "Usage:"
Write-Host "  .\.venv\Scripts\Activate.ps1   <- activate"
Write-Host "  python main.py                 <- run pipeline"
Write-Host "  terraform version              <- verify terraform"
Write-Host "  aws --version                  <- verify AWS CLI"
Write-Host ""
