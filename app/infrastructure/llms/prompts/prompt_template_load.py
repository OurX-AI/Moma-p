import os
import logging
import dataclasses
from typing import Any, Optional
from datetime import datetime
from jinja2 import Environment, FileSystemLoader, select_autoescape


def get_prompt_template(path: str, file_name: str, params: Optional[dict[str, Any]] = None) -> str:
    """
    Load and return a prompt template using Jinja2.

    Args:
        path: Relative path to the template directory within the project
        name: Name of the prompt template file (without .md extension)
        params: Dictionary of parameters to substitute in the template (optional)

    Returns:
        The template string with proper variable substitution syntax
    """
    try:        
        # 创建Jinja2环境
        custom_env = Environment(
            loader=FileSystemLoader(path),
            autoescape=select_autoescape(),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        
        # 加载模板
        template = custom_env.get_template(f"{file_name}")
        
        # 如果有参数则应用参数，否则直接渲染
        if params:
            return template.render(**params)
        else:
            return template.render()
    except Exception as e:
        logging.error(f"Error loading template {file_name} from {path}: {e}")
        return ""
