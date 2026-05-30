from typing import TypedDict


class AgentState(TypedDict):
    # Input — không thay đổi trong suốt pipeline
    prompt: str

    # Cấu hình môi trường — đọc từ env vars khi khởi tạo graph
    terraform_plan_timeout: int
    auto_destroy: bool          # True trong eval/batch — destroy ngay sau apply thành công

    # Agent 1 — {"resources": [...], "data_sources": [...]}
    # attributes: primitive → flat attr, nested dict → HCL block, "REF:..." → reference
    infrastructure_plan: dict

    # Agent 2 — {"type.name": ["CKV_AWS_NNN", ...]} — A3 dùng để generate HCL thỏa checks,
    # A4 dùng để chạy checkov --check <ids>
    security_ckv_ids: dict

    # Agent 3
    generated_code: str

    # Agent 4
    fix_feedback: dict

    # Retry counters — tách biệt theo loop type
    arch_retry_count: int
    sec_retry_count: int
    eng_retry_count: int
    total_retry_count: int
    error_history: list        # global log — dùng cho oscillation detection
    arch_error_history: list   # chỉ lỗi MISSING_RESOURCE → A1 retry
    sec_error_history: list    # chỉ lỗi WRONG_CONSTRAINT → A2 retry
    eng_error_history: list    # chỉ lỗi SECURITY/SYNTAX/LOGIC → A3 retry

    # Agent 5
    deployment_result: dict
    deploy_retry_count: int      # tất cả A5 failures (TRANSIENT + FIXABLE + MISSING + UNKNOWN)
    deploy_eng_retry_count: int  # chỉ A5→A3 FIXABLE routes — tách khỏi eng_retry_count A4

    # Audit log
    routing_log: list

    # Per-run working directory — set by benchmark_pipeline, used by A4/A5 for structured dirs
    run_dir: str
