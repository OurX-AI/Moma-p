def mr_pattern_space_name(repo_id: str, dim: int) -> str:
    return f"repo_{repo_id}_mr_pattern_{dim}"


class ExperienceAnalysisType:
    MR_PATTERN_VECTOR = "mr_pattern_vector"
