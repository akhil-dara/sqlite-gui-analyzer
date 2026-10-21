# Changelog

Notable changes in each release. The section of a version is also the text of its GitHub
release.

## 3.0.0

Deleted-record recovery rebuilt around what examiners need to trust it, a query builder that
needs no SQL, the values inside BLOBs as columns, formulas of your own, folder scans picked by
your own conditions, scrolling that keeps up with millions of rows, and plain words for what a
database does by itself.

### Highlights

- **Recovered records you can trust.** Every record says what it is - deleted, older version,
  inserted again, unreferenced page, index entry - where it was found, how sure the match to
  a table is and why. Copies of live rows are left out, even when the bytes fit several
  tables; rows on pages nothing references are listed apart (they may be live rows a damaged
  or manipulated tree hides).
- **Query without SQL.** Add tables and they join on their links; tick columns; filter with
  month bars, value lists, number ranges or any SQLite condition; summarise, label values,
  calculate new columns; across the databases of a case, and over recovered rows too.
- **Fields inside values.** Open a BLOB or JSON column (plists, NSKeyedArchiver, protobuf,
  JSON) and use its fields - a payment's sender, receiver and amount - as columns in Query
  and its exports (also to join other tables), and as columns of their own in Browse
  (`ƒ sender.name`, `ƒ amount`: sorted, filtered, copied and exported like any column).
- **Formulas of your own, as new columns.** Right-click a column header › Calculate
  from…: a formula in Excel-like terms (`round(x / 100, 2)`, `x * qty + fee`,
  `upper(left(name, 3))`, `x if x > 0 else 0`, `date(x, "ms")`, `json(x, "to.name")`,
  any column of the row by name; sizes `size(x)`, durations, date parts and zones,
  conversions between date kinds, hashes, base64 / hex / UTF-8, regular expressions,
  fields inside JSON, plist, NSKeyedArchiver and protobuf values) or steps (divide,
  round, thousands, text edits…),
  checked as you type with a live preview on real rows; the result is a new ƒ= column,
  sortable and exportable, remembered per table; the stored values never change.
- **Show as number.** Right-click a column header › Show as number: thousands
  separators, ÷100 (amounts kept in the smallest unit), ÷1,000, ÷1,000,000, exact for
  integers, a badge on the header, the stored value kept in copies and beside the shown
  text in exports; remembered per table and column; works on ƒ columns.
- **Pick the databases of a folder by your own conditions.** Open Folder goes in three
  steps - the folder, which files (optional conditions on the file name, path in the
  folder, folder name, extension or size: is, is not, contains, starts / ends with, matches
  `*` `?`, at least / at most; all or any of them), then Scan for databases, with a line
  saying beforehand what the scan will do. Files that do not match are never opened, so a
  scan of a whole users folder for the files named one way reads only those (890,000 files
  seen, 1,900 opened, in about a minute). The list fills as databases are found; a stopped
  scan keeps them and offers Scan again; Tick shown only, and sort by any column.
- **Drag the scrollbar anywhere in millions of rows.** The rows follow the thumb as it
  moves (a 2.5-million-row table: every step of a drag shows the rows at the thumb), the
  scrollbar spans the whole table at once (the row count is no longer held back behind
  other work on the table), a jump draws about three times faster, and the status line
  says 'reading rows' only when a read really keeps the view waiting.
- **Triggers in plain words.** What each trigger does - deletes rows of another table,
  deletes its own table's old rows, writes a change log, refuses a change, calls an app
  function - above its SQL; per table, which triggers change it; a recovered record says
  when its table's rows are deleted or logged by the database itself.
- **The database as of any commit.** Open a WAL database as it was after any of its commits,
  beside the live state; the banner says when a checkpoint already copied later commits
  into the main file.
- **A recovery says where it is.** The running step - looking for dropped tables,
  comparing with the live rows of a table, reading the pages - with how far it is, the
  time left in the step and the time elapsed.
- **The window keeps up while the engine works.** A recovery or a row-history scan in
  the background no longer makes scrolling or a tab switch stutter: while you click,
  type or scroll, the interpreter hands over to the window within a fraction of a
  millisecond, and back to full speed for the workers when you only watch.
