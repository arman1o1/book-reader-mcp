"""
book-reader-mcp: MCP server for chapter-wise PDF book reading.

Splits PDF books into chapters, extracts text for LLM summarization,
and saves summaries as formatted PDFs. No LLM baked in — your MCP
client's model does the thinking.

Folder convention:
    books/              — Drop PDF books here
    books_summarized/   — Structured output per book

Tools:
    list_books         — List available PDFs in books/ folder
    load_book          — Load a PDF, detect chapters, split into chapter PDFs
    list_chapters      — List detected chapters with page ranges and metadata
    get_chapter_text   — Get full text of one chapter (1-based index)
    save_chapter_summary — Save an LLM-generated summary as a formatted PDF
    get_book_info      — Get book metadata (title, author, pages)
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
    extract_chapter_images,
    extract_chapter_text,
    get_book_metadata,
    sanitize_filename,
    split_chapter_to_pdf,
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
        "PDF book reader that splits books into chapters for summarization. "
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
    "pdf_path": None,
    "chapter_images": {},
}


def _ensure_book_loaded() -> None:
    """Raise if no book is loaded."""
    if _state["doc"] is None:
        raise ValueError("No book loaded. Call load_book first.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_pdf_path(pdf_path: str) -> Path:
    """Resolve a PDF path from index, name, or absolute path.

    Resolution order:
    1. Digit → 1-based index from books/ folder (sorted)
    2. Non-absolute string → filename or stem lookup in books/
    3. Absolute path → use directly (backward compat)
    """
    stripped = pdf_path.strip()

    # 1. Numeric index
    if stripped.isdigit():
        _BOOKS_DIR.mkdir(exist_ok=True)
        pdfs = sorted(_BOOKS_DIR.glob("*.pdf"))
        idx = int(stripped) - 1
        if idx < 0 or idx >= len(pdfs):
            raise ValueError(
                f"Book index {stripped} out of range. "
                f"Available: 1-{len(pdfs)}. Use list_books to see options."
            )
        return pdfs[idx]

    path = Path(stripped)

    # 2. If not absolute, look in books/
    if not path.is_absolute():
        # Try as exact filename first
        candidate = _BOOKS_DIR / stripped
        if candidate.exists():
            return candidate
        # Try adding .pdf extension
        if not stripped.lower().endswith(".pdf"):
            candidate = _BOOKS_DIR / (stripped + ".pdf")
            if candidate.exists():
                return candidate
        # Fuzzy: search for stem match
        for p in _BOOKS_DIR.glob("*.pdf"):
            if stripped.lower() in p.stem.lower():
                return p
        raise FileNotFoundError(
            f"No PDF matching '{stripped}' found in {_BOOKS_DIR}. "
            f"Use list_books to see available books."
        )

    # 3. Absolute path — use directly
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    return path


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_books() -> str:
    """
    List available PDF books in the books/ folder.

    Returns an indexed list of books. Use the index or filename
    with load_book to load a specific book.

    Returns:
        JSON with books directory path, count, and book details.
    """
    _BOOKS_DIR.mkdir(exist_ok=True)
    pdfs = sorted(_BOOKS_DIR.glob("*.pdf"))
    books = []
    for i, p in enumerate(pdfs, 1):
        size_mb = round(p.stat().st_size / (1024 * 1024), 2)
        books.append({
            "index": i,
            "name": p.stem,
            "file": p.name,
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
    Load a PDF book, detect chapters, and split into per-chapter PDFs.

    This is the first tool to call. It opens the PDF, auto-detects chapter
    boundaries (via TOC bookmarks or heuristic heading detection), splits
    into individual chapter PDFs, and extracts images.

    Args:
        pdf_path: Path to the PDF. Accepts:
                  - A number (e.g. "2") → index from list_books
                  - A filename or partial name → looked up in books/ folder
                  - An absolute path → used directly (backward compatible)
        output_dir: Directory to save output. Defaults to
                     books_summarized/<book_name>/.

    Returns:
        JSON with book metadata, detected chapters, and output paths.
    """
    path = _resolve_pdf_path(pdf_path)
    if not path.suffix.lower() == ".pdf":
        raise ValueError(f"Not a PDF file: {path}")

    # Close previous book if any
    if _state["doc"] is not None:
        _state["doc"].close()
        logger.info("Closed previously loaded book")

    # Open the document
    doc = pymupdf.open(str(path))
    metadata = get_book_metadata(doc, path)
    book_title = metadata["title"]
    logger.info("Loaded '%s' (%d pages)", book_title, metadata["total_pages"])

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

    # Detect chapters
    chapters = detect_chapters(doc)
    logger.info("Detected %d chapters", len(chapters))

    # Split into chapter PDFs and extract images
    chapter_files = []
    chapter_images: dict[int, list[dict]] = {}
    for ch in chapters:
        safe_title = sanitize_filename(ch.title)
        filename = f"chapter_{ch.index + 1:02d}_{safe_title}.pdf"
        ch_path = chapters_dir / filename
        split_chapter_to_pdf(doc, ch, ch_path)
        chapter_files.append(str(ch_path))

        # Extract images for this chapter
        ch_prefix = f"ch{ch.index + 1:02d}"
        images = extract_chapter_images(doc, ch, book_dir, ch_prefix)
        chapter_images[ch.index] = images

    # Update state
    _state["doc"] = doc
    _state["chapters"] = chapters
    _state["book_dir"] = book_dir
    _state["chapters_dir"] = chapters_dir
    _state["summaries_dir"] = summaries_dir
    _state["book_title"] = book_title
    _state["pdf_path"] = path
    _state["chapter_images"] = chapter_images

    # Build chapter info with image counts and content flags
    chapters_info = []
    for ch in chapters:
        info = ch.to_dict()
        info["images_count"] = len(chapter_images.get(ch.index, []))
        info["is_content"] = _is_content_chapter(ch)
        chapters_info.append(info)

    result = {
        "status": "loaded",
        "metadata": metadata,
        "output_directory": str(book_dir),
        "chapters_directory": str(chapters_dir),
        "summaries_directory": str(summaries_dir),
        "chapters_detected": len(chapters),
        "chapters": chapters_info,
        "chapter_pdfs": chapter_files,
    }

    return json.dumps(result, indent=2)


