"""
Chapter detection for PDF books.

Uses a hybrid approach to find chapter boundaries:

1. **TOC/Bookmarks** (primary): Parses the PDF's embedded Table of Contents.
   Most professionally published PDFs include this. Extracts top-level entries
   (level 1), falling back to level 2 if too few level-1 entries exist.

2. **Heuristic detection** (fallback): Scans page text for heading patterns —
   "Chapter X", "Part X", "Section X", numbered headings like "1. Introduction",
   and large-font text in the top 40% of a page.

3. **Whole document** (last resort): If neither method finds chapters, the entire
   PDF is treated as a single chapter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pymupdf

__all__ = ["Chapter", "detect_chapters"]


@dataclass
class Chapter:
    """A detected chapter in a PDF book."""

    index: int
    title: str
    start_page: int  # 0-based
    end_page: int  # 0-based, inclusive

    @property
    def page_count(self) -> int:
        return self.end_page - self.start_page + 1

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "title": self.title,
            "start_page": self.start_page + 1,  # 1-based for display
            "end_page": self.end_page + 1,
            "page_count": self.page_count,
        }


# Patterns that indicate chapter headings
CHAPTER_PATTERNS = [
    re.compile(r"^chapter\s+(\d+|[ivxlcdm]+)", re.IGNORECASE),
    re.compile(r"^part\s+(\d+|[ivxlcdm]+)", re.IGNORECASE),
    re.compile(r"^section\s+(\d+)", re.IGNORECASE),
    re.compile(r"^\d+\.\s+\w", re.IGNORECASE),  # "1. Introduction"
    re.compile(
        r"^(prologue|epilogue|introduction|conclusion|preface|foreword|"
        r"afterword|appendix)",
        re.IGNORECASE,
    ),
]

# Minimum pages for a chapter to be valid (avoids detecting single-page noise)
MIN_CHAPTER_PAGES = 2


def detect_chapters(doc: pymupdf.Document) -> list[Chapter]:
    """
    Detect chapters using hybrid approach: TOC first, heuristic fallback.

    Args:
        doc: An open PyMuPDF document.

    Returns:
        List of Chapter objects with page boundaries.
    """
    chapters = _detect_from_toc(doc)
    if chapters:
        return chapters

    chapters = _detect_from_heuristics(doc)
    if chapters:
        return chapters

    # Ultimate fallback: treat entire document as one chapter
    return [
        Chapter(
            index=0,
            title="Full Document",
            start_page=0,
            end_page=len(doc) - 1,
        )
    ]


def _detect_from_toc(doc: pymupdf.Document) -> list[Chapter]:
    """Extract chapters from PDF's embedded Table of Contents / bookmarks."""
    toc = doc.get_toc()
    if not toc:
        return []

    # Filter to top-level entries only (level 1)
    top_level = [(title, page - 1) for level, title, page in toc if level == 1]

    if len(top_level) < 2:
        # If only one or zero top-level entries, try level 2
        top_level = [(title, page - 1) for level, title, page in toc if level <= 2]

    if len(top_level) < 2:
        return []

    # Remove duplicates at the same page
    seen_pages: set[int] = set()
    unique_entries: list[tuple[str, int]] = []
    for title, page in top_level:
        if page not in seen_pages:
            seen_pages.add(page)
            unique_entries.append((title, page))

    # Sort by page number
    unique_entries.sort(key=lambda x: x[1])

    chapters = []
    total_pages = len(doc)

    for i, (title, start_page) in enumerate(unique_entries):
        if i + 1 < len(unique_entries):
            end_page = unique_entries[i + 1][1] - 1
        else:
            end_page = total_pages - 1

        # Clamp pages
        start_page = max(0, min(start_page, total_pages - 1))
        end_page = max(start_page, min(end_page, total_pages - 1))

        chapters.append(
            Chapter(
                index=i,
                title=title.strip(),
                start_page=start_page,
                end_page=end_page,
            )
        )

    return chapters


def _detect_from_heuristics(doc: pymupdf.Document) -> list[Chapter]:
    """Detect chapters by looking for heading patterns in text."""
    candidates: list[tuple[str, int]] = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        blocks = page.get_text("dict")["blocks"]

        for block in blocks:
            if block["type"] != 0:  # text blocks only
                continue

            for line in block.get("lines", []):
                text = ""
                max_font_size = 0.0

                for span in line.get("spans", []):
                    text += span["text"]
                    max_font_size = max(max_font_size, span["size"])

                text = text.strip()
                if not text or len(text) > 200:
                    continue

                # Check if this looks like a chapter heading
                is_heading = False

                # Pattern match
                for pattern in CHAPTER_PATTERNS:
                    if pattern.match(text):
                        is_heading = True
                        break

                # Large font heuristic (only if top 40% of page)
                if not is_heading and max_font_size >= 16 and len(text) < 100:
                    bbox = line["bbox"]
                    page_height = page.rect.height
                    if bbox[1] < page_height * 0.4:
                        is_heading = True

                if is_heading:
                    # Avoid duplicate detections on the same page
                    if not candidates or candidates[-1][1] != page_num:
                        candidates.append((text, page_num))
                    break  # only take first heading per page

    if len(candidates) < 2:
        return []

    # Filter out candidates that are too close together (< MIN_CHAPTER_PAGES)
    filtered: list[tuple[str, int]] = [candidates[0]]
    for title, page in candidates[1:]:
        if page - filtered[-1][1] >= MIN_CHAPTER_PAGES:
            filtered.append((title, page))

    if len(filtered) < 2:
        return []

    chapters = []
    total_pages = len(doc)

    for i, (title, start_page) in enumerate(filtered):
        if i + 1 < len(filtered):
            end_page = filtered[i + 1][1] - 1
        else:
            end_page = total_pages - 1

        chapters.append(
            Chapter(
                index=i,
                title=title.strip(),
                start_page=start_page,
                end_page=end_page,
            )
        )

    return chapters
