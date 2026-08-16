from app.config.settings import settings


CODEBASE_SETTINGS_DEFAULTS = {
    "repo_storage_path": settings.repo_storage_path,
    "code_graph_enabled": True,
    "code_graph_provider": "codegraph",
    "enable_incremental_scan": True,
    "incremental_scan_interval_sec": 300,
    "code_analysis_file_worker_count": 2,
    "code_analysis_line_chunk_enabled": True,
    "code_analysis_line_chunk_target_lines": 5,
    "code_analysis_line_chunk_overlap_lines": 1,
    "code_analysis_line_chunk_max_lines": 200,
    "code_analysis_symbol_summary_enabled": True,
    "code_analysis_symbol_summary_llm_concurrency": 2,
    "code_analysis_symbol_summary_llm_batch_size": 20,
    "code_analysis_symbol_body_max_lines_function": 500,
    "code_analysis_symbol_body_max_lines_class": 120,
    "code_analysis_embed_max_chars": 4000,
    "code_analysis_content_grep_enabled": True,
    "code_analysis_nl_to_code_enabled": False,
    "code_analysis_nl_rewrite_enabled": False,
    "code_analysis_nl_rewrite_mode": "auto",
    "resolve_channel_timeout_ms": 60000,
    "mr_experience_enabled": True,
    "mr_experience_min_quality_score": 0.3,
    "mr_experience_merge_by_scenario": True,
    "lsp_enabled": True,
}


def ensure_codebase_settings_defaults() -> None:
    for key, value in CODEBASE_SETTINGS_DEFAULTS.items():
        if not hasattr(settings, key):
            object.__setattr__(settings, key, value)
