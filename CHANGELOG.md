# Changelog

Notable changes in each release. The section of a version is also the text of its GitHub
release.

## 2.1.0

A large update: a new look with light and dark themes, many databases opened as one case,
stronger forensic tools, and protection against crafted evidence files.

### Highlights

- **Cases of many databases.** Open a whole extraction folder (30–60 databases): one
  navigator, an Overview, one search across everything, a Timeline across all databases and
  the links found between them. Files are found by their content, whatever they are called.
- **A new interface.** New logo, light and dark themes, interface sizes (View ▾), a welcome
  screen, a collapsible databases panel, a command palette (Ctrl+K) and a layout that works
  from small laptop screens to large monitors.
- **Spreadsheet-style column filters.** Value lists with counts and a search over the whole
  column, text / number / date conditions, filter chips, saved filters and undo.
- **Relationships.** Links found from foreign keys and from the values themselves, inside a
  database and between databases, shown as a diagram, as related rows and in Find this value
  everywhere.
- **Timeline.** Every dated row of every database in time order, with a density chart, named
  time zones and a sample value for each date format.

### New

- Dates shown in the format you choose, with an example of each style or a pattern of your
  own; values that are 0 or negative read "not set" instead of 1970 or 1601.
- Row detail: every value selectable, Copy as UTC / Unix / WebKit, Related rows.
- Row History lists the rows that have older versions, table by table.
- BLOB inspector: each value opens in the view that suits its format (XML for plists,
  fields for protobuf, JSON, text), with search.
- Export BLOBs as files as stored, decoded as JSON, or plists as XML property lists, with a
  manifest of every file.
- Tags and notes on rows, with HTML, CSV and JSON exports that record where every value came
  from.
- Copy with related and a Database Map of a database or a whole case.
- Safe parse: open a file with the tool's own parser only.
- zstd, LZ4 and LZFSE compressed BLOBs are decoded on every Python version.

### Improved

- Opening, scrolling and searching stay responsive with millions of rows and large cases;
  long work runs in the background with progress and Stop.
- Exports of large tables are streamed, and HTML reports handle very large tables.
- HTML reports: dates in any time zone (UTC, local time or a named zone), Wrap text,
  double-click a column edge to fit it, each BLOB's decoded value in the row details, and
  Inspect… on a BLOB: its decoded value, hex and the text inside, with Find (text or hex)
  and Save. The table search also finds words inside decoded BLOBs.
- The home page says where your tags and settings are saved, with Open and Change.
- Every limit is a setting in Limits…, and the tool always says when one cut something.

### Security

- Every file is treated as possibly crafted: table definitions in a database are never run,
  views and computed values are limited, and sizes a file claims are checked before anything
  is loaded, so damaged or forged files cannot run code or exhaust memory.
- CSV exports are safe to open in a spreadsheet, and HTML reports run only their own script.
- Network paths are never contacted unless you open them; nothing is written into an evidence
  folder.

### Fixed

- Many fixes to layout on small screens and high display scaling, dark theme colours,
  timestamps and filters.
- Numbers in columns that are not dates (flags, counters) are no longer offered as dates;
  text holding binary data reads as such instead of raw control characters.

## 2.0.0

A rewrite around a new evidence-safe engine.

- **Evidence-safe engine.** Databases are opened read-only with SQLite's immutable mode and are
  never copied; nothing is written in the evidence folder, not even a `-shm` file. Committed WAL
  frames are merged in RAM. Size, modification time and SHA-256 of the database and its sidecar
  files are recorded on open and verified on close, and exports into the evidence folder are
  refused.
- **WITHOUT ROWID tables** are browsed, counted and searched like any other table (for example
  OneDrive and Windows Search databases), as are views, UTF-16 databases and invalid text.
- **WAL frame states and hidden data.** The Hidden Data (WAL) tab replays the WAL with SQLite's
  own checksum rules and labels every frame Current, Superseded, Uncommitted or Stale (an earlier
  WAL generation), so data SQLite hides can be browsed, compared with the database and searched.
- **Native recovery of damaged databases.** Files SQLite rejects as malformed are read by the
  tool's own B-tree parser, which shows every row it can still reach. Damaged records are marked
  and never filled with invented values; status chips, per-table notes and an Issues list say what
  could not be read.
- **Faster parallel search with one line per row.** Several tables are searched at once, each on
  its own read-only connection, and results stream in. A row matching in several columns is one
  line with its matching cells underneath, and a WAL copy identical to a database row is shown on
  that row's line. Running searches and SQL queries can be cancelled. Views can be searched too
  ('Include views', off by default) and chosen in the search scope.
