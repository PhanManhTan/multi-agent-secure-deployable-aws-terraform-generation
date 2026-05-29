"""Wrapper cho Terraform CLI, Checkov, và Floci — dùng chung cho toàn pipeline.

_TF_ENV đảm bảo mọi subprocess đều dùng plugin cache — tránh download
provider hàng trăm lần khi chạy benchmark.
"""
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
# Cache provider giữa các lần gọi terraform — đặt ngoài thư mục tmp
# để tồn tại xuyên suốt toàn bộ benchmark
_TF_CACHE_DIR = Path(__file__).parent.parent / ".tf_plugin_cache"
_TF_CACHE_DIR.mkdir(exist_ok=True)

# Env dùng chung cho mọi subprocess terraform — inject cache dir.
# MAY_BREAK_DEPENDENCY_LOCK_FILE: cho phép dùng plugin cache khi init trong
# thư mục chưa có .terraform.lock.hcl (tránh lỗi checksum mismatch ngẫu nhiên).
_TF_ENV = {
    **os.environ,
    "TF_PLUGIN_CACHE_DIR": str(_TF_CACHE_DIR),
    "TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE": "true",
}

_REQUIRED_TOOLS = ("checkov", "terraform")


def check_required_tools() -> None:
    """Kiểm tra các công cụ bắt buộc có trong PATH không.

    Gọi một lần lúc startup để fail fast thay vì crash giữa benchmark.
    """
    missing = [t for t in _REQUIRED_TOOLS if not shutil.which(t)]
    if missing:
        raise RuntimeError(f"Công cụ chưa được cài: {', '.join(missing)}")


_STUBS_DIR = Path(__file__).parent / "stubs"

# Các pattern HCL có thể reference file local — capture group 1 là path
_LOCAL_FILE_PATTERNS = re.compile(
    r'(?:'
    r'filename\s*=\s*"([^"]+)"'                  # filename = "..."
    r'|source_file\s*=\s*"([^"]+)"'              # source_file = "..."
    r'|source_dir\s*=\s*"([^"]+)"'               # source_dir = "..." (archive_file dir)
    r'|source\s*=\s*"(\.{1,2}/[^"]+)"'           # source = "./..." or "../..." (local only)
    r'|(?:template|config)file?\s*=\s*"([^"]+)"' # template/config = "..."
    r'|file\s*\(\s*"([^"]+)"\s*\)'               # file("...")
    r'|templatefile\s*\(\s*"([^"]+)"'            # templatefile("...", ...)
    r')'
)

_STUB_CONTENT: dict[str, bytes | str] = {
    ".zip": None,   # generated dynamically
    ".py":  "def handler(event, context):\n    return {'statusCode': 200}\n",
    ".js":  "exports.handler = async (event) => ({ statusCode: 200 });\n",
    ".sh":  "#!/bin/bash\necho stub\n",
    ".json": "{}\n",
    ".yaml": "",
    ".yml":  "",
    ".env":  "",
    ".conf": "",
    ".tpl":  "",
    ".txt":  "",
    ".pub":  "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCeIKVffQkauMFfx8SmDv0grmAyvDIKnMDFz7UOyOuKd3doC+YPPe1HaOVbbRvPEAnmfFtFFaBoq9iSVMDSAuHBsbW4TOH6FovNLcNPKzR+VU9AQJ+tWJADTyMJd7KG5peJKM8VPD965F87LZiS0r7mpSbmun7RMmr08Kd8VtNkg8xYisgUn34y0i+kIEiC6y9xaOYaqz+iqjxONTQWKZzDKZB9PewAq/T3GsG7HQnWKsly3PdoK1dYXoLA6tvnTui7Qjsz96f5lgIkECM+/aCUF5OvfJ6/Dt/REs0vmPFzprD/UYLv+3cgTUqx3vQ/DBIhf2T+U/nQT46PzVAIDVu3 user@example\n",
    ".pem":  "",
}

_STUB_ZIP_HANDLER = (
    "def handler(event, context):\n"
    "    return {'statusCode': 200, 'body': 'stub'}\n"
)


def _make_stub_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("handler.py", _STUB_ZIP_HANDLER)
    return buf.getvalue()


