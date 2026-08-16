from pathlib import Path
from pypdf import PdfReader


def extract_pdf_lines(path: Path) -> list[str]:
    reader = PdfReader(str(path))
    lines: list[str] = []
    for page_no, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        if not text.strip():
            continue
        lines.append(f"--- Page {page_no} ---")
        lines.extend(text.splitlines())
    return lines