- **Search results scroll smoothly.** The results list draws only the lines that come
  into view (a wheel step on a full window of results took 100-400 ms before); the
  data grids paint the plain rows' stripes and grid lines as one image (about a sixth
  less work per scroll step).
- **An illustrated guide.** Help › Open the illustrated guide: one page, light and dark,
  with diagrams of how a deleted row comes back, where a database keeps traces, what the
  states and confidences mean, the tabs, reports and the limits.

### Recovery

- A dedicated Recovered Records view: tables overview, records in their table's own columns,
  a record panel with dates read as dates, Copy / Open / Row history; recovery runs by itself
  after opening a small database.
- Recovered: records whose first bytes a freeblock header overwrote (their rowid inferred,
  and said, when the page leaves one possibility or one live row agrees in every column but
  one; at most medium confidence); records cut by a newer, shorter one (partial,
  low confidence); rows written before ALTER TABLE ADD COLUMN; deleted index entries; older
  cells between the live cells of a page; overflow chains past a freelist trunk; UTF-16
  databases; hot rollback journals.
- New states: inserted again (deleted, and the same values are live under another rowid -
  the record names it), older version also for a row replaced through its UNIQUE key
  (INSERT OR REPLACE).
- Fewer false "deleted": a copy of a live row with parts zeroed is a live copy, not an older
  version; a record whose rowid was overwritten is matched to its live row through the
  table's UNIQUE index, also in tables too large to compare in memory.
- Fewer misreadings: a freeblock of a live page is read only as its own table's record;
  sqlite_sequence holds only a name and a number; a guessed end with a blob or broken text in
  a text or number column is not a record.
- Missing row numbers: gaps in a table's rowids, after the last row up to sqlite_sequence,
  cross-checked with the recovered records.
- Files that look encrypted are said to be so - also those whose header carries another
  product's name instead of 'SQLite format 3' (an encrypted database format): an error banner,
  the warning count, and 'encrypted' in the Overview instead of 0 tables and 0 rows.
- Search with 'Recovered records' recovers for at most search_recover_seconds (5 minutes by
  default) per database, shows the pages it is on, keeps its clock running, and says when it
  stopped at the limit (the Forensics tab recovers without it).
- Recovery is faster on large phone databases (a 15 MB, 176-table one: 139 s to 55 s), with
  identical results.

### Browse, Search, Timeline, SQL

- Date columns open as dates when their name and values say so (the stored value kept, the
  choice remembered); exports add a '(date, UTC)' column beside the stored value.
- BLOB pictures as thumbnails in the grid; the BLOB inspector opens a decoded value level by
  level.
- Search covers recovered records (freeblocks, unallocated space, freed and unreferenced
  pages, the rollback journal) when asked, and says what it left out; previews start near the
  match. Every result line says what it is - a live row, a WAL version, a recovered record
  (with its page and offset, tinted) - and the Kind filter uses the same words.
- Fields inside a BLOB / JSON column become new columns of the Browse grid (named ƒ field, remembered per table name,
  in their own colour, said in the status line): sort, filter, copy and export them like any
  column; the stored value stays untouched.
- A recovered record's detail window shows its state, where its bytes are (file, page,
  offset in the page, byte offset in the file, length, WAL frame), why it was read so, every
  value (copy with a right-click or Ctrl+C, view whole, pretty JSON), and the bytes
  themselves in a hex view with the record highlighted; Copy ▾ and Save ▾ give the record as
  JSON, CSV, text or its bytes. Recovered records in Forensics and Browse carry a Byte offset
  column; reports carry the file path and byte offset of every record.
- The Query tab: a column's tooltip shows sample values, a card's menu opens the first rows
  of its table, the column list keeps the actions used most (the rest under More ▾).
- The navigator marks tables whose rows have older versions (⟲ n) once Forensics counted
  them - by itself soon after a database with a WAL or journal opens - and a 'Has history'
  chip lists the databases that have them.
