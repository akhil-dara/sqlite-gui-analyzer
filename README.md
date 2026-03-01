# SQLite Forensic Analyzer

**A fast, zero-dependency desktop GUI for searching, browsing, and forensically analyzing SQLite databases -- including hidden WAL (Write-Ahead Log) data that standard tools miss.**

Built with Python and tkinter. Zero dependencies. Read-only -- your data is never modified.

> Perfect for forensic investigators, developers, data analysts, and anyone who needs to quickly explore SQLite databases without writing SQL.

![Search](Screenshots/search.png)
![Browse](Screenshots/browse.png)
![Schema](Screenshots/schema.png)

---

## Why This Tool?

Most SQLite viewers choke on large databases, require SQL knowledge, or need complex installations. This tool is different:

- **One file, zero setup** -- just run `python sqlite_gui_analyzer.py`
- **Handles huge databases** -- tested on 5 GB+ databases with 270+ tables and millions of rows
- **Blazing-fast search** -- 3000+ results across 272 tables in under 5 seconds
- **WAL forensic recovery** -- recovers uncommitted, deleted, and overwritten data from WAL files
- **Read-only** -- your database is never modified (opened with `?mode=ro`)
- **No SQL needed** -- point, click, search, browse
- **Cross-platform** -- Windows, macOS, Linux

---

## Features

### Search Across All Tables

Search your entire database with a single query. Results stream in live.

- **8 search modes**: Case-Insensitive, Case-Sensitive, Exact Match, Starts With, Ends With, Regex, BLOB/Hex, Column Name
- Per-table result counts with expandable grouping
- Filter results by table, column, data type, or source (DB / WAL)
- Regex engine with automatic LIKE pre-filtering for fast pattern matching
- Configurable limits (100 / 500 / 1,000 / 5,000 / All per table)
- Search scope -- restrict to specific tables
- **Search WAL data** alongside regular results with source indicators
- Export to CSV or JSON
- Pagination for large result sets

### Browse Tables

Browse any table with sortable columns, row filtering, and pagination.

- Click column headers to sort ascending/descending
- **Per-column filter entries** -- type a filter below any column header
- BLOB detection with file type and size display (e.g., `[JPEG 1.2 KB]`)
- CSV export -- full table or current page
- Bulk BLOB export with progress tracking
- WAL-only tables accessible from the Browse dropdown

### Schema Sidebar

Tree view of every table, view, index, and trigger.

- Expand tables to see columns with types, constraints, and foreign keys
- Filter tables and columns by name
- Right-click context menu: browse, preview rows, copy CREATE SQL, set search scope, export CSV
- SQL preview pane with Copy Schema / Copy SQL buttons
- **Interactive HTML schema report** -- with live search, copy buttons, constraint badges, and print layout

### WAL Forensic Analysis

Dedicated tab for analyzing SQLite Write-Ahead Log (WAL) files at the binary level. Recovers data that standard SQLite tools cannot see.

![WAL Tab](Screenshots/wal_tab.png)
![WAL Search](Screenshots/wal-search.png)

- **Pure binary WAL parser** using memory-mapped I/O -- reads data that standard SQLite APIs hide
- **Automatic WAL backup** -- WAL file is backed up before opening so forensic data is never lost
- **Frame classification**: Saved (committed), Unsaved (uncommitted), Overwritten (older versions)
- **Frame browser** with filtering by status, table, page type, and page number
- **All Records view** -- browse all recovered WAL records with diff indicators:
  - `✓` Same as DB | `≠` Different from DB | `∅` Not in DB | `★` WAL-only table
- **Show filter**: All / Different from DB / WAL Only / WAL-Only Tables / Same as DB
- **WAL-only table detection** -- tables created in uncommitted transactions (invisible to SQLite) are marked with ★
- **Summary panel** -- per-table WAL statistics with record counts and forensic notes
- **WAL Record detail** -- full column view with DB comparison, word wrapping, copy per column
- **Transaction grouping** -- frames grouped by salt values and commit markers
- **Technical Details** -- WAL header metadata in a dedicated window
- **Export** -- frame summary CSV, all records CSV, BLOB export
- **Close confirmation** -- warns before closing if WAL data exists, shows backup location

### Row Detail View

Double-click any row for a detailed view.

- All values displayed with full column names (no truncation)
- Multi-line text in scrollable widget with word wrapping
- BLOB viewer with hex dump, text view, and image preview (zoom supported)
- Copy row as JSON, CSV, or plain text
- Search result highlighting -- scrolls to and highlights the matched column

### BLOB Viewer

- Hex dump (first 16 KB) with copy as raw hex, formatted, or Base64
- UTF-8 text view (first 64 KB)
- Image preview with zoom (Fit / 100% / Zoom+ / Zoom- / mouse wheel)
- Save to file with auto-detected extension
- Detected formats: JPEG, PNG, GIF, TIFF, PDF, ZIP, GZIP, SQLite, RIFF/WebP, bplist, XML/Plist, MP3/ID3

### Timestamp Decoding

Numeric values are automatically decoded in the Row Detail view:

