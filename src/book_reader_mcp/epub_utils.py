"""
EPUB utilities: extracting metadata, chapters, text, and images from EPUB files.

Uses ebooklib for reading EPUBs and BeautifulSoup4 for HTML parsing.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import ebooklib
from bs4 import BeautifulSoup
from ebooklib import epub

from .chapter_detector import Chapter

logger = logging.getLogger("book-reader-mcp")

__all__ = [
    "get_epub_metadata",
    "extract_epub_chapters",
    "extract_epub_text",
    "extract_epub_images",
    "search_epub",
]


def get_epub_metadata(epub_path: Path) -> dict[str, Any]:
    """Extract book metadata from an EPUB file."""
    book = epub.read_epub(str(epub_path))
    
    title = book.get_metadata("DC", "title")
    author = book.get_metadata("DC", "creator")
    
    # Metadata returns lists
    title_str = title[0][0] if title else epub_path.stem
    author_str = author[0][0] if author else "Unknown"
    
    return {
        "title": title_str,
        "author": author_str,
        "subject": "",
        "total_pages": 0,  # EPUBs don't have fixed pages
        "file_name": epub_path.name,
        "file_size_mb": round(epub_path.stat().st_size / (1024 * 1024), 2),
    }


def _get_text_from_html(html_content: bytes) -> str:
    """Extract clean text from HTML content."""
    soup = BeautifulSoup(html_content, "html.parser")
    # Remove script and style elements
    for script_or_style in soup(["script", "style"]):
        script_or_style.decompose()
    
    # Get text, preserving some structure
    text = soup.get_text(separator="\n")
    # Clean up multiple newlines
    return re.sub(r"\n\s*\n", "\n\n", text).strip()


def _get_title_from_html(html_content: bytes, default: str) -> str:
    """Try to find a meaningful title in HTML content."""
    soup = BeautifulSoup(html_content, "html.parser")
    
    # Try H1, H2, H3 in order
    for tag in ["h1", "h2", "h3"]:
        header = soup.find(tag)
        if header and header.get_text().strip():
            return header.get_text().strip()
    
    # Fallback to title tag
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    
    return default


def _get_ordered_items(book: epub.EpubBook) -> list[epub.EpubHtml]:
    """Get HTML items in the correct order from the spine."""
    items = list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))
    item_map = {item.id: item for item in items}
    
    ordered_items = []
    for item_ref in book.spine:
        # Spine entries can be (id, linear) or just id
        item_id = item_ref[0] if isinstance(item_ref, tuple) else item_ref
        if item_id in item_map:
            ordered_items.append(item_map[item_id])
            
    # Fallback if spine is empty or incomplete
    if not ordered_items:
        ordered_items = items
    return ordered_items


def _get_merged_chapters(epub_path: Path) -> list[dict]:
    """Internal helper to get merged chapter data."""
    book = epub.read_epub(str(epub_path))
    ordered_items = _get_ordered_items(book)
    
    raw_sections = []
    for i, item in enumerate(ordered_items):
        content = item.get_content()
        text = _get_text_from_html(content)
        title = _get_title_from_html(content, f"Section {i+1}")
        
        raw_sections.append({
            "id": item.id,
            "title": title,
            "text": text,
            "length": len(text)
        })

    merged_chapters = []
    current_chapter = None
    
    for section in raw_sections:
        if section["length"] < 10:
            continue
            
        is_generic = section["title"].lower().startswith("section ")
        is_small = section["length"] < 1500
        
        if current_chapter is None:
            current_chapter = {
                "title": section["title"],
                "text": section["text"],
                "ids": [section["id"]]
            }
        elif is_small or is_generic:
            current_chapter["text"] += "\n\n" + section["text"]
            current_chapter["ids"].append(section["id"])
            # Adopt more specific title if current is generic
            if current_chapter["title"].lower().startswith("section ") and not is_generic:
                current_chapter["title"] = section["title"]
        else:
            merged_chapters.append(current_chapter)
            current_chapter = {
                "title": section["title"],
                "text": section["text"],
                "ids": [section["id"]]
            }
            
    if current_chapter:
        merged_chapters.append(current_chapter)
    return merged_chapters


def extract_epub_chapters(epub_path: Path) -> list[Chapter]:
    """Detect logical chapters in an EPUB file."""
    merged_chapters = _get_merged_chapters(epub_path)
    
    final_chapters = []
    for i, ch_data in enumerate(merged_chapters):
        estimated_pages = max(2, len(ch_data["text"]) // 1500)
        ch = Chapter(
            index=i,
            title=ch_data["title"],
            start_page=0,
            end_page=estimated_pages - 1,
        )
        final_chapters.append(ch)
    return final_chapters


def extract_epub_text(epub_path: Path, chapter_index: int, chapters: list[Chapter]) -> str:
    """Extract text for a logical chapter."""
    merged_chapters = _get_merged_chapters(epub_path)
    if 0 <= chapter_index < len(merged_chapters):
        return merged_chapters[chapter_index]["text"]
    return ""


def extract_epub_images(epub_path: Path, chapter_index: int, output_dir: Path) -> list[dict]:
    """Extract all images from the EPUB."""
    images_info = []
    images_dir = output_dir / "images"
    images_dir.mkdir(exist_ok=True)
    
    book = epub.read_epub(str(epub_path))
    image_items = list(book.get_items_of_type(ebooklib.ITEM_IMAGE))
    
    for i, item in enumerate(image_items):
        ext = Path(item.file_name).suffix.lstrip('.') or "png"
        img_filename = f"image_{i+1:03d}.{ext}"
        img_path = images_dir / img_filename
        
        with open(img_path, "wb") as f:
            f.write(item.get_content())
            
        images_info.append({
            "index": i + 1,
            "path": str(img_path),
            "name": item.file_name
        })
    return images_info


def search_epub(
    epub_path: Path,
    query: str,
    chapters: list[Chapter],
    max_results: int = 20,
) -> list[dict]:
    """Search across all EPUB chapters for a text query."""
    merged = _get_merged_chapters(epub_path)
    query_lower = query.lower()
    matches = []
    for i, ch in enumerate(chapters):
        if i >= len(merged):
            break
        text = merged[i]["text"]
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
                "snippet": snippet,
            })
            if len(matches) >= max_results:
                break
    return matches
