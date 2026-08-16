def line_chunk_space_name(repo_id: str, dim: int) -> str:
    return f"repo_{repo_id}_line_chunk_{dim}"


def symbol_summary_space_name(repo_id: str, dim: int) -> str:
    return f"repo_{repo_id}_symbol_summary_{dim}"