- Unix Epoch (seconds and milliseconds)
- WebKit/Chrome (microseconds since 1601-01-01)
- Windows FILETIME (100ns intervals since 1601-01-01)
- Mac/Cocoa Absolute Time (seconds since 2001-01-01)
- GPS Time (seconds since 1980-01-06)

---

## Getting Started

### Requirements

- **Python 3.8+**
- **tkinter** (included with most Python installs)
- **Pillow** (optional, for JPEG/WEBP image previews): `pip install Pillow`

### Run

```bash
python sqlite_gui_analyzer.py
```

Open a database directly:

```bash
python sqlite_gui_analyzer.py path/to/database.db
```

That's it. No pip install, no virtual env, no config files.

---

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Ctrl+O` | Open database |
| `Ctrl+F` | Focus search bar |
| `Enter` | Start search / Open row detail |
| `Escape` | Cancel running search |
| `Double-click` | Open row detail / WAL record detail |
| `Right-click` | Schema tree context menu |

---

## How Search Works

### Standard Search (LIKE-based)

One combined SQL query per table:

```sql
SELECT rowid, * FROM "table_name"
WHERE col1 LIKE ? OR col2 LIKE ? OR ... LIMIT 500
```

BLOB columns are excluded by default. Enable Deep BLOB mode to include them.

### Regex Search (LIKE + Python re)

Two-phase approach for speed:

1. **Literal hint extraction** -- extracts the longest guaranteed literal from the regex via `sre_parse`
2. **SQL pre-filter** -- `LIKE '%hint%'` narrows rows at the DB level before Python regex
3. **Batch processing** -- results fetched in batches of 5,000 rows

Delivers regex search in **3-5 seconds** on million-row tables.

### Performance

Tested with a 3.3 GB WhatsApp database (272 tables, 2.4M+ messages):

| Search Type | Example | Time |
|-------------|---------|------|
| Case-insensitive | `"hello"` | ~5s |
| Email regex | `[a-zA-Z0-9._%+-]+@...` | ~3-5s |
| URL regex | `https?://[^\s]+` | ~3-4s |
| Phone regex | `\d{3}-\d{3}-\d{4}` | ~4s |

---

## Supported Databases

Works with any valid SQLite database, including:

- WhatsApp (`msgstore.db`, `wa.db`, `whatsapp_status.db`)
- Signal, Telegram, and other messaging apps
- Chrome / Firefox / Safari browser databases
- iOS / Android backups and app databases
- Django, Flask, Rails development databases
- Any `.db`, `.sqlite`, `.sqlite3`, or `.db3` file

---

## Technical Details

| | |
|---|---|
| Language | Python 3.8+ |
| GUI | tkinter / ttk |
| DB Access | `sqlite3` with URI read-only mode |
| WAL Parser | Pure binary parser with mmap |
| Architecture | Modular (8 source files), multi-threaded |
| Regex Engine | AST-based hint extraction via `sre_parse` |
| BLOB Detection | Signature-based (14+ formats) |
| Unicode | Full UTF-8 including emoji |

### Project Structure

```
sqlite_gui_analyzer.py       Entry point
src/
  app.py                     Main application window
  database.py                SQLite connection and schema introspection
  wal_parser.py              Binary WAL file parser
  dialogs.py                 Help, Scope, BlobViewer, RowWin dialogs
  widgets.py                 ToolTip, TreeviewTooltip, theme setup
  utils.py                   Utility functions, schema HTML generator
  constants.py               Shared constants, colors, BLOB signatures
Screenshots/
  search.png                 Search tab
  browse.png                 Browse tab
  schema.png                 Schema sidebar
  wal_tab.png                WAL forensic analysis tab
  wal-search.png             WAL search results
test_data/
  wal_demo.db                Demo database with WAL forensic data
```

### Demo Database

A demo database with WAL forensic data is included for testing:

```bash
python sqlite_gui_analyzer.py test_data/wal_demo.db
```

The demo includes 18 tables, views, indexes, triggers, and a WAL file with committed, uncommitted, and overwritten records -- ideal for exploring the forensic analysis features.

---

## FAQ

**Will this tool modify my database?**
No. The database is opened in read-only mode (`?mode=ro`). Your data is never changed.

**Does it handle WAL mode databases?**
Yes. WAL databases are supported at two levels:

1. **Standard access** -- opens with `?mode=ro` which reads committed WAL data normally.
2. **Forensic WAL analysis** -- a dedicated Hidden Data (WAL) tab appears automatically when a `.db-wal` file exists. This uses a pure binary parser (not sqlite3) to read ALL WAL data including uncommitted transactions, rolled-back data, and overwritten frames that standard SQLite APIs hide.

**Will it destroy my WAL file?**
No. The WAL file is automatically backed up (as `.db-wal.bak`) before any connection is opened.

**Can it handle large databases?**
Yes. Tested on databases over 5 GB with hundreds of tables and millions of rows.

**Do I need to know SQL?**
No. All functionality is available through the GUI.

**What about encrypted databases?**
Standard unencrypted SQLite databases only. Encrypted databases (SQLCipher, etc.) must be decrypted first.

---

## Contributing

Contributions, bug reports, and feature requests are welcome.

## License

MIT
