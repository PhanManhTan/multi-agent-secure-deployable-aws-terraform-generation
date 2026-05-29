"""HCL parse utilities dùng chung cho Engineering và Validation agent."""
import re

_RESOURCE_DECL_RE = re.compile(r'resource\s+"([^"]+)"\s+"([^"]+)"')


def find_resource_pairs(hcl: str) -> set[tuple[str, str]]:
    """Trích set (type, name) từ tất cả resource declarations trong HCL."""
    return set(_RESOURCE_DECL_RE.findall(hcl))


def extract_resource_block(hcl: str, res_type: str, res_name: str) -> str | None:
    """Trích nội dung block `resource "type" "name" { ... }` bằng bracket matching.

    Trả None nếu không tìm thấy resource label trong HCL.
    """
    pattern = re.compile(
        rf'resource\s+"{re.escape(res_type)}"\s+"{re.escape(res_name)}"\s*\{{',
        re.MULTILINE,
    )
    m = pattern.search(hcl)
    if not m:
        return None
    open_idx = m.end() - 1
    depth, end_idx = 0, None
    for i in range(open_idx, len(hcl)):
        if hcl[i] == "{":
            depth += 1
        elif hcl[i] == "}":
            depth -= 1
            if depth == 0:
                end_idx = i + 1
                break
    return hcl[open_idx:end_idx] if end_idx else hcl[open_idx:]


def check_injected_attrs(code: str, constraints: dict,
                         block_constraints: dict | None = None) -> list[tuple[str, str, any]]:
    """Kiểm tra attrs/blocks trong constraints có xuất hiện trong đúng resource block không.

    Tìm trong block cụ thể (bracket matching) thay vì toàn file — tránh false positive
    khi attr trùng tên xuất hiện ở resource khác.
    Trả list (resource_label, attr, expected_value) cho attrs bị thiếu.
    """
    missing = []

    # Flat attr check: tìm `attr =` trong resource block
    for resource_label, attrs in constraints.items():
        parts = resource_label.split(".", 1)
        if len(parts) != 2:
            continue
        res_type, res_name = parts
        block = extract_resource_block(code, res_type, res_name)
        if block is None:
            for attr, val in attrs.items():
                missing.append((resource_label, attr, val))
            continue
        for attr, val in attrs.items():
            if not re.search(rf'^\s*{re.escape(attr)}\s*=', block, re.MULTILINE):
                missing.append((resource_label, attr, val))

    # Nested block check: tìm `block_name {` trong resource block
    for resource_label, blocks in (block_constraints or {}).items():
        parts = resource_label.split(".", 1)
        if len(parts) != 2:
            continue
        res_type, res_name = parts
        block = extract_resource_block(code, res_type, res_name)
        if block is None:
            for block_name, block_val in blocks.items():
                missing.append((resource_label, block_name, block_val))
            continue
        for block_name, block_val in blocks.items():
            if not re.search(rf'^\s*{re.escape(block_name)}\s*\{{', block, re.MULTILINE):
                missing.append((resource_label, block_name, block_val))

    return missing