def _create_stub_file(path: Path, stub_zip: bytes) -> bytes | None:
    """Tạo stub file phù hợp với extension. Trả về stub_zip bytes nếu vừa tạo."""
    ext = path.suffix.lower()
    if ext not in _STUB_CONTENT and ext not in (".zip",):
        return stub_zip  # extension không biết — bỏ qua
    path.parent.mkdir(parents=True, exist_ok=True)
    if ext == ".zip":
        if stub_zip is None:
            stub_zip = _make_stub_zip()
        path.write_bytes(stub_zip)
    else:
        content = _STUB_CONTENT.get(ext, "")
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    return stub_zip


def write_terraform_dir(tmpdir: str | Path, code: str,
                        files_dir: str | Path | None = None) -> None:
    """Write main.tf + copy stubs + create stub files for any local path reference.

    Scan HCL cho tất cả pattern reference file local (filename, source_file,
    file(), templatefile(), v.v.). Nếu file chưa tồn tại → tạo stub phù hợp
    theo extension để terraform validate/plan/apply không fail vì thiếu file.

    files_dir: thư mục cache chung giữa các agent trong cùng 1 run.
               Lần đầu tạo stub → copy vào files_dir.
               Lần sau → copy từ files_dir thay vì tạo lại.
    """
    d = Path(tmpdir)
    (d / "main.tf").write_text(code, encoding="utf-8")
    if _STUBS_DIR.exists():
        for stub in _STUBS_DIR.iterdir():
            if stub.is_file():
                shutil.copy2(stub, d / stub.name)

    fd = Path(files_dir) if files_dir else None
    if fd:
        fd.mkdir(parents=True, exist_ok=True)

    stub_zip: bytes | None = None
    seen: set[str] = set()
    for m in _LOCAL_FILE_PATTERNS.finditer(code):
        raw = next(g for g in m.groups() if g)  # lấy group đầu tiên không None
        if raw in seen or raw.startswith("${") or raw.startswith("http"):
            continue  # bỏ qua Terraform interpolation và URL
        seen.add(raw)
        file_path = d / raw
        if file_path.exists():
            continue
        # Copy từ cache nếu đã tạo trước đó (vd: A4 đã tạo, A5 copy lại)
        if fd:
            cached = fd / raw
            if cached.exists():
                file_path.parent.mkdir(parents=True, exist_ok=True)
                if cached.is_dir():
                    if not file_path.exists():
                        shutil.copytree(cached, file_path)
                else:
                    shutil.copy2(cached, file_path)
                continue
        # Tạo stub mới
        if not file_path.suffix:
            # path không có extension → là directory (vd: source_dir = "./lambda")
            file_path.mkdir(parents=True, exist_ok=True)
            stub_entry = file_path / "index.js"
            stub_entry.write_text(
                "exports.handler = async (event) => ({ statusCode: 200 });\n",
                encoding="utf-8",
            )
            if fd:
                cached = fd / raw
                cached.mkdir(parents=True, exist_ok=True)
                shutil.copy2(stub_entry, cached / "index.js")
            continue
        stub_zip = _create_stub_file(file_path, stub_zip)
        # Lưu vào cache để agent tiếp theo dùng lại
        if fd and file_path.exists():
            cached = fd / raw
            cached.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file_path, cached)


@contextmanager
def terraform_workdir(run_dir: str | Path | None, subdir: str):
    """Context manager trả về thư mục làm việc cho terraform.

    Nếu run_dir được cung cấp: dùng run_dir/subdir (persistent, không xóa khi exit).
    Nếu không: tạo tempdir tạm thời (xóa khi exit).
    """
    if run_dir:
        d = Path(run_dir) / subdir
        d.mkdir(parents=True, exist_ok=True)
        yield d
    else:
        with tempfile.TemporaryDirectory(prefix=f"tf_{subdir}_") as tmp:
            yield Path(tmp)


def run_terraform(cmd: list[str], cwd: str | Path, timeout: int) -> subprocess.CompletedProcess:
    """Chạy lệnh terraform với plugin cache và timeout tường minh.

    Không bắt TimeoutExpired ở đây — để agent gọi tự xử lý
    vì mỗi agent có cách route khác nhau khi timeout.
    """
    # Timeout với Popen + wait để ensure cleanup (subprocess.run timeout sometimes hangs)
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_TF_ENV,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    except subprocess.TimeoutExpired:
        proc.kill()  # Forcefully terminate if timeout
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass  # Already dead
        raise



