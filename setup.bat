@echo off
setlocal enabledelayedexpansion

echo === Multi-Agent Terraform Generation - Windows Setup ===
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.11+ from https://www.python.org/downloads/
    exit /b 1
)
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo [OK] Python %PY_VER% found

:: Create venv
if exist .venv (
    echo [SKIP] .venv already exists
) else (
    echo [INFO] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 ( echo [ERROR] Failed to create venv & exit /b 1 )
    echo [OK] .venv created
)

:: Activate and install Python deps
echo [INFO] Installing Python dependencies...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip --quiet
pip install -r requirements.txt
if errorlevel 1 ( echo [ERROR] pip install failed & exit /b 1 )
echo [OK] Python dependencies installed

:: Create bin/ if not exists
if not exist bin mkdir bin

:: Download Terraform into bin/
if exist bin\terraform.exe (
    echo [SKIP] Terraform already in bin\terraform.exe
) else (
    echo [INFO] Downloading Terraform 1.10.5...
    powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://releases.hashicorp.com/terraform/1.10.5/terraform_1.10.5_windows_amd64.zip' -OutFile 'bin\terraform.zip'"
    if errorlevel 1 ( echo [ERROR] Terraform download failed & exit /b 1 )
    powershell -NoProfile -Command "Expand-Archive -Path 'bin\terraform.zip' -DestinationPath 'bin' -Force"
    del bin\terraform.zip >nul 2>&1
    echo [OK] terraform.exe saved to bin\
)

:: Download AWS CLI v2 into bin/
if exist bin\aws.exe (
    echo [SKIP] AWS CLI already in bin\aws.exe
) else (
    echo [INFO] Downloading AWS CLI v2...
    powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://awscli.amazonaws.com/AWSCLIV2.msi' -OutFile 'bin\AWSCLIV2.msi'"
    if errorlevel 1 ( echo [ERROR] AWS CLI download failed & exit /b 1 )
    echo [INFO] Installing AWS CLI v2 silently (requires admin)...
    msiexec /i bin\AWSCLIV2.msi /quiet /norestart INSTALLDIR="%~dp0bin\awscli"
    del bin\AWSCLIV2.msi >nul 2>&1
    :: Create aws.exe shim pointing to installed location
    echo @"%~dp0bin\awscli\aws.exe" %%* > bin\aws.bat
    echo [OK] AWS CLI v2 installed to bin\awscli\
)

:: Patch activate.bat to add bin\ to PATH automatically
findstr /c:"matg-bin-path" .venv\Scripts\activate.bat >nul 2>&1
if errorlevel 1 (
    echo.>> .venv\Scripts\activate.bat
    echo rem matg-bin-path>> .venv\Scripts\activate.bat
    echo set "PATH=%~dp0..\..\bin;%~dp0..\..\bin\awscli;%%PATH%%">> .venv\Scripts\activate.bat
    echo [OK] bin\ added to venv PATH
) else (
    echo [SKIP] bin\ already in venv PATH
)

:: Copy .env
if not exist .env (
    if exist .env.example (
        copy .env.example .env >nul
        echo [OK] .env created from .env.example — fill in your API keys
    )
) else (
    echo [SKIP] .env already exists
)

echo.
echo === Setup complete ===
echo.
echo Included:
echo   - Python packages: langgraph, checkov, langchain, ...
echo   - bin\terraform.exe  ^(Terraform 1.10.5^)
echo   - bin\awscli\aws.exe ^(AWS CLI v2^)
echo.
echo Usage:
echo   .venv\Scripts\activate       ^<-- activate
echo   python main.py               ^<-- run pipeline
echo   terraform version            ^<-- verify terraform
echo   aws --version                ^<-- verify AWS CLI
echo.