@mcp.tool()
def list_chapters() -> str:
    """
    List all detected chapters in the currently loaded book.

    Each chapter includes its title, page range, page count, number of
    extracted images, and whether it's a content chapter (vs structural
    sections like TOC, Copyright, Index).

    Returns:
        JSON with book title and array of chapter metadata.
    """
    _ensure_book_loaded()

    chapters_info = []
    chapter_images = _state.get("chapter_images", {})
    for ch in _state["chapters"]:
        info = ch.to_dict()
        info["images_count"] = len(chapter_images.get(ch.index, []))
        info["is_content"] = _is_content_chapter(ch)
        chapters_info.append(info)

    result = {
        "book_title": _state["book_title"],
        "total_chapters": len(_state["chapters"]),
        "chapters": chapters_info,
    }

    return json.dumps(result, indent=2)


@mcp.tool()
def get_chapter_text(chapter_index: int) -> str:
    """
    Get the full text content of a specific chapter for summarization.

    Process ONE chapter at a time to avoid blowing LLM context. After
    receiving the text, generate a comprehensive summary, then call
    save_chapter_summary to persist it as a PDF.

    Text includes [IMAGE] markers where figures/diagrams appear, with
    dimensions and file paths for reference.

    Args:
        chapter_index: 1-based chapter index (as shown by list_chapters).

    Returns:
        The chapter's full text content with metadata header.
    """
    _ensure_book_loaded()

    idx = chapter_index - 1  # convert to 0-based
    chapters: list[Chapter] = _state["chapters"]

    if idx < 0 or idx >= len(chapters):
        raise ValueError(
            f"Invalid chapter index {chapter_index}. "
            f"Valid range: 1 to {len(chapters)}"
        )

    chapter = chapters[idx]
    images_info = _state.get("chapter_images", {}).get(idx, [])
    text = extract_chapter_text(_state["doc"], chapter, images_info)

    header = (
        f"=== {_state['book_title']} ===\n"
        f"=== Chapter {chapter_index}: {chapter.title} ===\n"
        f"=== Pages {chapter.start_page + 1}-{chapter.end_page + 1} "
        f"({chapter.page_count} pages) ===\n"
    )

    if images_info:
        header += (
            f"=== {len(images_info)} images extracted — "
            f"view them for diagrams/figures the text references ===\n"
        )

    header += "\n"
    logger.info(
        "Extracted text for chapter %d: %s (%d chars)",
        chapter_index, chapter.title, len(text),
    )
    return header + text


