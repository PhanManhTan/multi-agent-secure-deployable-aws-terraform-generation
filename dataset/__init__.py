from .loader import load_and_process, load_from_cache, load_dev, load_test, get_by_difficulty, load_original_format
from .filter import parse_resources, is_floci_compatible, FLOCI_SUPPORTED
from .enricher import enrich_sample, map_difficulty

try:
    from .evaluator import (
        compute_pass_at_k,
        compute_pass_itr_at_n,
        validate_with_rego,
        run_benchmark,
        check_floci_health,
    )
except ModuleNotFoundError as exc:
    if exc.name not in {"dataset.evaluator", f"{__name__}.evaluator"}:
        raise
    compute_pass_at_k = None
    compute_pass_itr_at_n = None
    validate_with_rego = None
    run_benchmark = None
    check_floci_health = None

__all__ = [
    "load_and_process", "load_from_cache", "load_dev", "load_test", "get_by_difficulty",
    "load_original_format",
    "parse_resources", "is_floci_compatible", "FLOCI_SUPPORTED",
    "enrich_sample", "map_difficulty",
    "compute_pass_at_k", "compute_pass_itr_at_n", "validate_with_rego", "run_benchmark",
    "check_floci_health",
]