_CKV_HEADER = re.compile(r"^Check:\s+(CKV2?_AWS_\d+):")
_CKV_FAILED_RES = re.compile(r"^\s*FAILED for resource:\s+(\S+)", re.MULTILINE)
_REGO_PACKAGE_RE = re.compile(r"(?m)^\s*package\s+([A-Za-z_][A-Za-z0-9_.]*)\s*$")
_REGO_ENTRY_RULES = (
    "valid",
    "is_configuration_valid",
    "has_valid_resources",
    "valid_configuration",
    "allow",
    "pass",
)
_REGO_AGGREGATE_RULES = set(_REGO_ENTRY_RULES)
_REGO_DEFAULT_FALSE_RE = re.compile(
    r"(?m)^\s*default\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?::=|=)\s*false\s*$"
)
_REGO_BOOL_RULE_RE = re.compile(
    r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:if\s*)?\{"
)


def run_checkov_on_hcl(hcl: str, timeout: int = 60,
                       check_ids: list[str] | None = None) -> dict:
    """Chạy Checkov trên HCL string, trả dict structured.

    check_ids: nếu truyền, chỉ chạy các CKV IDs đó (--check flag).
               None = chạy tất cả rules (dùng cho scan toàn bộ).

    Returns:
        {
          "failed_ckv_ids":      sorted list of CKV IDs failed,
          "failed_per_resource": list of (resource_addr, ckv_id),
          "passed_count":        int,
          "failed_count":        int,
          "total_checks":        int,
          "scan_seconds":        float,
        }
    """
    checkov_bin = os.environ.get("CHECKOV_BIN") or shutil.which("checkov")
    if not checkov_bin:
        raise RuntimeError("checkov not found — set CHECKOV_BIN in .env or add to PATH")

    cmd = [checkov_bin, "-d", ".", "--framework", "terraform", "--quiet", "--compact"]
    if check_ids:
        cmd += ["--check", ",".join(sorted(set(check_ids)))]

    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="checkov_") as tmpdir:
        (Path(tmpdir) / "main.tf").write_text(hcl)
        try:
            proc = subprocess.run(
                cmd,
                cwd=tmpdir,
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Checkov timeout after {timeout}s")
        out = proc.stdout + "\n" + proc.stderr

    elapsed = round(time.time() - t0, 2)

    passed = failed = 0
    m = re.search(r"Passed checks:\s*(\d+),\s*Failed checks:\s*(\d+)", out)
    if m:
        passed, failed = int(m.group(1)), int(m.group(2))

    failed_ids: set[str] = set()
    failed_pairs: list[tuple[str, str]] = []
    for block in re.split(r"\n(?=Check:\s+CKV)", out):
        m_id = _CKV_HEADER.match(block)
        if not m_id:
            continue
        ckv_id = m_id.group(1)
        for m_res in _CKV_FAILED_RES.finditer(block):
            failed_ids.add(ckv_id)
            failed_pairs.append((m_res.group(1), ckv_id))

    return {
        "failed_ckv_ids":      sorted(failed_ids),
        "failed_per_resource": failed_pairs,
        "passed_count":        passed,
        "failed_count":        failed,
        "total_checks":        passed + failed,
        "scan_seconds":        elapsed,
    }


def _rego_package(rego: str) -> str | None:
    m = _REGO_PACKAGE_RE.search(rego or "")
    return m.group(1) if m else None


def _rego_entry_rules(rego: str) -> list[str]:
    rules: list[str] = []
    seen: set[str] = set()

    def add(rule: str) -> None:
        if rule not in seen:
            seen.add(rule)
            rules.append(rule)

    for rule in _REGO_ENTRY_RULES:
        add(rule)
    for rule in _REGO_DEFAULT_FALSE_RE.findall(rego or ""):
        add(rule)
    for rule in _REGO_BOOL_RULE_RE.findall(rego or ""):
        if rule not in {"else", "not"}:
            add(rule)
    return rules


def _opa_eval_bool(
    opa_bin: str,
    cwd: Path,
    expr: str,
    timeout: int,
    *,
    v0_compatible: bool = False,
) -> tuple[bool | None, str]:
    cmd = [opa_bin, "eval", "--format", "json"]
    if v0_compatible:
        cmd.append("--v0-compatible")
    cmd += ["-i", "plan.json", "-d", "intent.rego", expr]
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout or "").strip()
    try:
        payload = json.loads(proc.stdout or "{}")
        value = payload["result"][0]["expressions"][0]["value"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return None, ""
    return (value if isinstance(value, bool) else None), ""


def run_rego_intent_on_hcl(
    hcl: str,
    rego: str,
    *,
    run_dir: str | Path | None = None,
    files_dir: str | Path | None = None,
    timeout: int = 120,
) -> dict:
    """Evaluate dataset Rego intent against Terraform plan JSON.

    Returns a compact result used by benchmark scripts:
      {"ok": bool, "skipped": bool, "rule": str|None, "error": str|None}
    """
    if not (rego or "").strip():
        return {"ok": None, "skipped": True, "rule": None, "error": "missing rego intent"}

    opa_bin = shutil.which("opa")
    if not opa_bin:
        return {"ok": None, "skipped": True, "rule": None, "error": "opa not found in PATH"}

    package = _rego_package(rego)
    if not package:
        return {"ok": False, "skipped": False, "rule": None, "error": "rego package not found"}

    try:
        with terraform_workdir(run_dir, "rego") as d:
            write_terraform_dir(d, hcl, files_dir=files_dir)
            (d / "intent.rego").write_text(rego, encoding="utf-8")

            init = run_terraform(["terraform", "init", "-no-color"], d, 60)
            if init.returncode != 0:
                err = ((init.stderr or "") + "\n" + (init.stdout or "")).strip()
                return {"ok": False, "skipped": False, "rule": None,
                        "error": f"terraform init failed: {err[:500]}"}

            plan = run_terraform(["terraform", "plan", "-out=tfplan", "-no-color"], d, timeout)
            if plan.returncode != 0:
                err = (plan.stderr or plan.stdout or "").strip()
                return {"ok": False, "skipped": False, "rule": None,
                        "error": f"terraform plan failed: {err[:500]}"}

            show = run_terraform(["terraform", "show", "-json", "tfplan"], d, 60)
            if show.returncode != 0:
                err = (show.stderr or show.stdout or "").strip()
                return {"ok": False, "skipped": False, "rule": None,
                        "error": f"terraform show failed: {err[:500]}"}
            (d / "plan.json").write_text(show.stdout, encoding="utf-8")

            values: dict[str, bool | None] = {}
            errors: dict[str, str] = {}
            true_rules: list[str] = []
            false_rules: list[str] = []
            for rule in _rego_entry_rules(rego):
                expr = f"data.{package}.{rule}"
                value, err = _opa_eval_bool(opa_bin, d, expr, 30)
                if err:
                    value_v0, err_v0 = _opa_eval_bool(
                        opa_bin, d, expr, 30, v0_compatible=True
                    )
                    if value_v0 is not None or not err_v0:
                        value, err = value_v0, err_v0
                values[rule] = value
                if err:
                    errors[rule] = err[:300]
                if value is True:
                    true_rules.append(rule)
                if value is False:
                    false_rules.append(rule)

            aggregate_true = [r for r in true_rules if r in _REGO_AGGREGATE_RULES]
            aggregate_false = [r for r in false_rules if r in _REGO_AGGREGATE_RULES]
            if aggregate_true:
                return {
                    "ok": True,
                    "skipped": False,
                    "rule": aggregate_true[0],
                    "error": None,
                    "values": values,
                    "true_rules": true_rules,
                    "false_rules": false_rules,
                    "entrypoint_type": "aggregate",
                }
            if aggregate_false:
                return {
                    "ok": False,
                    "skipped": False,
                    "rule": aggregate_false[0],
                    "error": "aggregate Rego intent rule evaluated to false; "
                             f"false aggregate rules: {', '.join(aggregate_false[:8])}",
                    "values": values,
                    "true_rules": true_rules,
                    "false_rules": false_rules,
                    "entrypoint_type": "aggregate",
                }
            if true_rules:
                return {
                    "ok": True,
                    "skipped": False,
                    "rule": true_rules[0],
                    "error": None,
                    "values": values,
                    "true_rules": true_rules,
                    "false_rules": false_rules,
                    "entrypoint_type": "derived",
                }
            if false_rules:
                return {
                    "ok": False,
                    "skipped": False,
                    "rule": false_rules[0],
                    "error": "no Rego intent rule evaluated to true; "
                             f"false rules: {', '.join(false_rules[:8])}",
                    "values": values,
                    "true_rules": true_rules,
                    "false_rules": false_rules,
                    "entrypoint_type": "derived",
                }

            return {
                "ok": False,
                "skipped": False,
                "rule": None,
                "error": "no supported boolean entry rule found "
                         f"(tried: {', '.join(_rego_entry_rules(rego))})",
                "values": values,
                "opa_errors": errors,
            }
    except subprocess.TimeoutExpired as e:
        return {"ok": False, "skipped": False, "rule": None,
                "error": f"timeout while running {e.cmd}"}