@mcp.tool()
def save_chapter_summary(chapter_index: int, summary_text: str) -> str:
    """
    Save a chapter summary as a formatted PDF file.

    Call this after generating a summary for a chapter. The summary text
    supports basic markdown formatting: headings (#, ##, ###), bullet
    points (-, *), and numbered lists.

    Args:
        chapter_index: 1-based chapter index.
        summary_text: The comprehensive summary text to save.

    Returns:
        JSON with status, chapter info, and path to saved PDF.
    """
    _ensure_book_loaded()

    idx = chapter_index - 1
    chapters: list[Chapter] = _state["chapters"]

    if idx < 0 or idx >= len(chapters):
        raise ValueError(
            f"Invalid chapter index {chapter_index}. "
            f"Valid range: 1 to {len(chapters)}"
        )

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

    logger.info("Saved summary for chapter %d: %s", chapter_index, output_path)

    return json.dumps(
        {
            "status": "saved",
            "chapter_index": chapter_index,
            "chapter_title": chapter.title,
            "summary_pdf": str(output_path),
        },
        indent=2,
    )


@mcp.tool()
def get_book_info() -> str:
    """
    Get metadata about the currently loaded book.

    Returns:
        JSON with title, author, page count, file info, output directory,
        and number of detected chapters.
    """
    _ensure_book_loaded()

    metadata = get_book_metadata(_state["doc"], _state["pdf_path"])
    metadata["output_directory"] = str(_state["book_dir"])
    metadata["chapters_detected"] = len(_state["chapters"])

    return json.dumps(metadata, indent=2)


@mcp.tool()
def get_summary_status() -> str:
    """
    Check which chapters have already been summarized.

    Scans the output directory for existing summary PDFs and reports
    each chapter's status as 'completed' or 'pending'. Use this to
    resume interrupted summarization workflows — already-completed
    chapters can be skipped.

    Returns:
        JSON with per-chapter status, overall progress count, and
        completion percentage.
    """
    _ensure_book_loaded()

    summaries_dir: Path = _state["summaries_dir"]
    book_dir: Path = _state["book_dir"]
    chapters: list[Chapter] = _state["chapters"]

    statuses = []
    completed = 0
    for ch in chapters:
        ch_index = ch.index + 1  # 1-based
        safe_title = sanitize_filename(ch.title)
        summary_name = f"chapter_{ch_index:02d}_{safe_title}_summary.pdf"
        # Check summaries/ subdir first, then book_dir for backward compat
        expected = summaries_dir / summary_name
        if not expected.exists():
            legacy = book_dir / summary_name
            if legacy.exists():
                expected = legacy
        is_done = expected.exists()
        if is_done:
            completed += 1
        statuses.append({
            "chapter_index": ch_index,
            "title": ch.title,
            "is_content": _is_content_chapter(ch),
            "status": "completed" if is_done else "pending",
            "summary_pdf": str(expected) if is_done else None,
        })

    content_chapters = [s for s in statuses if s["is_content"]]
    content_completed = sum(1 for s in content_chapters if s["status"] == "completed")

    return json.dumps({
        "total_chapters": len(chapters),
        "content_chapters": len(content_chapters),
        "completed": content_completed,
        "pending": len(content_chapters) - content_completed,
        "progress_pct": round(
            100 * content_completed / max(len(content_chapters), 1), 1,
        ),
        "chapters": statuses,
    }, indent=2)


