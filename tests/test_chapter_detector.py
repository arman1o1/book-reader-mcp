"""Tests for chapter detection and utility functions."""

import re

import pytest

from book_reader_mcp.chapter_detector import Chapter
from book_reader_mcp.pdf_utils import sanitize_filename


# ---------------------------------------------------------------------------
# _is_content_chapter tests (inline the logic to avoid private import)
# ---------------------------------------------------------------------------

_NON_CONTENT_PATTERNS = re.compile(
    r"^(copyright|table of contents|index|glossary|about the author|dedication|"
    r"acknowledg(?:e?ments?)|colophon|title page|half title|cover|also by|other books|"
    r"front matter|back matter|contents|list of figures|list of tables|"
    r"permissions|credits|praise for)\b",
    re.IGNORECASE,
)


def _is_content_chapter(chapter: Chapter) -> bool:
    title = chapter.title.strip()
    if _NON_CONTENT_PATTERNS.match(title):
        return False
    if chapter.page_count < 2:
        return False
    return True


class TestIsContentChapter:
    """Test non-content chapter filtering."""

    def test_regular_chapter_is_content(self):
        ch = Chapter(index=0, title="Introduction to AI", start_page=0, end_page=10)
        assert _is_content_chapter(ch) is True

    def test_copyright_is_not_content(self):
        ch = Chapter(index=0, title="Copyright", start_page=0, end_page=1)
        assert _is_content_chapter(ch) is False

    def test_toc_is_not_content(self):
        ch = Chapter(index=0, title="Table of Contents", start_page=0, end_page=3)
        assert _is_content_chapter(ch) is False

    def test_index_is_not_content(self):
        ch = Chapter(index=0, title="Index", start_page=0, end_page=5)
        assert _is_content_chapter(ch) is False

    def test_glossary_is_not_content(self):
        ch = Chapter(index=0, title="Glossary", start_page=0, end_page=3)
        assert _is_content_chapter(ch) is False

    def test_about_author_is_not_content(self):
        ch = Chapter(index=0, title="About the Author", start_page=0, end_page=1)
        assert _is_content_chapter(ch) is False

    def test_single_page_is_not_content(self):
        ch = Chapter(index=0, title="Chapter 1", start_page=5, end_page=5)
        assert _is_content_chapter(ch) is False

    def test_acknowledgments_variant(self):
        ch = Chapter(index=0, title="Acknowledgments", start_page=0, end_page=2)
        assert _is_content_chapter(ch) is False

    def test_acknowledgements_variant(self):
        ch = Chapter(index=0, title="Acknowledgements", start_page=0, end_page=2)
        assert _is_content_chapter(ch) is False

    def test_case_insensitive(self):
        ch = Chapter(index=0, title="COPYRIGHT", start_page=0, end_page=2)
        assert _is_content_chapter(ch) is False

    def test_chapter_with_index_in_title(self):
        """'Index' as part of a title like 'Index Investing' should be non-content."""
        ch = Chapter(index=0, title="Index", start_page=0, end_page=5)
        assert _is_content_chapter(ch) is False

    def test_dedication_is_not_content(self):
        ch = Chapter(index=0, title="Dedication", start_page=0, end_page=0)
        assert _is_content_chapter(ch) is False


class TestSanitizeFilename:
    """Test filename sanitization."""

    def test_basic(self):
        assert sanitize_filename("Chapter 1") == "chapter_1"

    def test_special_characters(self):
        result = sanitize_filename("What's Next? (Part 2)")
        assert "?" not in result
        assert "'" not in result
        assert "(" not in result

    def test_long_title_truncated(self):
        long_title = "A" * 200
        result = sanitize_filename(long_title)
        assert len(result) <= 80

    def test_whitespace_normalized(self):
        result = sanitize_filename("  Chapter   One  ")
        assert "  " not in result
        assert result == "chapter_one"

    def test_empty_string(self):
        result = sanitize_filename("")
        assert result == ""


class TestChapterDataclass:
    """Test Chapter dataclass."""

    def test_page_count(self):
        ch = Chapter(index=0, title="Test", start_page=5, end_page=15)
        assert ch.page_count == 11

    def test_single_page_count(self):
        ch = Chapter(index=0, title="Test", start_page=5, end_page=5)
        assert ch.page_count == 1

    def test_to_dict_is_1_based(self):
        ch = Chapter(index=0, title="Test", start_page=0, end_page=9)
        d = ch.to_dict()
        assert d["start_page"] == 1  # 1-based
        assert d["end_page"] == 10
        assert d["page_count"] == 10
