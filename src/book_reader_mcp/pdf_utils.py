"""
PDF utilities: splitting chapters, extracting text/images, generating summary PDFs.

Uses PyMuPDF for reading and markdown + xhtml2pdf for writing summary PDFs.
"""

from __future__ import annotations

import re
from pathlib import Path

import markdown
import pymupdf
from xhtml2pdf import pisa

from .chapter_detector import Chapter

__all__ = [
    "create_compiled_summary_pdf",
    "create_summary_pdf",
    "extract_chapter_images",
    "extract_chapter_text",
    "get_book_metadata",
    "sanitize_filename",
    "split_chapter_to_pdf",
]


def sanitize_filename(name: str) -> str:
    """Convert a chapter title to a safe filename."""
    # Remove non-alphanumeric characters except spaces and hyphens
    clean = re.sub(r"[^\w\s-]", "", name)
    # Replace whitespace with underscores
    clean = re.sub(r"\s+", "_", clean.strip())
    # Truncate to reasonable length
    clean = clean[:80].lower()
    return clean if clean else "untitled"


def split_chapter_to_pdf(
    doc: pymupdf.Document,
    chapter: Chapter,
    output_path: Path,
) -> Path:
    """Extract a chapter's pages into a separate PDF file."""
    new_doc = pymupdf.open()
    new_doc.insert_pdf(
        doc,
        from_page=chapter.start_page,
        to_page=chapter.end_page,
    )
    new_doc.save(str(output_path))
    new_doc.close()
    return output_path


def extract_chapter_images(
    doc: pymupdf.Document,
    chapter: Chapter,
    output_dir: Path,
    chapter_prefix: str,
) -> list[dict]:
    """Extract images from a chapter's pages and save as PNGs.

    Returns list of dicts with image metadata: page, path, size, index.
    Only extracts images larger than 5KB (skips tiny icons/decorations).
    """
    images_info = []
    img_counter = 0
    images_dir = output_dir / "images"
    images_dir.mkdir(exist_ok=True)

    for page_num in range(chapter.start_page, chapter.end_page + 1):
        page = doc[page_num]
        image_list = page.get_images(full=True)

        for img_info in image_list:
            xref = img_info[0]
            try:
                base_image = doc.extract_image(xref)
            except Exception:
                continue

            image_bytes = base_image["image"]
            # Skip tiny images (icons, bullets, decorations)
            if len(image_bytes) < 5000:
                continue

            img_counter += 1
            ext = base_image.get("ext", "png")
            img_filename = f"{chapter_prefix}_img_{img_counter:02d}.{ext}"
            img_path = images_dir / img_filename

            with open(img_path, "wb") as f:
                f.write(image_bytes)

            images_info.append({
                "index": img_counter,
                "page": page_num + 1,
                "path": str(img_path),
                "size_kb": round(len(image_bytes) / 1024, 1),
                "width": base_image.get("width", 0),
                "height": base_image.get("height", 0),
            })

    return images_info


def extract_chapter_text(
    doc: pymupdf.Document,
    chapter: Chapter,
    images_info: list[dict] | None = None,
) -> str:
    """Extract all text from a chapter's page range.

    If images_info is provided, injects [IMAGE] markers into the text
    at the pages where images appear so the LLM knows they exist.
    """
    # Group images by page for insertion
    images_by_page: dict[int, list[dict]] = {}
    if images_info:
        for img in images_info:
            images_by_page.setdefault(img["page"], []).append(img)

    texts = []
    for page_num in range(chapter.start_page, chapter.end_page + 1):
        page = doc[page_num]
        text = page.get_text("text")
        if text.strip():
            page_text = f"--- Page {page_num + 1} ---\n{text}"

            # Append image markers for this page
            page_images = images_by_page.get(page_num + 1, [])
            for img in page_images:
                page_text += (
                    f"\n[IMAGE on page {img['page']}: "
                    f"{img['width']}x{img['height']}px, "
                    f"saved at {img['path']}]"
                )

            texts.append(page_text)
    return "\n\n".join(texts)


# ---------------------------------------------------------------------------
# PDF summary generation via markdown -> HTML -> xhtml2pdf
# ---------------------------------------------------------------------------