- Every list's header text sits over its values (left-aligned, right-aligned over numbers).
- Identifying facts whole, everywhere: databases with the same file name are named by the
  folders that tell them apart ('History (anu)', 'History (bob)' - never '#2' unless the paths
  are identical); the Overview shows each database's full path and whole SHA-256, hover
  repeats them, right-click copies the hash or the line, and Export ▾ copies or saves every
  database's facts (path, size in bytes, tables, rows, WAL, SHA-256, dates, links, triggers,
  warnings) as CSV, JSON or text; the search scope dialog shows each database's path.
- Links between databases: the same column of a same-named table in another database (ten
  Chrome profiles' meta.key) is a copy of that table, counted as 'the same table in N
  databases' and never drawn as a link.
- The HTML forensic report reads for analysis: the records table leads with the state,
  table, rowid, confidence and a preview; the whole record and its provenance open in the
  drawer; the recovery's numbers are words; audit findings' details are lists.
- Timeline and column filters draw the busy part of the dates, with the far ones counted at
  the ends; 0 and -1 are 'not set', never 1970.
- SQL completion for tables, columns, keywords and functions; result columns that hold dates
  are shown as dates.

### Cases, Overview, Relationships

- A new case lands on the Overview with its largest database active; the date range shown is
  the span most date columns share; links between databases show their progress; a Triggers
  column per database.
- The relationship diagram opens readable; a table's panel lists its triggers.
- A row window opened while the links are still being worked out gets its Related (N)
  buttons once they are known; it opens faster (one window fewer for each line).
- The Limits window groups every limit by area; Help starts with Getting started.

### Fixes

- A date column holding one date in the future (an end time, an expiry) is no longer read
  in another format decades away: the reading with most dates in the past and nearest to
  now wins (a Core Data end time: 2025-2030, not 1994-1999).
- A row window's date says which reading it shows and why ('found for the whole column',
  as the Timeline reads it); 'Read as: 1 other ▾' lists what the same number would mean in
  another format.
- The WAL tab is always there: a database without a -wal file gets a plain statement of
  that, why, which databases of the case have one, and how a WAL is read when there is
  one (the tab no longer comes and goes, and switching the active database no longer
  jumps to the SQL tab).
- Forensics: the record panel's buttons are never cut off in a narrow panel; the facts
  at the top of the Bytes window and the recovered record window (page, byte offset,
  path, state) are selectable and copyable.
- The window no longer freezes for seconds while a recovery builds the live index of a
  very large table (the issue counter waited on the scan's lock); grids reset on a tab
  switch without a layout pass.
- Forensics: with recovered records shown the panes no longer resize in a loop (the
  records grid redrew without end and the tab felt stuck).
- Relationships diagram: Auto-arrange of a large case keeps the cards readable (fitted
  whole, every card was drawn empty); Fit still shows everything.
- Closing the window with a database open asks first, and says the evidence is checked
  once more on closing.
- The window comes to the front when the tool starts (it could appear behind the window
  it was started from, until clicked).
- Row detail: a wide text lost its last letter and a long one its end (a field sized by
  character count); values are measured now and a long one wraps whole.
- Relationships diagram: Auto-arrange packs the blocks of tables beside a tall one
  instead of in rows under it (a third less height on a six-database case).
- Clearer counts and names: the navigator has column headings and a database's total
  waits for the exact count; empty tables say 0, not ~0; values show SQLite type names
  everywhere; Search filters are named like the list's headings; a table card counts
  links and tables; the Scope dialog says what each number counts; the Overview and
  Timeline say what their figures are; Row History explains where a version was seen.
- Many layout fixes at small sizes and high scaling, dark theme contrast (tagged rows, WAL
  windows), and wording that says when a list or result was cut off.
- Searchable dropdown lists close when another program takes the focus (they no longer float
  over it).

### Windows builds

- The portable build is the zip of the program folder; there is no self-extracting single
  file any more (such files, which unpack a Python runtime into a temporary folder on each
  start, are what some virus scanners flag by heuristics). The installer wraps the same
  folder. PyInstaller's start-up code is compiled for each release instead of the shared
  prebuilt one, and no TLS code is bundled (the program never opens a connection).
- The installer and the portable folder carry LICENSE and THIRD-PARTY-NOTICES (the
  bundled software and its licenses).

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
