"""
PDF utilities: splitting chapters, extracting text/images, generating summary PDFs.

Uses PyMuPDF for reading and fpdf2 for writing summary PDFs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pymupdf
from fpdf import FPDF

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
    return clean[:80].lower()


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
# PDF summary generation
# ---------------------------------------------------------------------------


def _sanitize_for_helvetica(text: str) -> str:
    """Replace unicode characters that Helvetica can't render."""
    replacements = {
        "\u2014": "--",   # em dash
        "\u2013": "-",    # en dash
        "\u2010": "-",    # hyphen
        "\u2018": "'",    # left single quote
        "\u2019": "'",    # right single quote
        "\u201c": '"',    # left double quote
        "\u201d": '"',    # right double quote
        "\u2022": "-",    # bullet
        "\u2026": "...",  # ellipsis
        "\u00a0": " ",    # non-breaking space
        "\u2011": "-",    # non-breaking hyphen
        "\u200b": "",     # zero-width space
        "\u2212": "-",    # minus sign
        "\u00b7": "-",    # middle dot
    }
    for char, replacement in replacements.items():
        text = text.replace(char, replacement)
    # Fallback: replace any remaining non-latin1 chars
    return text.encode("latin-1", errors="replace").decode("latin-1")


def _is_numbered_list(text: str) -> bool:
    """Check if text starts with a numbered list pattern like '1.', '10.'."""
    return bool(re.match(r"^\d+\.\s", text))


def _render_markdown_body(pdf: FPDF, text: str, left: float, width: float) -> None:
    """Render markdown-formatted text into an FPDF document.

    Supports: headings (#, ##, ###), bullet points (-, *), numbered lists.
    """
    for line in text.split("\n"):
        stripped = line.strip()

        if not stripped:
            pdf.ln(5)
            continue

        stripped = _sanitize_for_helvetica(stripped)
        pdf.set_x(left)

        # Heading detection (most specific first)
        if stripped.startswith("### "):
            pdf.set_font("Helvetica", "B", 12)
            pdf.multi_cell(width, 7, text=stripped[4:])
            pdf.set_font("Helvetica", "", 11)
            pdf.ln(2)
        elif stripped.startswith("## "):
            pdf.set_font("Helvetica", "B", 14)
            pdf.multi_cell(width, 7, text=stripped[3:])
            pdf.set_font("Helvetica", "", 11)
            pdf.ln(2)
        elif stripped.startswith("# "):
            pdf.set_font("Helvetica", "B", 16)
            pdf.multi_cell(width, 8, text=stripped[2:])
            pdf.set_font("Helvetica", "", 11)
            pdf.ln(3)
        elif stripped.startswith(("- ", "* ")):
            bullet_text = stripped[2:]
            indent = 8
            pdf.set_x(left + indent)
            pdf.multi_cell(width - indent, 6, text=f"- {bullet_text}")
        elif _is_numbered_list(stripped):
            indent = 8
            pdf.set_x(left + indent)
            pdf.multi_cell(width - indent, 6, text=stripped)
        else:
            pdf.multi_cell(width, 6, text=stripped)


def create_summary_pdf(
    summary_text: str,
    chapter_title: str,
    book_title: str,
    output_path: Path,
) -> Path:
    """Generate a PDF from a chapter summary text."""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    left = pdf.l_margin
    effective_width = pdf.w - pdf.l_margin - pdf.r_margin

    # Title
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_x(left)
    pdf.multi_cell(
        effective_width, 10,
        text=_sanitize_for_helvetica(book_title), align="C",
    )
    pdf.ln(5)

    # Chapter title
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_x(left)
    pdf.multi_cell(
        effective_width, 8,
        text=_sanitize_for_helvetica(f"Summary: {chapter_title}"), align="C",
    )
    pdf.ln(5)

    # Separator line
    pdf.set_draw_color(100, 100, 100)
    pdf.line(20, pdf.get_y(), pdf.w - 20, pdf.get_y())
    pdf.ln(10)

    # Summary body
    pdf.set_font("Helvetica", "", 11)
    _render_markdown_body(pdf, summary_text, left, effective_width)

    pdf.output(str(output_path))
    return output_path


def create_compiled_summary_pdf(
    summary_text: str,
    book_title: str,
    output_path: Path,
) -> Path:
    """Generate a compiled full-book summary PDF."""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    left = pdf.l_margin
    effective_width = pdf.w - pdf.l_margin - pdf.r_margin

    # Title block
    pdf.set_font("Helvetica", "B", 20)
    pdf.set_x(left)
    pdf.multi_cell(
        effective_width, 10,
        text=_sanitize_for_helvetica(book_title), align="C",
    )
    pdf.ln(3)

    pdf.set_font("Helvetica", "B", 14)
    pdf.set_x(left)
    pdf.multi_cell(effective_width, 8, text="Complete Book Summary", align="C")
    pdf.ln(5)

    # Double separator
    y = pdf.get_y()
    pdf.set_draw_color(60, 60, 60)
    pdf.set_line_width(0.8)
    pdf.line(20, y, pdf.w - 20, y)
    pdf.ln(2)
    y2 = pdf.get_y()
    pdf.set_line_width(0.3)
    pdf.line(20, y2, pdf.w - 20, y2)
    pdf.ln(10)

    # Body
    pdf.set_font("Helvetica", "", 11)
    _render_markdown_body(pdf, summary_text, left, effective_width)

    pdf.output(str(output_path))
    return output_path


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