# CSS stylesheet for rendered PDFs
_PDF_CSS = """
@page {
    size: A4;
    margin: 2cm 2.5cm;
    @frame footer {
        -pdf-frame-content: footerContent;
        bottom: 0.5cm;
        margin-left: 2.5cm;
        margin-right: 2.5cm;
        height: 1cm;
    }
}

body {
    font-family: Helvetica, Arial, sans-serif;
    font-size: 11px;
    line-height: 1.5;
    color: #1a1a1a;
}

h1 {
    font-size: 20px;
    color: #111;
    margin-top: 18px;
    margin-bottom: 8px;
    border-bottom: 2px solid #333;
    padding-bottom: 4px;
}

h2 {
    font-size: 16px;
    color: #222;
    margin-top: 14px;
    margin-bottom: 6px;
    border-bottom: 1px solid #aaa;
    padding-bottom: 3px;
}

h3 {
    font-size: 13px;
    color: #333;
    margin-top: 10px;
    margin-bottom: 4px;
}

p {
    margin-top: 4px;
    margin-bottom: 4px;
}

ul, ol {
    margin-top: 4px;
    margin-bottom: 4px;
    padding-left: 20px;
}

li {
    margin-bottom: 2px;
}

table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 8px;
    margin-bottom: 8px;
    font-size: 10px;
}

th {
    background-color: #2c3e50;
    color: #ffffff;
    padding: 6px 8px;
    text-align: left;
    border: 1px solid #2c3e50;
    font-weight: bold;
}

td {
    padding: 5px 8px;
    border: 1px solid #ddd;
    vertical-align: top;
}

tr:nth-child(even) td {
    background-color: #f7f7f7;
}

code {
    font-family: Courier, monospace;
    font-size: 10px;
    background-color: #f0f0f0;
    padding: 1px 3px;
}

pre {
    background-color: #f4f4f4;
    border: 1px solid #ddd;
    padding: 10px;
    font-family: Courier, monospace;
    font-size: 10px;
    line-height: 1.4;
    margin-top: 6px;
    margin-bottom: 6px;
    white-space: pre-wrap;
    word-wrap: break-word;
}

hr {
    border: none;
    border-top: 1px solid #ccc;
    margin: 10px 0;
}

strong {
    font-weight: bold;
}

em {
    font-style: italic;
}

blockquote {
    border-left: 3px solid #ccc;
    padding-left: 10px;
    margin-left: 0;
    color: #555;
    font-style: italic;
}

.title-block {
    text-align: center;
    margin-bottom: 20px;
}

.title-block h1 {
    font-size: 24px;
    border: none;
    margin-bottom: 4px;
}

.title-block h2 {
    font-size: 16px;
    color: #555;
    border: none;
    font-weight: normal;
}

.separator {
    border-top: 2px solid #333;
    margin: 15px 0;
}
"""

# Markdown extensions to enable
_MD_EXTENSIONS = [
    "tables",
    "fenced_code",
    "codehilite",
    "toc",
    "nl2br",
    "sane_lists",
]

_MD_EXTENSION_CONFIGS = {
    "codehilite": {
        "css_class": "code",
        "noclasses": True,
    },
}


def _markdown_to_html(text: str) -> str:
    """Convert markdown text to HTML using the markdown library."""
    return markdown.markdown(
        text,
        extensions=_MD_EXTENSIONS,
        extension_configs=_MD_EXTENSION_CONFIGS,
    )


def _html_to_pdf(html: str, output_path: Path) -> Path:
    """Convert an HTML string to a PDF file using xhtml2pdf."""
    with open(output_path, "wb") as f:
        pisa_status = pisa.CreatePDF(html, dest=f)
    if pisa_status.err:
        raise RuntimeError(
            f"xhtml2pdf failed with {pisa_status.err} error(s) "
            f"generating {output_path}"
        )
    return output_path


def create_summary_pdf(
    summary_text: str,
    chapter_title: str,
    book_title: str,
    output_path: Path,
) -> Path:
    """Generate a PDF from a chapter summary text."""
    body_html = _markdown_to_html(summary_text)

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>{_PDF_CSS}</style>
</head>
<body>
    <div class="title-block">
        <h1>{_escape_html(book_title)}</h1>
        <h2>Summary: {_escape_html(chapter_title)}</h2>
    </div>
    <div class="separator"></div>
    {body_html}
</body>
</html>"""

    return _html_to_pdf(html, output_path)


def create_compiled_summary_pdf(
    summary_text: str,
    book_title: str,
    output_path: Path,
) -> Path:
    """Generate a compiled full-book summary PDF."""
    body_html = _markdown_to_html(summary_text)

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>{_PDF_CSS}</style>
</head>
<body>
    <div class="title-block">
        <h1>{_escape_html(book_title)}</h1>
        <h2>Complete Book Summary</h2>
    </div>
    <div class="separator"></div>
    {body_html}
</body>
</html>"""

    return _html_to_pdf(html, output_path)


def _escape_html(text: str) -> str:
    """Escape HTML special characters in text."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def get_book_metadata(doc: pymupdf.Document, pdf_path: Path) -> dict:
    """Extract book metadata from PDF."""
    metadata = doc.metadata or {}
    return {
        "title": metadata.get("title", "") or pdf_path.stem,
        "author": metadata.get("author", "Unknown"),
        "subject": metadata.get("subject", ""),
        "total_pages": len(doc),
        "file_name": pdf_path.name,
        "file_size_mb": round(pdf_path.stat().st_size / (1024 * 1024), 2),
    }
