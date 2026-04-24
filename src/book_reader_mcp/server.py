"""
book-reader-mcp: MCP server for chapter-wise PDF and EPUB book reading.

Splits books into chapters, extracts text for LLM summarization,
and saves summaries as formatted PDFs. No LLM baked in — your MCP
client's model does the thinking.

Folder convention:
    books/              — Drop PDF/EPUB books here
    books_summarized/   — Structured output per book

Tools:
    list_books         — List available books in books/ folder
    load_book          — Load a book, detect chapters, prepare for processing
    list_chapters      — List detected chapters with metadata
    get_chapter_text   — Get full text of one chapter (1-based index)
    save_chapter_summary — Save an LLM-generated summary as a formatted PDF
    get_book_info      — Get book metadata (title, author, format)
    get_summary_status — Check which chapters have been summarized (auto-resume)
    search_book        — Full-text search across all chapters
    compile_book_summary — Generate a combined book summary PDF
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pymupdf
from mcp.server.fastmcp import FastMCP

from .chapter_detector import Chapter, detect_chapters
from .pdf_utils import (
    create_compiled_summary_pdf,
    create_summary_pdf,
    extract_chapter_images as extract_pdf_images,
    extract_chapter_text as extract_pdf_text,
    get_book_metadata as get_pdf_metadata,
    sanitize_filename,
    split_chapter_to_pdf,
)
from .epub_utils import (
    get_epub_metadata,
    extract_epub_chapters,
    extract_epub_text,
    extract_epub_images,
    search_epub,
)

logger = logging.getLogger("book-reader-mcp")

# ---------------------------------------------------------------------------
# Non-content chapter detection
# ---------------------------------------------------------------------------
_NON_CONTENT_PATTERNS = re.compile(
    r"^(copyright|table of contents|index|glossary|about the author|dedication|"
    r"acknowledg(?:e?ments?)|colophon|title page|half title|cover|also by|other books|"
    r"front matter|back matter|contents|list of figures|list of tables|"
    r"permissions|credits|praise for)\b",
    re.IGNORECASE,
)


def _is_content_chapter(chapter: Chapter) -> bool:
    """Determine if a chapter is actual content vs structural section."""
    title = chapter.title.strip()
    if _NON_CONTENT_PATTERNS.match(title):
        return False
        
    # For EPUB, we trust our smart merge/filter logic in epub_utils
    if _state.get("format") == "EPUB":
        return True
        
    if chapter.page_count < 2:
        return False
    return True


# ---------------------------------------------------------------------------
# Project directory convention
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path.cwd()
_BOOKS_DIR = _PROJECT_ROOT / "books"
_SUMMARIES_DIR = _PROJECT_ROOT / "books_summarized"

# ---------------------------------------------------------------------------
# Server state
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "book-reader",
    instructions=(
        "Book reader that splits books into chapters for summarization. "
        "Workflow: 1) list_books → 2) load_book → 3) list_chapters → "
        "4) get_chapter_text for each chapter → 5) summarize with your LLM → "
        "6) save_chapter_summary for each. Process ONE chapter at a time to "
        "avoid blowing context."
    ),
)

# In-memory state for the currently loaded book
_state: dict = {
    "doc": None,
    "chapters": [],
    "book_dir": None,
    "chapters_dir": None,
    "summaries_dir": None,
    "book_title": "",
    "book_path": None,
    "format": "PDF",
    "chapter_images": {},
}


def _ensure_book_loaded() -> None:
    """Raise if no book is loaded."""
    if _state["doc"] is None:
        raise ValueError("No book loaded. Call load_book first.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_book_path(path_str: str) -> Path:
    """Resolve a book path from index, name, or absolute path."""
    stripped = path_str.strip()
    _BOOKS_DIR.mkdir(exist_ok=True)
    
    # Get all supported files
    supported_files = sorted(list(_BOOKS_DIR.glob("*.pdf")) + list(_BOOKS_DIR.glob("*.epub")))

    # 1. Numeric index
    if stripped.isdigit():
        idx = int(stripped) - 1
        if idx < 0 or idx >= len(supported_files):
            raise ValueError(
                f"Book index {stripped} out of range. "
                f"Available: 1-{len(supported_files)}. Use list_books to see options."
            )
        return supported_files[idx]

    path = Path(stripped)

    # 2. If not absolute, look in books/
    if not path.is_absolute():
        # Try as exact filename first
        candidate = _BOOKS_DIR / stripped
        if candidate.exists():
            return candidate
            
        # Try adding extensions
        for ext in [".pdf", ".epub"]:
            if not stripped.lower().endswith(ext):
                candidate = _BOOKS_DIR / (stripped + ext)
                if candidate.exists():
                    return candidate
                    
        # Fuzzy: search for stem match
        for p in supported_files:
            if stripped.lower() in p.stem.lower():
                return p
        raise FileNotFoundError(
            f"No book matching '{stripped}' found in {_BOOKS_DIR}. "
            f"Use list_books to see available books."
        )

    # 3. Absolute path — use directly
    if not path.exists():
        raise FileNotFoundError(f"Book not found: {path_str}")
    return path


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_books() -> str:
    """
    List available PDF and EPUB books in the books/ folder.
    """
    _BOOKS_DIR.mkdir(exist_ok=True)
    supported_files = sorted(list(_BOOKS_DIR.glob("*.pdf")) + list(_BOOKS_DIR.glob("*.epub")))
    books = []
    for i, p in enumerate(supported_files, 1):
        size_mb = round(p.stat().st_size / (1024 * 1024), 2)
        books.append({
            "index": i,
            "name": p.stem,
            "file": p.name,
            "format": p.suffix.lstrip('.').upper(),
            "size_mb": size_mb,
        })

    return json.dumps({
        "books_dir": str(_BOOKS_DIR),
        "count": len(books),
        "books": books,
    }, indent=2)


@mcp.tool()
def load_book(pdf_path: str, output_dir: str = "") -> str:
    """
    Load a PDF or EPUB book, detect chapters, and prepare for summarization.
    """
    path = _resolve_book_path(pdf_path)
    file_format = path.suffix.lower().lstrip('.')
    if file_format not in ["pdf", "epub"]:
        raise ValueError(f"Unsupported file format: {file_format}")

    # Close previous book if any
    if _state["doc"] is not None:
        try:
            # Only call close() if it's a PyMuPDF doc, not our EPUB string handle
            if hasattr(_state["doc"], "close"):
                _state["doc"].close()
        except Exception:
            pass
        logger.info("Closed previously loaded book")

    if file_format == "pdf":
        doc = pymupdf.open(str(path))
        metadata = get_pdf_metadata(doc, path)
        chapters = detect_chapters(doc)
    else:
        # EPUB doesn't need to stay "open"
        doc = "EPUB_HANDLE" 
        metadata = get_epub_metadata(path)
        chapters = extract_epub_chapters(path)

    book_title = metadata["title"]
    logger.info("Loaded '%s' (%s format)", book_title, file_format.upper())

    # Determine output directory
    if output_dir:
        book_dir = Path(output_dir)
    else:
        _SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
        book_dir = _SUMMARIES_DIR / sanitize_filename(book_title)

    book_dir.mkdir(parents=True, exist_ok=True)

    # Create structured subdirectories
    chapters_dir = book_dir / "chapters"
    summaries_dir = book_dir / "summaries"
    images_dir = book_dir / "images"
    chapters_dir.mkdir(exist_ok=True)
    summaries_dir.mkdir(exist_ok=True)
    images_dir.mkdir(exist_ok=True)

    # Handle format-specific extraction
    chapter_pdfs = []
    chapter_images: dict[int, list[dict]] = {}
    
    if file_format == "pdf":
        for ch in chapters:
            safe_title = sanitize_filename(ch.title)
            filename = f"chapter_{ch.index + 1:02d}_{safe_title}.pdf"
            ch_path = chapters_dir / filename
            split_chapter_to_pdf(doc, ch, ch_path)
            chapter_pdfs.append(str(ch_path))
            ch_prefix = f"ch{ch.index + 1:02d}"
            images = extract_pdf_images(doc, ch, book_dir, ch_prefix)
            chapter_images[ch.index] = images
    else:
        extract_epub_images(path, 0, book_dir)
        for ch in chapters:
            chapter_images[ch.index] = []

    # Update state
    _state["doc"] = doc
    _state["chapters"] = chapters
    _state["book_dir"] = book_dir
    _state["chapters_dir"] = chapters_dir
    _state["summaries_dir"] = summaries_dir
    _state["book_title"] = book_title
    _state["book_path"] = path
    _state["format"] = file_format.upper()
    _state["chapter_images"] = chapter_images

    # Build chapter info
    chapters_info = []
    for ch in chapters:
        info = ch.to_dict()
        info["images_count"] = len(chapter_images.get(ch.index, []))
        info["is_content"] = _is_content_chapter(ch)
        chapters_info.append(info)

    result = {
        "status": "loaded",
        "format": file_format.upper(),
        "metadata": metadata,
        "output_directory": str(book_dir),
        "summaries_directory": str(summaries_dir),
        "chapters_detected": len(chapters),
        "chapters": chapters_info,
    }
    if chapter_pdfs:
        result["chapter_pdfs"] = chapter_pdfs

    return json.dumps(result, indent=2)


@mcp.tool()
def list_chapters() -> str:
    """List all detected chapters in the currently loaded book."""
    _ensure_book_loaded()
    
    chapters_info = []
    for ch in _state["chapters"]:
        info = ch.to_dict()
        info["images_count"] = len(_state["chapter_images"].get(ch.index, []))
        info["is_content"] = _is_content_chapter(ch)
        chapters_info.append(info)

    return json.dumps({
        "book_title": _state["book_title"],
        "format": _state["format"],
        "chapters": chapters_info,
    }, indent=2)


@mcp.tool()
def get_chapter_text(chapter_index: int) -> str:
    """Get the full text content of a specific chapter."""
    _ensure_book_loaded()
    
    idx = chapter_index - 1
    chapters: list[Chapter] = _state["chapters"]
    if idx < 0 or idx >= len(chapters):
        raise ValueError(f"Invalid chapter index {chapter_index}. Valid: 1-{len(chapters)}")

    chapter = chapters[idx]
    images_info = _state.get("chapter_images", {}).get(idx, [])
    
    if _state["format"] == "PDF":
        text = extract_pdf_text(_state["doc"], chapter, images_info)
        header = (
            f"=== {_state['book_title']} ===\n"
            f"=== Chapter {chapter_index}: {chapter.title} ===\n"
            f"=== Pages {chapter.start_page + 1}-{chapter.end_page + 1} ({chapter.page_count} pages) ===\n"
        )
    else:
        text = extract_epub_text(_state["book_path"], idx, chapters)
        header = (
            f"=== {_state['book_title']} ===\n"
            f"=== Chapter {chapter_index}: {chapter.title} ===\n"
            f"=== ~{chapter.page_count} estimated pages ===\n"
        )

    if images_info:
        header += f"=== {len(images_info)} images extracted ===\n"

    return header + "\n" + text


@mcp.tool()
def save_chapter_summary(chapter_index: int, summary_text: str) -> str:
    """Save a chapter summary as a formatted PDF file."""
    _ensure_book_loaded()
    idx = chapter_index - 1
    chapters: list[Chapter] = _state["chapters"]
    if idx < 0 or idx >= len(chapters):
        raise ValueError(f"Invalid chapter index {chapter_index}")

    chapter = chapters[idx]
    summaries_dir: Path = _state["summaries_dir"]
    safe_title = sanitize_filename(chapter.title)
    filename = f"chapter_{chapter_index:02d}_{safe_title}_summary.pdf"
    output_path = summaries_dir / filename

    create_summary_pdf(
        summary_text=summary_text,
        chapter_title=chapter.title,
        book_title=_state["book_title"],
        output_path=output_path,
    )
    return json.dumps({
        "status": "saved",
        "chapter_index": chapter_index,
        "summary_pdf": str(output_path),
    }, indent=2)


@mcp.tool()
def get_book_info() -> str:
    """Get metadata about the currently loaded book."""
    _ensure_book_loaded()
    if _state["format"] == "PDF":
        metadata = get_pdf_metadata(_state["doc"], _state["book_path"])
    else:
        metadata = get_epub_metadata(_state["book_path"])
    metadata["output_directory"] = str(_state["book_dir"])
    metadata["chapters_detected"] = len(_state["chapters"])
    return json.dumps(metadata, indent=2)


@mcp.tool()
def get_summary_status() -> str:
    """Check which chapters have already been summarized."""
    _ensure_book_loaded()
    summaries_dir: Path = _state["summaries_dir"]
    chapters: list[Chapter] = _state["chapters"]
    statuses = []
    completed = 0
    for ch in chapters:
        ch_index = ch.index + 1
        safe_title = sanitize_filename(ch.title)
        summary_name = f"chapter_{ch_index:02d}_{safe_title}_summary.pdf"
        expected = summaries_dir / summary_name
        is_done = expected.exists()
        if is_done: completed += 1
        statuses.append({
            "chapter_index": ch_index,
            "title": ch.title,
            "is_content": _is_content_chapter(ch),
            "status": "completed" if is_done else "pending",
        })
    content_chapters = [s for s in statuses if s["is_content"]]
    content_completed = sum(1 for s in content_chapters if s["status"] == "completed")
    return json.dumps({
        "total_chapters": len(chapters),
        "content_chapters": len(content_chapters),
        "completed": content_completed,
        "progress_pct": round(100 * content_completed / max(len(content_chapters), 1), 1),
        "chapters": statuses,
    }, indent=2)


@mcp.tool()
def search_book(query: str, max_results: int = 20) -> str:
    """
    Search across all chapters in the loaded book for a text query.

    Returns matching passages with chapter index, page number, and
    surrounding context. Case-insensitive.

    Args:
        query: The text to search for.
        max_results: Maximum number of matches to return (default 20).

    Returns:
        JSON with query, match count, and array of matches with context.
    """
    _ensure_book_loaded()

    chapters: list[Chapter] = _state["chapters"]
    query_lower = query.lower()
    matches = []

    if _state["format"] == "EPUB":
        matches = search_epub(
            _state["book_path"], query, chapters, max_results
        )
    else:
        doc: pymupdf.Document = _state["doc"]
        for ch in chapters:
            for page_num in range(ch.start_page, ch.end_page + 1):
                page = doc[page_num]
                text = page.get_text("text")
                if query_lower in text.lower():
                    idx = text.lower().find(query_lower)
                    start = max(0, idx - 100)
                    end = min(len(text), idx + len(query) + 100)
                    snippet = text[start:end].strip()
                    if start > 0:
                        snippet = "..." + snippet
                    if end < len(text):
                        snippet = snippet + "..."
                    matches.append({
                        "chapter_index": ch.index + 1,
                        "chapter_title": ch.title,
                        "page": page_num + 1,
                        "snippet": snippet,
                    })
                    if len(matches) >= max_results:
                        break
            if len(matches) >= max_results:
                break

    logger.info("Search '%s': %d matches found", query, len(matches))

    return json.dumps({
        "query": query,
        "total_matches": len(matches),
        "matches": matches,
    }, indent=2)


@mcp.tool()
def compile_book_summary(summary_text: str) -> str:
    """Generate a compiled full-book summary PDF."""
    _ensure_book_loaded()
    output_path = _state["book_dir"] / "00_complete_book_summary.pdf"
    create_compiled_summary_pdf(summary_text, _state["book_title"], output_path)
    return json.dumps({"status": "saved", "summary_pdf": str(output_path)}, indent=2)


@mcp.prompt()
def summarize_book(pdf_path: str) -> str:
    """Instructions for summarizing a book."""
    return f"You are summarizing a book. Start by calling load_book('{pdf_path}')."


def main():
    mcp.run(transport="stdio")

if __name__ == "__main__":
    main()
