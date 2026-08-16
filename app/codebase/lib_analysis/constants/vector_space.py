def api_summary_space_name(repo_id: str, dim: int) -> str:
    return f"lib_{repo_id}_api_summary_{dim}"


class LibAnalysisType:
    API_SUMMARY_VECTOR = "api_summary_vector"