- **BLOB Inspector.** BLOBs are decoded into a tree: binary and XML property lists, keyed
  archives with objects, class names and dates resolved, protobuf without a schema,
  typedstream message text, JSON, base64, text in UTF-8 or UTF-16, and images. Compressed
  layers (gzip, zlib, deflate, bz2, xz, and zstd on Python 3.14) and data nested inside data are
  decoded again. LZ4 frames (checksums verified) and Apple LZFSE / LZVN / LZ4 streams (`bvx2`,
  `bvx1`, `bvxn`, `bvx-`, `bv41`, `bv4-`) are decompressed in pure Python. Selecting a value highlights its bytes in a hex view that scrolls smoothly
  through BLOBs of any size, and clicking a byte selects the value stored there. Numbers can be
  read as dates in every common epoch. Row Detail shows a one-line summary of each BLOB.
- **Forensics tab.** Deleted records recovered from free space inside pages, freed pages, WAL
  and journal copies, each with its location and a confidence; the version history of any row
  across the main file, the WAL and the journal; dropped tables with their rows; the table state
  before an unfinished transaction from the rollback journal; an audit of the file; and HTML,
  CSV or JSON reports with the evidence hashes. Deleted entries of indexes (plain, composite,
  DESC, expression, UNIQUE, partial and automatic indexes) are recovered too, with the indexed
  values and the rowid, and linked to the table records of the same row, or flagged when they
  are all that is left of it.
- **Data grid.** Browse and SQL results scroll smoothly through millions of rows and hundreds
  of columns. Every column has a filter under its header (`>5`, `1~5`, `/regex/`, `NULL`,
  `!text`, ...), with the same results whether SQLite or the tool's own reader serves the
  table; a row inspector shows the selected row's values one per line.
- **Byte-level search.** Text is found inside BLOBs as UTF-8, UTF-16LE and UTF-16BE; hex
  patterns match on byte boundaries with `??` wildcards; search can also look inside decoded
  BLOBs and in the records of freed pages. Each BLOB hit shows its encoding and byte offset.
- **SQL Query Editor** tab (read-only) and **Deleted Pages** tab (records recovered from freelist
  pages).
- **Tags and reports.** Rows of tables, views, WAL versions and freed pages can be tagged
  (Relevant, Review, Suspicious, Not relevant or your own tags) with a note, one by one, with
  `Ctrl+T` / `Ctrl+1..9`, or all rows a filter or a search keeps. The Tagged tab lists any number
  of them in the virtual grid, with column filters and the tag colours, and
  they export to a printable HTML report, a CSV folder with the BLOBs as files, or JSON, each with
  the evidence SHA-256 hashes and the rows as they were when tagged. Tags, notes and the Browse
  layout are saved per database in the application data folder, never next to the evidence; a
  Recent menu reopens databases.
- **Relationships.** The links between tables are mapped in the background when a database
  opens (declared foreign keys, names such as `message_row_id` -> `message._id`, shared id-like
  names) and trusted only when a sample of up to 200 values agrees. Right-click a value for its
  Related rows, listed only when related tables hold it, with their row counts; open those
  rows, filter Browse on them or tag them all. "Find this value everywhere" and "Find inside
  other values" look for a value in every table, WAL row version and freed-page record. The
  Relationships tab lists every link and draws them as a diagram, exported to CSV and SVG.
- **Copy with related and the Database Map.** "Copy with related" takes a row, the selected
  rows or all rows a filter keeps, with every row a trusted link leads to, each with the link it
  came through and its evidence, the schema of the tables, dates converted beside the raw value
  and BLOBs described, as Markdown, JSON or SQL (JOIN queries returning exactly those rows);
  large selections stream to a file with progress and Stop. "Export Database Map…" writes one
  self-contained HTML file (or Markdown or JSON) with every table, the links and their evidence,
  date and BLOB columns, ready-to-run JOIN queries, the diagram and the evidence hashes. Every
  cap is a named limit, and what a limit leaves out is said in the output. In a case of several
  databases both follow the links between databases (matched by value): related rows name
  their database, and the map covers the active database or the whole case.
- **Timeline tab.** The date columns of every table are found by name and by their values (Unix
  s/ms/µs/ns, Cocoa, WebKit, FILETIME, HFS+, .NET, OLE and ISO 8601 / RFC 2822 text), each with
  a confidence and the reason, while ids, counters, sizes and phone numbers are left out. Their
  events are listed in time order with the row and a short description, optionally with the
  WAL's older row versions and the recovered records, limited to a date range that SQLite
  itself filters; open, tag and export them as CSV, JSON or HTML.
- **Show as date.** Any Browse column can show its numbers as UTC dates in a chosen or
  detected format, while filters, sorting and exports keep the stored values.
- **Windows installer and portable builds.** One setup program installs for the current user (no
  administrator rights) or for all users; the portable zip and single-file exe need no
  installation. The builds include Pillow for JPEG and WEBP previews. New command-line options
  `--version` and `--self-test`.
- Running from source needs only Python 3.8 to 3.14 with the standard library; Pillow stays
  optional.