@mcp.tool()
def search_book(query: str, max_results: int = 20) -> str:
    """
    Search across all chapters in the loaded book for a text query.

    Returns matching passages with chapter index, page number, and
    surrounding context. Case-insensitive. Useful for answering specific
    questions about the book without re-reading entire chapters.

    Args:
        query: The text to search for.
        max_results: Maximum number of matches to return (default 20).

    Returns:
        JSON with query, match count, and array of matches with context.
    """
    _ensure_book_loaded()

    doc: pymupdf.Document = _state["doc"]
    chapters: list[Chapter] = _state["chapters"]
    query_lower = query.lower()
    matches = []

    for ch in chapters:
        for page_num in range(ch.start_page, ch.end_page + 1):
            page = doc[page_num]
            text = page.get_text("text")
            if query_lower in text.lower():
                # Extract a context snippet around the match
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
    """
    Generate a compiled full-book summary PDF combining insights from all chapters.

    Call this once after all chapter summaries are complete. The summary_text
    should contain the overall book synthesis: core thesis, key learnings,
    who should read it, and how chapters connect.

    Args:
        summary_text: The full-book summary text (supports markdown formatting).

    Returns:
        JSON with status and path to the saved compiled summary PDF.
    """
    _ensure_book_loaded()

    book_dir: Path = _state["book_dir"]
    output_path = book_dir / "00_complete_book_summary.pdf"

    create_compiled_summary_pdf(
        summary_text=summary_text,
        book_title=_state["book_title"],
        output_path=output_path,
    )

    logger.info("Compiled book summary saved: %s", output_path)

    return json.dumps({
        "status": "saved",
        "summary_pdf": str(output_path),
        "book_title": _state["book_title"],
    }, indent=2)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


@mcp.prompt()
def summarize_book(pdf_path: str) -> str:
    """
    Generate a complete chapter-by-chapter book summary workflow.

    Provides step-by-step instructions for the LLM to follow.
    """
    return (  # noqa: E501
        "You are summarizing a PDF book chapter by chapter. "
        "Follow these steps EXACTLY:\n\n"
        "## Step 1: Find and load the book\n"
        "Call list_books() to see available books in the "
        "books/ folder.\n"
        f"Then call load_book with pdf_path=\"{pdf_path}\"\n"
        "(You can pass a number index, filename, or partial "
        "name.)\n"
        "Note the number of chapters and their titles.\n\n"
        "## Step 2: Check progress\n"
        "Call get_summary_status() to see which chapters are "
        "already done.\nSkip completed chapters.\n\n"
        "## Step 3: Process each pending content chapter "
        "sequentially\n"
        "For each content chapter (skip non-content chapters "
        "like Copyright, TOC, Index):\n\n"
        "1. Call get_chapter_text(chapter_index) to get the "
        "full text\n"
        "2. Read the text carefully and write a COMPREHENSIVE "
        "summary that includes:\n"
        "   - An overview of the chapter's main theme "
        "(2-3 sentences)\n"
        "   - All key concepts explained clearly\n"
        "   - Important frameworks, models, or methodologies "
        "discussed\n"
        "   - Practical takeaways and actionable insights\n"
        "   - Code examples or tools mentioned "
        "(with brief descriptions)\n"
        "   - How this chapter connects to the broader book "
        "narrative\n"
        "3. Format the summary with markdown headings "
        "(#, ##, ###), bullet points (-), and numbered lists\n"
        "4. Call save_chapter_summary(chapter_index, "
        "summary_text) to save as PDF\n\n"
        "## Step 4: After all chapters are done\n"
        "Write a full-book synthesis and call "
        "compile_book_summary(summary_text).\n\n"
        "IMPORTANT RULES:\n"
        "- Process ONE chapter at a time to avoid context "
        "overflow\n"
        "- Do NOT skip any content — be comprehensive but "
        "concise (no fluff)\n"
        "- If a chapter has [IMAGE] markers, note what the "
        "figure likely shows based on surrounding text\n"
        "- Each summary should be self-contained — readable "
        "without the original chapter\n"
        "- Start now with Step 1.\n"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    """Run the MCP server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
