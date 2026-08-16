import logging
import mimetypes
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from ...schemes import RuntimeContext
from ..utils import await_with_abort


IMAGE_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".ico",
})
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_BYTES_LABEL = f"{MAX_IMAGE_BYTES // (1024 * 1024)} MB"

def is_image_file(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix == ".svg":
        return False
    if suffix in IMAGE_SUFFIXES:
        return True
    mime, _ = mimetypes.guess_type(str(path))
    return bool(mime and mime.startswith("image/") and mime != "image/svg+xml")

def image_file_metadata(path: Path) -> Dict[str, Any]:
    from PIL import Image

    size = path.stat().st_size
    mime, _ = mimetypes.guess_type(str(path))
    with Image.open(path) as img:
        return {
            "mime": mime or "image/unknown",
            "format": (img.format or path.suffix.lstrip(".")).upper(),
            "width": img.width,
            "height": img.height,
            "size_bytes": size,
            "mode": img.mode,
        }

async def describe_image_file(
    path: Path,
    prompt: Optional[str] = None,
    run_ctx: Optional[RuntimeContext] = None,
) -> Optional[Tuple[str, str]]:
    from app.infrastructure.llms import cv_factory

    provider, model = cv_factory.get_default_model()
    if not provider or not model:
        raise RuntimeError(
            "No default CV model configured. Set default provider/model in data/models/cv_models.json."
        )
    if not cv_factory.if_model_support(provider, model):
        raise RuntimeError(
            f"CV model not available: {provider}/{model}. Enable it in data/models/cv_models.json."
        )

    cv = cv_factory.create_model(provider, model)
    if prompt and prompt.strip():
        result = await await_with_abort(run_ctx, cv.describe_with_prompt(str(path), prompt.strip()))
    else:
        result = await await_with_abort(run_ctx, cv.describe(str(path)))
    if result is None:
        return None

    text, _ = result
    if not text:
        raise RuntimeError("Image description failed")

    return text, f"{provider}/{model}"

def format_image_read_output(
    file_path: Path,
    metadata: Dict[str, Any],
    description: str,
    model_label: str,
) -> str:
    meta_lines = [
        f"format: {metadata.get('format', 'unknown')}",
        f"dimensions: {metadata.get('width')}x{metadata.get('height')}",
        f"mime: {metadata.get('mime', 'unknown')}",
        f"size_bytes: {metadata.get('size_bytes', 0)}",
        f"vision_model: {model_label}",
    ]
    return "\n".join([
        f"<path>{file_path}</path>",
        "<type>image</type>",
        "<metadata>",
        "\n".join(meta_lines),
        "</metadata>",
        "<content>",
        description,
        "</content>",
        "<truncated>false</truncated>",
        "<next_offset></next_offset>",
    ])
