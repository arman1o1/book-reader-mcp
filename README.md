# 📚 book-reader-mcp

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](https://modelcontextprotocol.io)

MCP server that splits PDF books into chapters and serves them to LLMs for summarization. No LLM baked in — your MCP client's model does the thinking.

## Features

- **Auto chapter detection** — parses PDF bookmarks/TOC, falls back to heuristic heading detection
- **Chapter splitting** — saves each chapter as a separate PDF
- **Image extraction** — pulls figures/diagrams (>5KB) and injects `[IMAGE]` markers in text
- **Summary PDFs** — renders LLM-generated markdown summaries as formatted PDFs
- **Auto-resume** — detects completed summaries so interrupted workflows continue seamlessly
- **Full-text search** — search across all chapters with context snippets
- **Smart filtering** — auto-skips non-content sections (TOC, Copyright, Index, Glossary, etc.)
- **Organized folders** — `books/` for input PDFs, `books_summarized/` for structured output

## Folder Structure

```
project_root/
├── books/                                  # Drop PDF books here
│   ├── AI Agents in Action.pdf
│   └── Supply and Demand Trading.pdf
├── books_summarized/                       # Structured output per book
│   └── ai_agents_in_action/
│       ├── chapters/                       # Extracted chapter PDFs
│       │   ├── chapter_01_introduction.pdf
│       │   └── chapter_02_foundations.pdf
│       ├── summaries/                      # LLM-generated summary PDFs
│       │   ├── chapter_01_introduction_summary.pdf
│       │   └── chapter_02_foundations_summary.pdf
│       ├── images/                         # Extracted figures
│       │   ├── ch01_img_01.png
│       │   └── ch02_img_01.png
│       └── 00_complete_book_summary.pdf    # Compiled book summary
└── src/book_reader_mcp/
```

## Installation

### Using uv (recommended)

```bash
git clone https://github.com/arman1o1/book-reader-mcp.git
cd book-reader-mcp
uv sync
```

### Using pip

```bash
git clone https://github.com/arman1o1/book-reader-mcp.git
cd book-reader-mcp
pip install -e .
```

## Configuration

Add to your MCP client's config file:

### Claude Desktop

Edit `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "book-reader": {
      "command": "uv",
      "args": [
        "--directory", "/path/to/book-reader-mcp",
        "run", "book-reader-mcp"
      ]
    }
  }
}
```

### Cursor / Windsurf / Antigravity

Edit your MCP config (`mcp_config.json` or `settings.json`):

```json
{
  "mcpServers": {
    "book-reader": {
      "command": "uv",
      "args": [
        "--directory", "/path/to/book-reader-mcp",
        "run", "book-reader-mcp"
      ]
    }
  }
}
```

### If installed globally via pip

```json
{
  "mcpServers": {
    "book-reader": {
      "command": "book-reader-mcp"
    }
  }
}
```

## Tools

| Tool | Description |
|------|-------------|
| `list_books()` | List available PDFs in the `books/` folder with index numbers |
| `load_book(pdf_path, output_dir?)` | Load PDF by index, name, or path. Detects chapters, splits PDFs, extracts images |
| `list_chapters()` | List detected chapters with page ranges, image counts, and content flags |
| `get_chapter_text(chapter_index)` | Get full text of a chapter (1-based index) with `[IMAGE]` markers |
| `save_chapter_summary(chapter_index, summary_text)` | Save markdown summary as formatted PDF |
| `get_book_info()` | Get book metadata (title, author, pages, file size) |
| `get_summary_status()` | Check which chapters are done — enables auto-resume |
| `search_book(query, max_results?)` | Full-text search with context snippets |
| `compile_book_summary(summary_text)` | Generate combined book summary PDF |

## Workflow

Tell your LLM:

> "Summarize book 1" or "Summarize the Supply and Demand book"

The LLM will call the tools in sequence:

```
1. list_books()                           → shows available books
2. load_book("1")                         → loads by index, detects chapters
3. get_summary_status()                   → checks for completed summaries
4. get_chapter_text(1)                    → extracts chapter 1 text
5. [LLM generates summary]
6. save_chapter_summary(1, summary)       → saves to summaries/ folder
7. ... repeats for each chapter ...
8. compile_book_summary(full_synthesis)   → saves combined summary
```

### Loading Books

`load_book` accepts multiple input formats:

```
load_book("1")                    → book index from list_books
load_book("Supply and Demand")    → partial name match in books/
load_book("mybook.pdf")           → filename in books/
load_book("C:/full/path.pdf")     → absolute path (backward compatible)
```

## Chapter Detection

The server uses a three-tier approach:

1. **TOC/Bookmarks** (primary) — Most professionally published PDFs include an embedded table of contents. The server parses level-1 entries, falling back to level-2 if too few exist.

2. **Heuristic detection** (fallback) — Scans for patterns like "Chapter X", "Part X", numbered headings ("1. Introduction"), and large-font text (≥16pt) in the top 40% of pages.

3. **Whole document** (last resort) — If neither method finds chapters, the entire PDF is treated as a single chapter.

## Development

```bash
# Install with dev dependencies
uv sync --extra dev

# Run tests
uv run pytest

# Lint
uv run ruff check src/

# Run the server directly
uv run book-reader-mcp
```

## License

[MIT](LICENSE)
