<p align="center">
  <img src="src/assets/logo_128.png" width="96" height="96" alt="SQLite GUI Analyzer logo">
</p>

<h1 align="center">SQLite GUI Analyzer</h1>

<p align="center">
  <b>Free, open-source SQLite viewer and forensic analyzer.</b><br>
  Search every table, browse millions of rows, decode BLOBs and timestamps, and recover deleted
  records from the WAL, freed pages and the rollback journal — without ever writing to the
  evidence.
</p>

<p align="center">
  <a href="https://github.com/akhil-dara/sqlite-gui-analyzer/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/akhil-dara/sqlite-gui-analyzer?label=download&color=1e40af"></a>
  <a href="https://github.com/akhil-dara/sqlite-gui-analyzer/releases"><img alt="Downloads" src="https://img.shields.io/github/downloads/akhil-dara/sqlite-gui-analyzer/total?color=3b82f6"></a>
  <a href="https://github.com/akhil-dara/sqlite-gui-analyzer/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/akhil-dara/sqlite-gui-analyzer/ci.yml?branch=main&label=tests"></a>
  <img alt="Python 3.8 to 3.14" src="https://img.shields.io/badge/python-3.8%20%E2%80%93%203.14-3776ab">
  <img alt="No dependencies" src="https://img.shields.io/badge/dependencies-none-16a34a">
  <img alt="Windows, macOS, Linux" src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-475569">
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-d97706"></a>
</p>

<p align="center">
  <a href="#download">Download</a> ·
  <a href="#screenshots">Screenshots</a> ·
  <a href="#features">Features</a> ·
  <a href="#run-from-source">Run from source</a> ·
  <a href="#security">Security</a> ·
  <a href="#faq">FAQ</a>
</p>

![SQLite GUI Analyzer browsing a WhatsApp-style message table with a column filter and timestamps shown as dates](docs/screenshots/browse.png)

SQLite GUI Analyzer is a desktop **SQLite database browser and forensic tool** for Windows,
macOS and Linux. Point it at a single `.db` file or at a whole phone extraction folder and it
lists every SQLite database it finds — whatever the file is called (`History`, `msgstore.db`,
`sms.db`, `Cookies`, hash-named backup files) — then lets you search, filter, decode and export
them without writing SQL.

It is built for **digital forensics and incident response (DFIR)**, mobile forensics, CTF
challenges and everyday database work: WhatsApp `msgstore.db` and `wa.db`, Chrome and Firefox
history, iOS `sms.db` and `CallHistory`, Android `mmssms.db` and `contacts2.db`, app caches and
any other SQLite file. One Python file, the standard library only, read-only by design.

## Why people use it

| | |
|---|---|
| **Never touches the evidence** | Opened with SQLite's `immutable` mode, the WAL merged in memory, SHA-256 at open and on close. No copy, no `-shm`, nothing written next to the file. |
| **Finds what other viewers miss** | Rows only in the WAL, overwritten versions, deleted records carved from freed pages, freeblocks and index entries, a table as it was before the journaled transaction. |
| **Fast on huge databases** | 5 GB, 270 tables, millions of rows: scrolling stays instant, search runs in parallel across tables and databases. |
| **Decodes the hard parts** | bplist / NSKeyedArchiver, protobuf, typedstream, gzip, zlib, LZ4, LZFSE, zstd BLOBs; Unix, WebKit, Cocoa, FILETIME and other timestamps with a format you choose. |
| **Many databases as one case** | Open a folder of 30–60 databases: one navigator, one search, a timeline across all of them, links found between databases. |
| **Safe with hostile files** | Every input is treated as crafted by an adversary: fuzzed parsers, size and time budgets, a Safe parse mode where SQLite's C code never reads the file. |
| **No install needed** | Installer or portable exe for Windows; `python sqlite_gui_analyzer.py` anywhere with Python 3.8+. |

---

## Download

Windows 10 / 11 (64-bit) programs are on the [Releases page](https://github.com/akhil-dara/sqlite-gui-analyzer/releases/latest).
They include Python, Tcl/Tk and Pillow, so nothing else needs to be installed.

| File | Use it to |
|------|-----------|
| `SQLiteGUIAnalyzer-<version>-setup.exe` | Install. It asks whether to install for you only (no administrator rights) or for all users, and adds a Start menu entry, an optional desktop icon, optional "Open with" entries for `.db`, `.sqlite`, `.sqlite3` and `.db3` files, and an uninstaller. |
| `SQLiteGUIAnalyzer-<version>-portable.zip` | Run without installing: unzip anywhere (for example a USB drive) and start `SQLiteGUIAnalyzer.exe`. |
| `SQLiteGUIAnalyzer-<version>-portable.exe` | Run without installing, as one file. It unpacks itself to a temporary folder on every start, so it starts more slowly than the zip. |
| `SHA256SUMS.txt` | Check a download: `Get-FileHash <file> -Algorithm SHA256` in PowerShell. |

- Unattended install: `SQLiteGUIAnalyzer-<version>-setup.exe /VERYSILENT /CURRENTUSER`
  (`/ALLUSERS` installs for all users and needs administrator rights).
- The programs are not code-signed, so Windows SmartScreen may ask for confirmation on the first
  start (More info, then Run anyway).
- `SQLiteGUIAnalyzer.exe --self-test --self-test-log check.txt` checks an installation without
  opening a window: exit code 0 means every check passed, and `check.txt` lists them.

On macOS and Linux, [run from source](#run-from-source).

---

## Screenshots

All screenshots show an invented demo case (a chat app, an address book and a browser
history); no real data.

| | |
|---|---|
| ![Search across every table of three databases](docs/screenshots/search.png) **Search** every table of every database at once — one line per row, grouped by database. | ![Deleted records recovered from freed pages and the WAL](docs/screenshots/recovered.png) **Recover deleted records** from freed pages, freeblocks, the WAL and index entries, each with a confidence. |
| ![Row History lists the rows with more than one version](docs/screenshots/row_history.png) **Row History** finds the rows with several versions in every table and shows each version. | ![Timeline with a density chart across three databases](docs/screenshots/timeline.png) **Timeline** of every dated row of every database, with a density chart and named time zones. |
| ![Relationships diagram of the message table](docs/screenshots/diagram.png) **Relationships** found from foreign keys and from the values themselves, drawn as a diagram. | ![Overview of a case of three databases](docs/screenshots/overview.png) **Overview** of a case: date range, biggest tables, links between databases, evidence hashes. |
| ![Row detail with timestamps decoded](docs/screenshots/row_detail.png) **Row detail**: every value selectable, dates decoded, Copy as UTC / Unix / WebKit. | ![Date format chooser with examples](docs/screenshots/date_format.png) **Date formats** shown with your own value as the example, or write your own pattern. |
| ![BLOB inspector showing a decoded bplist as an XML property list](docs/screenshots/blob_inspector.png) **BLOB inspector**: bplist, protobuf and compressed BLOBs decoded, each in the view that suits it (XML plist, protobuf fields, JSON), searchable. | ![Excel-style column filter with value counts](docs/screenshots/column_filter.png) **Column filters** like a spreadsheet: values with counts, conditions, chips, undo. |

<details>
<summary><b>Dark theme</b> (View ▾ › Dark)</summary>

![Browse in the dark theme](docs/screenshots/browse_dark.png)
![Search in the dark theme](docs/screenshots/search_dark.png)
![Timeline in the dark theme](docs/screenshots/timeline_dark.png)
![Relationships diagram in the dark theme](docs/screenshots/diagram_dark.png)
![BLOB inspector in the dark theme](docs/screenshots/blob_inspector_dark.png)

</details>

---

## Features

### Appearance

- **Dark mode**: View ▾ in the toolbar switches the whole app between light and dark,
  instantly; the choice is remembered
- **Interface size**: View ▸ Interface size offers Small (90%), Default (100%), Large (120%)
  and Extra large (140%). The whole UI — fonts, rows, controls — scales live, and the choice
  is remembered for the next start.

### Search Across All Tables

Search your entire database with a single query. Several tables are searched at once and
results stream in live.

- **9 search modes** in three groups, each under its heading in the mode menu: text
  (Case-Insensitive, Case-Sensitive, Exact Match, Starts With, Ends With, Regex), binary (Text
  in BLOBs, Byte pattern (hex)) and schema (Column Name)
- **One line per row**: a row matching in several columns is listed once ("Matched in: a, b"),
  with its matching cells underneath; untick the option to list every cell
- Per-table row counts; filter results by database (in a case), source (DB / WAL / Freed
  pages), table, column or data type
- Regex engine with automatic LIKE pre-filtering for fast pattern matching
- Max rows/table (100 / 500 / 1,000 / 5,000 / All): one matching row more is read, so only a
  table that had more is named in the status ("stopped at 100 matching rows … urls") and marked
  "(100+)" in the Table filter; All has no limit. The status says "Complete", or "Stopped after
  3 of 12 tables" after Stop
- Search scope -- restrict to specific tables (and views, listed apart); one Scope dialog for
  one database or, in a case, each database's tables under its name
- **Include views** (off by default, since views repeat rows of their tables): views are
  searched too, their lines are marked "(view)" and open the matched view row in its row detail
- **Include WAL row versions** and **Include freed pages** (offered only when a searched
  database has them) search those with the same matching rules: a row version copied into many WAL frames
  is one result listing all its frames, and a WAL copy identical to a matching database row is
  shown on that row's line ("DB + WAL x65") instead of being repeated
- Export ▾: Matches (CSV or JSON)…, Matches with their whole rows (JSON)… -- with the search's
  term, mode, limit and filters in the provenance (see Exports) -- or Copy results
- Pagination under the results; "N errors" appears only when a table could not be searched

### Several Databases (a Case)

An extraction usually holds many databases that belong together (a phone's WhatsApp, PhonePe,
contacts, SMS and Chrome databases...). **Open ▾ › Open folder…** lists every file of a folder
that starts with the SQLite header (size, WAL / journal, relative path; all ticked, a filter,
Select all / none, optionally the subfolders); **Open ▾ › Add database(s)…** adds files from
any folders. The workspace stays clear with 30, 60 databases:

- Each database is opened exactly like a single one: read-only, immutable, never copied, its own
  WAL overlay and SHA-256. The databases open one after the other on a worker thread, so the
  window never stops responding: each joins the navigator as soon as it is open and the first
  is shown at once; when opening takes more than half a second a small window says which
  database is opening ("Opening wa.db (12 of 35)…") with **Stop** (the ones already open stay,
  and the message says how many were not opened). Sixty databases open in about a second.
- **A one-line header** whatever the number of databases: the case name and summary
  ("com.phonepe.app · 16 databases · 1.2 GB · 253 tables · active: accounts_db"), one evidence
  chip ("16 read-only · 8 WAL merged") and a warnings chip only when there are warnings (a hot
  journal, a WAL SQL cannot see); click either for the status of every database.
- **The Case navigator** (left, Ctrl+B hides it) groups the databases by app or folder
  ("com.whatsapp (20)"; many groups are folded, Pinned first), each with its colour dot, status
  (WAL merged / not in SQL / warning), tables and rows, and a hover card with the path, size and
  SHA-256. One search box finds databases, tables and columns of every database as you type;
  chips keep those with rows, a WAL, dates, hits of the last search or warnings; Sort ▾ orders
  them by name, size, rows, hits or last use. Click a table to browse it; select several
  databases to use them as the scope; right-click for Make active, Open in Browse, Pin, Show
  in folder, Copy path, Hash details…, Remove from case.
- **The Overview tab**, the landing page of a case: cards for the dates found, the biggest
  tables, the links between the databases and the warnings, and a sortable, filterable table of
  every database (app, size, tables, rows, WAL, SHA-256, date range, links in and out), filled in
  the background database after database.
- **The active database**: Browse, WAL, SQL and Forensics work on one database and say which in a
  breadcrumb bar ("● accounts_db ▾ › account ▾"); its dropdown, the navigator (double-click) or
  the command palette (Ctrl+K) make another active. Tabs keep plain names.
- **Removing a database** stops only the work that reads it (a search or timeline job over it,
  its exports), verifies it like a close and removes its results; the other databases' search
  results, timeline events and running exports stay, and the search status says whose results
  went. Switching the active database says in each tab (SQL, WAL, every Forensics sub-tab) that
  its results were cleared.
- **One scope control**: Search, Timeline, Relationships, Find everywhere and the Database Map
  each have the same button ("All 16 databases ▾" / "3 of 16 databases ▾"), whose popover
  groups the databases with tri-state checks, searches them, and offers presets (All, Only
  active, Selected in navigator, With hits, With dates) and saved scopes ("Messaging DBs"). The
  scope is chosen once per case and remembered; a feature can have its own choice until
  "Follow global scope".
- **Search** says where it will look before it starts ("Will search 3 of 16 databases · 312
  tables") and, after it, in one line ("Complete: … · 2 databases with matches"), with Details
  listing each database with matches and one line for the rest ("14 databases: searched,
  nothing found"). Results say their database ("wa.db · DB"), can be filtered by database, and
  open in their own database; the Scope dialog lists the tables under their database.
- **Links between databases** are found by their values (identifier-like text: jids, URLs,
  hashes, e-mail addresses) with sampled, batched lookups, and are always labelled "matched by
  value". The Relationships tab lists and draws them (orange, short dashes) with a Database
  column and an "Only links across databases" filter; Related rows splits "In this database"
  and "In other databases"; Find this value everywhere searches every database of the search.
- **Show value from linked table…** (column header menu, only for a column with a confident
  link): each raw value is shown with the linked row's column, e.g. a jid with the contact's
  display name, as `raw → value (wa.db › wa_contacts.display_name)`. Sorting, filters and copies
  keep the raw value; exports add the looked-up value as a separate column.
- The **Timeline** merges the events of the searched databases (a Database column), and the
  **Tagged** tab lists the tagged rows of every database (each database keeps its own tag
  file).
- The case is saved in the app-data folder and listed under **Recent**; reopening it checks
  that no file changed (size and time at once, SHA-256 once hashed) and says which did. Open and
  Recent ask before they replace an open case of several databases.
- Limits (databases per case, databases searched side by side, sample sizes, rows read) are
  named settings with defaults; settings.json `{"limits": {"case_search_parallel": 2}}` changes
  them, a bad value keeps the default and is listed under Issues.

### Browse Tables

A virtual grid scrolls through any table -- 250 columns or millions of rows -- drawing only
the cells in view and reading rows in the background as you scroll. Rows never go blank:
while the rows at a new position are read (after a scrollbar drag, a sort or a filter) the
grid keeps showing the rows drawn last, and the status line already shows the new position.

- Scrolling is instant anywhere, also sorted and filtered: see [Performance](#performance)
- Long text values show their size in the cell (`↵ 42 lines · 12,345 chars`) and their first
  lines in the tooltip; **View value** (`Shift+Enter` or right-click) opens the whole value with
  find (`Ctrl+F`, match count), wrap, line numbers, JSON / XML pretty-printing and copy

- Click column headers to sort; drag header borders to resize, double-click to fit
- Drag a header to move the column (or right-click it: Move left / Move right); the row
  locator column (`_rid`) stays put and stays in view while scrolling sideways
- Right-click a header for **Wrap cell text**: long values wrap to three lines instead of
  one, with taller rows
- **Column filters like a spreadsheet** (every grid: Browse, SQL, WAL, Forensics, Timeline,
  Tagged): a funnel in each header opens the column's filter -- sort A→Z / Z→A, the values
  with their counts (searchable, Select all / none, "(Blanks)", the top values with bars,
  read in the background on millions of rows; it says when a limit cut the list),
  conditions that suit the column (text: contains, starts / ends with, equals, regex, empty;
  numbers: comparisons, between, top / bottom N; dates in any detected epoch: before, after,
  between, on a day, last N days, this month, with a calendar and a histogram to drag across;
  a few values as chips), any number of conditions with AND / OR per condition, and the rows
  kept counted before you apply. The filters in force are chips above the grid ("status = 3 ×") with the rows kept
  ("1,204 of 2,460,000 rows"), Clear all, Back / Forward (Ctrl+Z / Ctrl+Y), Save filter… and
  Saved ▾ (per table name, so they apply to the same table of another database), Copy as SQL
  WHERE and Copy as filter text. A filter that leaves no row offers to remove the last
  condition. Every filter is evaluated by SQLite or by the native reader with the same
  result. A filter row under every header takes the same language (shown by default; toggle it
  with the funnel button above the grid). In the filter popover, the "filter expression"
  operator has a **?** button with the syntax and examples:

  | Filter | Keeps values that |
  |---|---|
  | `text` / `!text` | contain / do not contain the text (any case) |
  | `^=text` / `$=text` | start / end with the text (any case) |
  | `>5` `>=5` `<5` `<=5` `=x` `<>x` | compare as a number (or as text: `="10"`, as a BLOB: `=x'00ff'`) |
  | `5~10` | lie in the inclusive range |
  | `IN (a, "b c", 5)` / `NOT IN (…)` | are (not) one of the values |
  | `a%b_c` | match the LIKE pattern (`%` any text, `_` one character) |
  | `/regex/`, `/regex/i` | match the regular expression |
  | `NULL`, `NOT NULL`, `""`, `EMPTY` | are NULL, not NULL, the empty text, NULL or empty |
  | `{a} AND {b}`, `{a} OR {b}` | match both / either condition |

  Filters match the values actually stored, not the column's declared type: `5` and `"5"` stay
  different, and a NULL in a NOT NULL column (damaged or recovered rows) is still found by
  `NULL`. SQLite itself does not enforce column types, so neither do the filters.

  Values of different storage classes compare in SQLite's order (NULL < numbers < text <
  BLOB); an expression that cannot be used is marked red with the reason.
- **Filter all columns** box: every word must occur in some column (`"quoted words"` count
  as one), the words found highlighted in the cells; `?` beside it shows the syntax
- Right-click: copy cell or rows (TSV / CSV / JSON), filter to or exclude this value (its day
  or hour for a date, several cells' values), Inspect BLOB…,
  View value…, hide columns, Row history, Related rows, Find this value everywhere, Copy with
  related, Tag (only what applies to the cell is offered, never a greyed-out item); a column
  chooser shows hidden columns again. The frozen row-id column cannot be hidden, so its header
  menu does not offer it
- Row panel: every column of the current row, with timestamp decoding
- **Export ▾**: Rows (CSV or JSON)… (rows the filters keep, or the selected rows) and BLOBs as
  files… (offered when the table holds BLOB values, in any column: a BLOB stored in a TEXT column
  is found by a check in the background) run on a worker thread with progress and Stop
- A jump far into a view sorted by a column without an index says which rows it reads and for
  how long; once the position index is built, the read is restarted through it
- **Show as date**: right-click a column header to show its numbers (or date text) as UTC
  dates -- Auto (the timeline's detector suggests the format), Unix s / ms / µs / ns, Cocoa,
  WebKit, FILETIME, HFS+, .NET, OLE, GPS or Off. Sorting, filters, copies of rows and exports
  keep the stored values; the tooltip, the row panel and 'Copy raw value' give them. The
  choice is kept per table
- Damaged and pre-ALTER rows are tinted and explained above the grid
- CSV / JSON export of every row passing the filters; bulk BLOB export
- WAL-only tables accessible from the Browse dropdown ("WAL: name"): read on a worker, with the
  values as stored (NULL stays NULL, a REAL keeps every digit, a BLOB its bytes, so Inspect BLOB
  and the exports work on them)

### Relationships

"Where else does this value live?" -- without hunting through hundreds of tables by hand.

![Rows of other tables related to one chat](docs/screenshots/related_rows.png)

- **Mapped in the background** when a database opens: declared foreign keys; names such as
  `message_row_id` / `chat_id` / `messageid` -> table `message` / `chat` on its
  `INTEGER PRIMARY KEY`, `_id`, primary key or rowid, `sender_jid_row_id` -> `jid`, a number
  column named after a table (`visits.url` -> `urls`); the same id-like column name; columns
  that name the same key. Each link is checked against the data (up to 200 distinct values
  looked up) and trusted only when the values agree, so a misleading name or a 0/1 flag column
  never shows up as a relation
- **Related rows** in the cell right-click menu, only when related tables hold that value:
  "message (_id) -- 1 row", "message_poll_option (message_row_id) -- 3 rows"; a `Related (n)`
  button in the row detail (counted in the background). The Related window lists them strongest
  first with the reason in plain words ("declared foreign key", "same values found in 97% of
  samples") and the rows below (row detail, Open in Browse with the filter set, Tag all rows
  found). Linked columns get a chain mark in their Browse header. A check that read only part
  of a column says so, with the limit's name
- **Find this value everywhere** (and **Find inside other values** for text and BLOBs): the
  value in every column of every table, in WAL row versions and in freed-page records; a number
  also matches the same number stored as text or bytes, a BLOB its bytes inside larger values.
  Known links come first, other hits are marked "same value found", and common values (small
  numbers, short words) carry a coincidence warning
- **Relationships tab**: one live search field for three views. *Tables*: the tables with
  links and the selected table's card: "Refers to" / "Referred by" (tables linked alike
  grouped: "142 tables via message_row_id → _id"), strength (Declared / Strong / Likely),
  values found, rows, and a collapsed "Weaker links (N)". *Diagram*: an entity-relationship
  diagram of the selected table and its neighbours (or the whole database): each table a card
  with all its columns, types and PK / FK markers; connectors from the exact column to the
  exact column with their cardinality (1 / many, from the schema or the checked values);
  several relationships between two tables drawn apart, self-references as loops, junction
  tables optionally collapsed to N:M lines, hubs grouped into a card listing their tables
  (limit `diagram_group_min`); drag cards (kept per database), Tidy, Auto-arrange with few
  crossings (barycenter sweeps, then neighbouring cards swapped wherever that removes
  crossings), zoom, pan, Fit and a minimap; hover follows a connector, a click opens its side
  panel with the evidence, the cardinality, a sample JOIN and Browse both sides. The diagram
  draws the trusted links (weaker ones are listed, not drawn) and is drawn when its tab is
  shown.
  *All links*: the full list. Tables without rows are hidden unless asked for (their links
  cannot be checked by values). Export ▾: Links listed (CSV)…, Diagram (SVG, every group
  listed)…, Database Map… (each with a manifest, noted in the activity log)
- **Column relationships** window (column header or the navigator): every related column of
  one column, the weaker matches in a collapsed section
- Fast on large app databases: on a 270-table messaging database the schema map builds in
  0.03 s, the values of all its links are checked in about 10 s in the background, a cell's
  Related rows menu is counted in 0.03 s and the related rows of one value come back in well
  under a second; SQL-served and natively read tables give the same rows

### Copy with related and the Database Map

Everything someone writing a reader for the database needs, in one place.

- **Copy with related** (Browse or search result right-click, the row detail, the Related window):
  the row, the selected rows or all rows a Browse filter keeps, with every row a trusted link
  leads to (one or two links deep). Each related row says the link it came through
  (`visits.url → urls.id, matched 100%`); the CREATE statements of the tables come along, dates
  are converted beside the raw values and BLOBs are described (bytes as hex on request).
  Markdown, JSON (`{table, row, columns, values, related: [{via, table, rows}]}`) or SQL (the
  JOIN queries that return exactly those rows, with date conversions such as
  `datetime(x / 1000000 - 11644473600, 'unixepoch')`). The window says how many rows and links
  are involved before anything long runs and previews the first rows; **Copy** fills the
  clipboard, **Export…** streams any number of rows to a file with progress and Stop. The output
  names the tool and version and each database's SHA-256, and an export gets a manifest and an
  activity-log entry
- **Database Map…** (Database ▾ menu and Relationships tab) writes
  `<database> - Database Map.html` (one self-contained, printable file with contents, collapsible
  sections and a search box), or Markdown or JSON: every table (columns, declared types and
  affinity, keys, row counts, WITHOUT ROWID, the storage classes found), the links with their
  evidence (weaker ones apart, on request), date columns with their kind and an example
  converted, BLOB columns with what their values decode to, a JOIN query per linked table, the
  diagram and the evidence hashes. Sample rows only when asked. The map gets a manifest and an
  activity-log entry
- **In a case of several databases** Copy with related also follows the links between databases
  (matched by value), naming the database of every row, and its SQL runs each query in its own
  database; the Database Map covers the active database or the whole case (every database's
  sections, the links between databases and one diagram in the databases' colours)
- Built for large databases: related rows are looked up in batches through indexes, capped per
  link and row with the rest counted ("+N more, not included"), and a link that would need a
  full scan of a huge unindexed table is left out and said so. On a 3.5 GB, 270-table messaging
  database the map takes about 15 s (HTML 1.7 MB), Copy with related of 1,000 messages about
  3 s, and a 278,000-message filter streams to a file at about 1,800 rows a second
- Every cap is a named limit with a default (see **Limits…**, saved in the settings); whatever a
  limit leaves out is said where it happens, with the limit's name

### Tags and reports

Mark the rows that matter and report on them.

- **Tag rows** from the Browse grid or the search results: right-click > Tag, or `Ctrl+T` (the
  first tag, "Relevant") and `Ctrl+1`..`Ctrl+9` on the selected rows. Default tags are Relevant,
  Review, Suspicious and Not relevant; add, rename, recolour, delete and reorder them with
  Manage tags. Each tagged row can carry a note
- **Tag many rows at once**: every row the Browse filters keep, or every search result the
  result filters keep (in the background, with progress and Stop)
- Tagged rows show their tag's colour in the grid (a marker keeps damaged-row tints visible) and
  a dot in the search results
- **Tagged tab**: every tagged row with its tags, note, table, row id and where it was found
  (database, WAL frame and its state, freed page and cell offset), in the same virtual grid as
  Browse, so tens of thousands of tagged rows scroll smoothly; each row shows its tag's colour.
  Filter by tag, by text or with the column filters, sort by any column, double-click to open
  the row; edit notes, remove tags or export the selected or listed rows
- **Exports** of all tagged rows, the rows shown, or one tag, each stating the tool version, the
  export time (UTC) and the evidence files with their SHA-256 hashes:
  - HTML report: one printable file, grouped by tag then table (or by table), BLOBs summarised
    with small images embedded, everything escaped and nothing loaded from outside
  - CSV folder: `index.csv`, one CSV per table, `export_info.csv` and the BLOBs as files in
    `blobs/` (UTF-8 with BOM; existing files are never replaced)
  - JSON: the tags with the rows' values, loadable again with Load tags
- Each row is kept as it was when tagged, so a report still shows it if the row later changes
  or disappears
- **Saved per database** in your application data folder (`%APPDATA%\SQLite GUI Analyzer` on
  Windows, `~/.local/share/sqlite-gui-analyzer` elsewhere), never next to the evidence: tags,
  notes and the Browse layout (column widths, hidden columns, sort, last table). A warning is
  shown when the database changed since its tags were saved. Save tags as… / Load tags…
  exchange tag files; Open ▾ › Recent reopens a database. The Tagged tab's status says the
  tags are saved in the app-data folder (hover it for the file). Tagging, untagging, notes and
  Tagged exports are noted in the activity log

### Storage

Where the app keeps its data, and how to change it (View > Storage...).

- **Folder**: `%APPDATA%\SQLite GUI Analyzer` on Windows,
  `~/.local/share/sqlite-gui-analyzer` elsewhere. **Change folder...** moves it anywhere you
  like (a bigger drive, for example): it offers to move your tags and settings along, and the
  change takes effect after a restart. (The `SGA_DATA_DIR` environment variable does the same
  for scripts.)
- **What is stored**: `settings.json` (recent databases and cases, theme, window state);
  `cases/<database>-<id>.json` (your tags and saved view state -- filters, column order, date
  formats -- per database); `case-lists/` (multi-database case definitions).
- **Recent items**: how many databases and cases Open > Recent keeps (0-50, default 10).
- **Evidence safety**: nothing is ever written into your evidence databases. Tags, settings and
  view state live only in the app-data folder above. Backing up that folder backs up everything.

### Case Navigator and Command Palette

The panel on the left lists the open database (or the databases of a case, grouped by app or
folder) with its tables and their row counts.

- Expand a database for its tables (then views, triggers and WAL-only tables), a table for its
  columns with types, foreign keys, indexes and CHECK constraints; click a table to browse it
- One search box finds databases, tables and columns of every open database as you type (the
  same search field as every list of the tool: grey hint, ×, "N of M", Enter for the next match)
- Right-click: browse, copy CREATE SQL / name, search this table, column relationships; for a
  database Make active, Pin, Show in folder, Copy path, Hash details…, Remove from case
- The selected table's CREATE statement folds out at the bottom, with Copy CREATE / Copy schema
- **Ctrl+K** opens the command palette: every database, table and column, the tabs and the
  actions (Build timeline, Export Database Map…, Find value…), ranked as you type
  ("msgdb" finds msgstore.db), recent choices first
- Every dropdown of the tool can be searched by typing, and every date field has a calendar
  and time picker with validation and presets; an open list or calendar closes when you click
  or move to another field, and stays open while you switch to another program
- Every list you scroll has a Find field (Ctrl+F comes to it), also the row details, the WAL
  per-table statistics and the BLOB Inspector's timestamp readings
- The window keeps responding while work runs: on a case of 35 databases, opening and closing
  it, the grid's first draw of a wide table (cell text is cut to fit by summed character
  widths; emoji and other scripts are measured a few at a time, and a redraw past 50 ms goes
  on at the next turn), the Limits window and a search's start and end each hold it under
  100 ms, and a first switch to a tab 25-115 ms (Tk drawing the tab); background work (link
  checks, row counts, searches) gives way whenever the window is busy
- Laid out for windows from 900×600 up to the screen at 100-200 % display scaling (checked by
  the tests at each size, nothing cut off); the smallest window size grows with the scaling
  but never beyond the screen. Sizes larger than the screen cannot be checked: Tk keeps a
  window within the screen
- **Interactive HTML schema report** (Database ▾ > Schema report) -- with live search, copy
  buttons, constraint badges, and print layout

### WAL Forensic Analysis

Dedicated tab for analyzing SQLite Write-Ahead Log (WAL) files at the binary level. Recovers data that standard SQLite tools cannot see.

- The **WAL** tab appears right after Browse for every database with a `-wal` file -- also one
  whose header cannot be read: the tab then says why, and that its frames are not applied
- **Pure binary WAL parser** using memory-mapped I/O -- reads data that standard SQLite APIs hide
- **Checksum-verified replay** -- frames are validated exactly like SQLite's own recovery (salts + running checksum)
- **Frame states**: Current (committed, latest), Superseded (older committed version), Uncommitted (in progress / rolled back), Stale (earlier WAL generation)
- **Frames** view with filters (status, table, page type, page number -- a wrong page number is
  marked) and sortable columns; each frame shows its **transaction** (the commit it belongs to)
  and the records on its page (counted in the background, "…" until then); selecting one shows
  its summary, its records (double-click, tag) and its bytes (hex view)
- **Records** view -- every record of the WAL frames compared with the database's current row
  on a worker thread (progress, Stop): `✓` same, `≠` different (which columns), `∅` not in the
  database, `★` WAL-only table, `?` could not compare (with the reason in the Note column: a
  read error is never reported as "not in DB"). Records of several tables share one "Values"
  column; choose a table to see its own columns. Changing the Table or Status filter after a
  comparison filters the compared records at once, and asking for records that were not
  compared says to compare again
- **Row detail** of a WAL record: each column's value in the WAL beside the database's current
  value; BLOBs open in the BLOB Inspector; copy as JSON (lossless)
- **Per-table statistics**, **Technical details** (the WAL header), **Export ▾**: Frames
  (CSV/JSON)…, Records (CSV/JSON)… (the records listed with their comparison, or every record,
  then marked "not compared"), BLOBs as files…, with provenance

### Forensics Tab

- **Recovered Records**: rows recovered from freeblocks and unallocated space inside pages,
  freed pages, older page copies in the WAL, replaced pages and the rollback journal. Records
  whose first bytes were overwritten are rebuilt from the table's schema; rows identical to a
  live row are left out. Every record carries its provenance (file, page, offset, WAL frame and
  state) and a confidence (high / medium / low) with the reasons. A recovery stopped early says
  why (Stop, the time limit, or the limit `carve_max_records`); a table or index too large to
  compare with its live rows (limits `live_hash_rows`, `live_index_entries`) is named in the
  status, since copies of live rows are then not told apart there
- **Freed Pages** (only for a database with freed pages): the pages of the freelist with the
  records still on them -- read by the same carver, so with the same confidence and reasons as
  Recovered Records and Search's "Include freed pages" -- and each page's bytes
- **Deleted index entries**: entries of ordinary indexes (plain, composite, DESC, expression,
  UNIQUE and partial indexes, and the automatic indexes of UNIQUE / PRIMARY KEY constraints) are
  carved from the same places and attributed to their index and table: the indexed values and
  the rowid, often still there after the row itself was overwritten. Live entries are left out;
  each entry says whether its row was also recovered as a table record, is live with other
  values, or left no other trace
- **Row History**: every version of a row across the main file, WAL frames and the journal,
  with the changed columns, deletions and reused row ids (and a note when the main file could
  not be read, never taken for "not there"); Find changed rows lists the rows that changed,
  were deleted or reused
- **Dropped Tables**: CREATE statements of dropped tables recovered from page 1 and its older
  copies, with their rows when the pages are still intact
- **Rollback Journal** (only for a database with a `-journal` file): a table as it was before an
  unfinished (hot) transaction; page checksums verified; a journal cut at the limit
  `journal_records` says so
- **Audit**: reserved bytes, header versus actual page and freelist counts, zeroed free pages
  (secure delete), WAL salts and stale frames, and other anomalies
- **Export ▾**: Forensic report (everything found so far)… or Recovered records listed…, as
  HTML, CSV or JSON naming SQLite GUI Analyzer, its version and the Python and SQLite versions,
  with the evidence SHA-256 hashes, findings, recovered records and dropped tables, a manifest
  and an activity-log entry; refused inside the evidence folder
- A job that fails says why in its sub-tab and is logged under Issues; switching the active
  database says in each sub-tab that its results were cleared

Tested against deleted-record test sets with known answers: every row still physically present
in the file was recovered, with no false positives.

### Timeline

Every dated row of the database in time order.

- **Date columns found for you**: by name (time, date, timestamp, `_ts`, created, modified,
  last_visit, expires, sent, received ...) and by sampled values, which must read as plausible
  dates (1990-2040) in one format: Unix s / ms / µs / ns, Cocoa s / ns, WebKit / Chrome µs,
  FILETIME, HFS+, .NET ticks, OLE days, or ISO 8601 / RFC 2822 text. Columns named like ids,
  counts, sizes or phone numbers are never taken, nor values that repeat like flags or step
  like counters. Each column shows its format, a confidence and the reason; untick it, or
  right-click to read it as another format (kept per database). Hovering a column shows
  sample raw values with how they read as dates, so a wrong guess is spotted before Build
- **Events**: time (UTC, plus a second column when a zone is picked in "Times in"), table, column, format, row,
  source and a few describing columns of the row; double-click opens the row, right-click tags
  it. Selecting an event shows it in the **preview pane** under the grid (its time, source,
  row, description and raw value; collapsible)
- **Time display**: the Time menu switches the timestamps between ISO 8601 (24-hour),
  12-hour, Excel-style (`02-Oct-2026 14:30:45.123`), US-style (`10/02/2026 02:30:45.123 PM`)
  and a custom strftime format (with a live preview); the choice is kept. The same formats
  are offered by Browse's "Show as date" (right-click a column header > Date display)
- **Large databases**: only the needed columns are read, in SQL where SQLite serves the table
  (natively otherwise), newest first up to "Max events per column" (limit
  `timeline_column_events`); sorting and filtering a million events run on a worker thread; a
  From / To range is turned into
  each column's raw numbers so SQLite reads only that range. On a messaging database with 269
  tables and millions of rows, detection takes under a second
- Optionally the older row versions and deleted rows still in the WAL, and the records the
  Forensics tab recovered, each marked by source (Options ▾)
- **A top bar** with the scope (in a case), a date range picker (calendar and time; presets
  last hour / 24 h / 7 days / 30 days / this month counted back from the newest date found),
  "Times in" (UTC is the forensic default; picking a zone adds a second column with the time
  in that zone -- display only, the data is always read as UTC) and Build
- **A density chart** above the events: bars per stretch of time (in the databases' colours in
  a case); hover for the count, drag across it to keep only that range in the grid
- **In a case**, the date columns are grouped by database (a tri-state tick per database, its
  count; the databases without date columns in one line), the status is one line whose
  Details list each database's events, every event row is marked in its database's colour,
  and the date columns the Overview already found are reused. Removing a database drops only
  its events and date columns; the others stay

- Export ▾ writes the shown events as HTML, JSON or CSV with the one export writer (the evidence
  SHA-256 waited for, every note of the build, Stop, a manifest, the same final message as every
  export; never into the evidence folder)

### Row detail

Double-click any row for a detailed view (a table row, a query result, a WAL record or a
recovered record -- each says where it comes from). A table row's window is titled "Row
detail — table, row N (database)" and its first line names the database, how it was opened,
the table and the row.

- All values displayed with full column names (no truncation)
- **Find** at the top (Ctrl+F): typing lists only the columns whose name or value holds every
  word ("12 of 250 columns match"), Enter goes to the next one and marks its name; it looks at
  up to 100,000 characters of a long text and a BLOB's summary and first 512 bytes as hex. The
  row detail of a query result, a WAL record and a recovered record have the same Find
- `Related (n)` beside a value other tables hold (counted in the background, so a large table
  never holds the window up); right-click a value for Related rows and Find this value
  everywhere; text that is not valid in the database's encoding has Inspect bytes…
- Multi-line text in scrollable widget with word wrapping
- Each BLOB is summarised ("bplist: NSKeyedArchiver NSDictionary (12 keys)", "gzip → protobuf
  (5 fields)", "PNG 640×480") and opens in the BLOB Inspector
- Copy row as JSON, CSV, or plain text -- every value whole (BLOBs as hex)
- Search result highlighting -- scrolls to and highlights the matched column

### BLOB Inspector

- **Decoded tree** of binary and XML property lists, keyed archives (NSKeyedArchiver: `$objects`
  and UIDs resolved into dictionaries, arrays, strings, dates and class names), protobuf without
  a schema, typedstream message text (e.g. `attributedBody`), JSON, base64, UTF-8 / UTF-16 text
  and images
- **Nested decoding**: compressed layers (gzip, zlib, deflate, bz2, xz / lzma, and zstd with the
  tool's own decoder on every Python) and data inside data (NSData holding a plist, protobuf
  bytes fields, base64 in JSON) are decoded again, level by level, with named work limits
  (`decode_*`, `summary_*`, `decode_lzma_memory`) so hostile data cannot hang the window or
  exhaust memory; the decode note names the limit it reached
- **LZ4 and Apple compression**: BLOBs compressed with LZ4 (the frame format, its checksums
  verified) or with Apple's LZFSE, LZVN and LZ4 containers (`bvx2`, `bvx1`, `bvxn`, `bvx-`,
  `bv41`, `bv4-`) are decompressed in pure Python and the output is decoded like any other
  BLOB, so an LZFSE-compressed property list shows its contents. Output is capped per stream
  (64 MiB), and damaged, truncated or checksum-failing streams are shown as uncertain with a
  note, never as an error
- **Views by format**: the decoded value opens as an XML property list for plists, as fields
  (`1: 150`, `2: "text"`) for protobuf, as JSON for JSON and archives and as text for text;
  JSON, XML plist, Protobuf and Text can be picked, and XML is offered only for plists
- **Export BLOBs as files** (Browse › Export): as stored, decoded as JSON, or plists as XML
  property lists (`.xml.plist`), optionally with the original bytes beside each, and a
  manifest with every file's SHA-256. The decoded forms are offered only when the table's
  first rows hold BLOBs they apply to, and the dialog says what it found there
- **Linked hex view**: selecting a value highlights its bytes in the buffer it lives in (the
  BLOB, or a decompressed layer); clicking a byte selects the value stored there. Scrolls
  smoothly through BLOBs of any size; go to offset; find
- **Interpretations** ranked confident / uncertain, with the reason each other format was ruled
  out
- **Timestamp** tab: a selected number read in every common epoch, plausible dates marked
- Image preview with zoom (Fit / 100% / Zoom+ / Zoom- / mouse wheel); the size is read from the
  image header first and nothing larger than `preview_pixels` is drawn (the pane says why)
- Save BLOB… (extension from its content) or Save decoded JSON… (each with a manifest and an
  activity-log entry); copy value, hex, base64

### Timestamp Decoding

One decoder serves the whole tool (the row detail, the BLOB Inspector, Browse's Show as date and
the Timeline), always in UTC and with the same names; plausible dates fall in 1990-2040. Show as
date › Auto reads a sample of the column in the background ("reading a sample…" meanwhile):

- Unix seconds, milliseconds, microseconds, nanoseconds
- Cocoa / Mac absolute time (seconds and nanoseconds since 2001-01-01)
- WebKit / Chrome (microseconds since 1601-01-01)
- Windows FILETIME (100 ns intervals since 1601-01-01)
- HFS+ (seconds since 1904-01-01), .NET ticks, OLE Automation dates
- GPS time (seconds since 1980-01-06)

### Exports and provenance

Every export -- Browse, Search, SQL, WAL frames and records, BLOB folders, the Timeline, the
Relationships links and diagram, tagged rows, the forensic report, Copy with related, the
Database Map, the schema report and BLOB Inspector saves -- states the following (the row
exports run on a worker thread with progress and Stop):

- the tool and version, the export time (UTC), the Python and SQLite versions;
- each database's evidence files with size, modification time (UTC) and SHA-256 (waited for
  or computed first);
- what was exported, the scope and the filters (a search's term, mode and limit; a query's SQL
  and whether SQL saw the WAL), the number of rows and whether the export is complete.

- **Tables (CSV)** (Browse › Export): tick any tables to export each as its own CSV in a
  folder, with the delimiter (comma, semicolon, tab, pipe), the encoding (UTF-8 with BOM for
  Excel, UTF-8, Windows-1252), BLOB handling and spreadsheet-safety chosen up front. Runs on
  a worker thread with progress and Stop; one manifest covers the folder.

**HTML reports** share one design and stay fast with hundreds of thousands of rows: a
self-contained file (no external fonts, scripts or links; works offline and on a phone) with
a cover (case, evidence files with SHA-256, tool version, UTC time, scope and filters), a
summary dashboard (counts, an activity chart over time, top values), contents with a search
across the report and deep links, and tables drawn as you scroll (rows embedded as JSON
chunks, written as the export streams): sort by several columns, column filters and value
checklists, search with highlights (also inside decoded BLOBs), show / hide / pin columns,
wrap text, double-click a column edge to fit it, a row drawer with every value, dates in
UTC, local time or a named time zone, each BLOB's decoded value (XML plist, protobuf fields
or JSON) and image thumbnails, a BLOB inspector (Inspect…: decoded value, hex pages and the
text inside, Find by text or hex, Save BLOB), copy cell / row, download of the rows
shown as CSV or JSON, the view kept in the link, light / dark, compact rows, keyboard
shortcuts (`?`), print. Without JavaScript the summary and the first rows still read as a
plain page. Above `html_rows_per_part` rows a table continues in part files listed in the
report and the manifest.

CSV files hold only the header and the rows; the provenance goes into `<file>.manifest.json`
next to it, with the SHA-256 and size of the export itself. JSON exports carry it inside
(`{"provenance", "columns", "rows", "end"}`) and get the manifest too; a BLOB folder gets
`export_manifest.json` with every file's SHA-256; an HTML export (the Timeline's) carries it in
its head. A stopped export is kept and marked incomplete ("stopped by the user after 52,001
rows"). Every export ends with one message saying what was written, where and its manifest --
or that the manifest could not be written, which is also logged under Issues -- and is noted in
the activity log. Nothing replaces an existing file, and nothing is written into an evidence
folder (also not through a junction, a substituted drive or a network alias of it).

Values are written the same way everywhere:

| Value | CSV | JSON |
|---|---|---|
| NULL | `NULL` (use JSON to tell it from the text 'NULL') | `null` |
| INTEGER | digits | number |
| REAL | `repr()`, exact | number (`{"real": "inf"}` when not finite) |
| TEXT | as stored, no truncation; spreadsheet-safe (see below); NUL as `\x00` | string |
| invalid text | valid parts as text, bad bytes as `\xNN` | `{"invalid_text_hex": …}` |
| BLOB, hex (default, lossless) | `x'…'` | `{"blob_hex": …, "size": n}` |
| BLOB, base64 (lossless) | `base64:…` | `{"blob_base64": …, "size": n}` |
| BLOB, summary | `[BLOB n bytes, SHA-256 …]` | `{"blob_summary", "size", "sha256"}` |
| several values in one cell (a record's values) | the JSON of them, each as in JSON | a list (or object) of the values |

A text that reads like a BLOB (`x'00ff'`, `base64:…`) looks the same as that BLOB in CSV; JSON
tells them apart.

**Spreadsheet-safe CSV.** Every CSV the tool writes (exports, tagged rows, the timeline, the
forensic report, the activity log, the links, copied rows and the HTML report's CSV download)
puts a `'` before a text value that starts with `=`, `+`, `-`, `@`, a tab or a carriage
return, so a spreadsheet shows it instead of running it as a formula. Numbers are never
changed. The export dialog can turn this off for a CSV export; the manifest's
`spreadsheet_safe` and `value_encoding` say which was used. NUL is written as the four
characters `\x00` (Python 3.8-3.10 cannot write it in CSV otherwise). A CSV that fails while
being written is removed, and the message says so.

**SQL taken from the evidence.** Copy with related as SQL writes `CREATE TABLE` statements
rebuilt from the parsed columns (quoted names, declared types, NOT NULL, primary key, WITHOUT
ROWID), never the statement text stored in the database; what cannot be rebuilt is written as
`--` comment lines only. The Database Map and the schema report show a stored statement up
to its end; text stored after it is shown as `--` comments. Markdown code blocks always use a
fence longer than any backtick run inside them.

**HTML reports run only their own script**: the page's Content-Security-Policy allows the
SHA-256 of that one script, so no inline event handler or other script can run.

### Evidence, verification and the activity log

- **Write guard**: nothing is written into an evidence folder -- in a case also not into the
  folder the case was opened from, any folder a database was found in (chosen or not), or the
  folder of a database that failed to open -- and a file with another name (a hard link) is
  never written over. File names made from table, tag or database names never form a Windows
  device name (`NUL.csv` becomes `_NUL.csv`), `.`, `..` or a right-to-left trick.
- **Network paths**: a path in a case file or the Recent list on another computer (`\\server\
  share`, `\\?\UNC\…`, a mapped network drive) is never checked or opened on its own, since
  that makes Windows connect to that computer with your sign-in. Recent shows it as "network
  path, not checked"; reopening such a case lists every path first and asks whether to open
  the network ones too. A database colour in a case file must be `#rrggbb`; another is
  replaced and listed under Issues.
- **Database ▾ > Evidence and verification**: every file (database, WAL, SHM, journal) with
  size, modification time (UTC) and SHA-256; the **other files next to the database** the tool
  does not use (e.g. `msgstore.db-wal.bak`, `x.db-wal.1`), listed so nothing is missed; **Verify
  now** (on a worker thread, with progress and Stop: a stopped re-hash says the SHA-256 was not
  re-computed for every file); **Compare with expected hash…** (paste the acquisition hash or a
  `sha256sum` / BSD list: each file says match, MISMATCH or not hashed yet).
- **On close** (and when a database is removed from a case) size and modification time are
  verified, and the SHA-256 again for files up to `verify_rehash_bytes` (256 MB). Closing a
  case closes its databases at once and verifies their files afterwards on a worker thread
  (the header says "verifying the evidence of 35 databases just closed…"); a re-hash taking
  more than half a second shows a progress window with **Skip SHA-256 (size and time only)**.
  The header then says what was verified ("verified unchanged (size, mtime and SHA-256)"), each
  result goes to the case's activity log, and a changed file is warned about. Quitting the app
  verifies before it exits.
- **Database ▾ > Info** gives the file's header facts and how it was opened, with the same names
  as the chips (WAL merged, WAL not in SQL, immutable, read natively).
- **Database ▾ > Issues** lists what the engine had to skip, substitute or guess, and what the
  Forensics scans met (marked "Forensics:"); a log that reached its limit (`issues_kept`) says
  how many more there were.
- **Database ▾ > Activity log**: what was done with the case -- databases opened (with their
  hashes when computed), searches, tags, exports (path, rows, SHA-256, manifest), verifications,
  closes -- appended to a file per case in the application data folder, viewable and
  exportable as CSV, JSON or text.

### Limits

Every cap (rows kept, sample sizes, memory for the in-RAM WAL view, time, parallelism) is a
named limit with a default and a range: **Database ▾ > Limits…** lists them in one list (limit,
value, default or "changed", what it limits) with a Find box and an editor for the limit
selected (double-click or Enter edits it; a wrong value is red and says why, and Save refuses
wrong values), and `settings.json` (`{"limits": {"name": value}}`) overrides them; a
value out of range keeps the default and is listed under Issues. Whatever a limit leaves out is
said where it happens, with the limit's name (e.g. `native_sort_rows`, `ram_overlay_bytes`,
`carve_max_records`, `live_hash_rows`, `journal_records`, `audit_ptrmap_pages`,
`relations_target_scan_rows`, `history_scan_rows`, `forensics_history_keys`,
`timeline_total_events`, `value_search_rows`, `related_rows_shown`, `issues_kept`,
`sql_history`, `tag_blob_bytes`).

---

## Security

Everything the tool reads is treated as possibly written by an adversary: the database files,
their `-wal` and `-journal` files, the values inside them, file names in an evidence folder, and
case and settings files. Whatever they contain, the examiner's computer and the evidence stay
safe.

- **Nothing is written to the evidence.** Databases are opened read-only with SQLite's
  `immutable` mode, never copied, and the tool refuses to save anything inside an evidence
  folder.
- **No code from the evidence runs.** Table definitions stored in a database are only read,
  never executed; SQLite extensions are never loaded; views and computed columns run within time
  and size limits.
- **Damaged or forged files cannot exhaust memory.** Sizes a file claims are checked against
  what it really holds before anything is loaded, and every decoder has a limit.
- **Safe parse** (Open ▾ › Open with Safe parse…) reads a file with the tool's own parser only,
  without SQLite. It is used automatically for files SQLite cannot read.
- **Exports are safe to open.** CSV files are protected against spreadsheet formulas, copied
  SQL and Markdown never carry evidence text as commands, and HTML reports run only their own
  script.
- **Network paths are not contacted** until you choose to open them.
- **Every limit is a setting** (Limits…), and the tool always says when one cut something.

## Run from source

### Requirements

- **Python 3.8+** (tested on 3.8 to 3.14)
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

`python sqlite_gui_analyzer.py --version` prints the version, and `--self-test` checks the
installation without opening a window (it builds a small database in a temporary folder, opens,
browses and searches it, and verifies nothing was written next to it).

---

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Ctrl+O` | Open database |
| `Ctrl+K` | Command palette: any database, table, column, tab or action |
| `Ctrl+B` | Show or hide the databases panel (the Case navigator) |
| `Ctrl+F` | Focus the search field of the tab or window shown (the Search tab's otherwise) |
| Typing, `↑` / `↓`, `Enter`, `Escape` | In a dropdown: filter, choose, pick, close (`Alt+Down` opens it) |
| `Alt+Down` in a date field | Calendar: arrows move the day, `PageUp` / `PageDown` the month, `Enter` picks |
| `Enter` / `F3`, `Shift+Enter`, `Escape` | In a search field: next match, previous match, clear |
| `Enter` | Start search / Open row detail |
| `Escape` | Stop the running search (in the Search tab) |
| `Ctrl+E` | Go to the SQL editor |
| `Ctrl+Enter` | SQL tab: run the query |
| `Alt+Up` / `Alt+Down` | SQL tab: previous / next query of the history |
| `Double-click` | Open row detail / WAL record detail |
| `Ctrl+C` / `Ctrl+Shift+C` | Browse grid: copy the cell / the selected rows as TSV |
| `Shift+Enter` | Browse grid: View value (the whole text of the current cell) |
| `Ctrl+T` / `Ctrl+1`..`Ctrl+9` | Browse grid and search results: tag or untag the selected rows (first tag / tag N) |
| `Delete` | Tagged tab: remove a tag from the selected rows |
| `PageUp` `PageDown` `Home` `End` (`Ctrl+Home` / `Ctrl+End`) | Grids: move by a page, to the first / last row |
| `Ctrl+Left` / `Ctrl+Right`, `Shift+Up` / `Shift+Down`, `Ctrl+A` | Grids: first / last column, extend the selection, select all rows |
| `Escape` (in a column filter) | Clear that filter |
| `Ctrl+F`, `F3` / `Shift+F3`, `Escape` | View value: find, next / previous match, close |
| `Right-click` | Navigator menu (a database or a table); grid cells and headers; on a search result: open, copy the value, go to its WAL frames, row history, related rows, Copy with related, tag, expand / collapse all |

---

## Performance

Measured on a 3.7 GB chat database (275 tables, 2.5 million messages) on an ordinary Windows 11
laptop, searching every table:

| Search | Example | Time |
|--------|---------|------|
| Text, any case | `hello` | about 2 s (5 s the first time, before the file is cached) |
| Regular expression | `https?://[^\s]+` | about 23 s |

A case of 35 databases (741 tables) is searched for `hello` in about 2.4 s.

Scrolling stays instant: any 200-row window of a 5-million-row table is drawn in about 1 ms once
the table is indexed in the background (1–5 s after it is opened, sorted or filtered), and the
grid never shows an empty row while it reads.

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

## Try it on the demo database

A small demo database with WAL data comes with the source:

```bash
python sqlite_gui_analyzer.py test_data/wal_demo.db
```

The demo includes 18 tables, views, indexes, triggers, and a WAL file with committed, uncommitted, and overwritten records -- ideal for exploring the forensic analysis features.

---

## FAQ

**Will this tool modify my database?**
No. The database is never copied and nothing is written in its folder -- not even a `-shm` file.
SQLite opens the original with `mode=ro&immutable=1`, and the tool's own parser reads the pages.
Size, modification time and SHA-256 of the database and its sidecars are recorded when you open
it and verified when you close it (Database ▾ › Evidence and verification…). Exports into the
evidence folder are refused.

**Does it handle WAL mode databases?**
Yes, without ever letting SQLite touch the WAL:

1. **Current state** -- the tool replays the WAL itself (salt and checksum validation, like SQLite's
   recovery) and merges the committed frames **in memory**. On Python 3.11+ SQL queries run on that
   in-memory image (up to the limit `ram_overlay_bytes`); otherwise tables are read by the native
   parser, a status chip says so, and the SQL tab says which committed frames its results miss.
2. **Forensic WAL analysis** -- the WAL tab shows every frame, including uncommitted,
   superseded and stale (earlier WAL generation) frames that SQLite hides.

**What if SQLite says the file is malformed?**
The tool falls back to its own B-tree parser and shows every row it can still reach, with a status
chip and per-table notes explaining what could not be read.

**How is correctness tested?**
`python -m unittest discover -t . -s tests` runs the unit, fixture and evidence-safety tests
(standard library only). Setting `SQLITE_CORPUS` to a folder of public test databases also runs a
differential test that compares every table the native parser reads against SQLite itself.

**Can it handle large databases?**
Yes. Tested on databases over 5 GB with hundreds of tables and millions of rows.

**Do I need to know SQL?**
No. All functionality is available through the GUI.

**What about encrypted databases?**
Standard unencrypted SQLite databases only. Encrypted databases (SQLCipher, etc.) must be decrypted first.

---

## Contributing

Contributions, bug reports, and feature requests are welcome. The app uses the Python standard
library only (Python 3.8 to 3.14); run the tests with `python -m unittest discover -s tests -t .`
and the app with `python sqlite_gui_analyzer.py`.

### Project structure

```
sqlite_gui_analyzer.py       Entry point
src/
  engine/                    Evidence-safe engine (no tkinter)
    evidence.py              Read-only open, sidecars, SHA-256, verification, write guard
    fileformat/              Native parser: header, records, WAL, pager, B-trees, freelist
    schema.py                Tables/views, column mapping, WITHOUT ROWID, row locators
    backends.py              SQL (immutable / in-RAM WAL overlay) and native row sources
    sqlsafe.py               SQL from the evidence with no power: scratch replays, hardened
                             readers, step and size budgets
    locks.py                 Evidence held by another program (lock-byte page, who has it open)
    xmlpretty.py             The XML pretty-printer (own strict parser, no DTD, no entities)
    filters.py               Column filter expressions, evaluated in SQL and natively alike
    session.py               The facade the UI uses (modes, browse, row, count, search)
    positions.py             Position indexes: windows anywhere in a table in milliseconds
    search.py                Matching rules shared by table, WAL and freelist search
    bytesearch.py            Byte patterns: hex with wildcards, text as UTF-8 / UTF-16
    tags.py                  Row tags: stable row keys, the tag store, saved view state, settings,
                             saved cases
    tag_export.py            Tagged rows as an HTML report, a CSV folder or JSON
    case.py                  Folder scan (SQLite header), database identity, names, colours
    crossdb.py               Links between the databases of a case, matched by value
    timeline.py              Date column detection, dated events (SQL / native), export
    linkgraph.py             The relationship diagram: layout and SVG
    related_copy.py          Copy with related: batched link walk, streamed Markdown/JSON/SQL
    datamap.py               The Database Map (HTML / Markdown / JSON) and shared helpers
    limits.py                Every named limit with its default and range
    uiyield.py               Worker threads give way while the Tk thread is busy
    export.py                One export writer: provenance, value encoding, manifests
    csvcells.py              The CSV rules of every writer: spreadsheet-safe text, NUL
    sqltext.py               SQL written from evidence text: comments, rebuilt CREATE TABLE
    filenames.py             One sanitizer for file names made from data
    activity.py              The examiner's activity log (one file per case, app data)
    decode/                  BLOB decoders: plists, keyed archives, protobuf, typedstream,
                             compression, text; timestamps in every common epoch
    forensics/               Carving, row history, dropped schema, rollback journal, audit,
                             reports; provenance and confidence on every recovered record
  app.py                     Main application window (one-line header, navigator, tabs)
  case.py                    The open databases (a case) and the active one
  case_ui.py                 Open folder dialog, a database's hover text
  navigator.py               The Case navigator and the name index of every table and column
  overview_tab.py            Overview tab: the databases of a case, dates found, cards
  scope.py                   Which databases each feature covers: the scope model and picker
  breadcrumb.py              '● database ▾ › table ▾' bar of the one-database tabs
  palette.py                 Command palette (Ctrl+K)
  density.py                 Timeline density chart (canvas histogram, drag to select)
  combobox.py                Searchable dropdown (replaces every ttk.Combobox)
  datepicker.py              Date-time picker and date range picker
  tokens.py                  Design tokens: spacing, type, colours, button kinds
  theme.py                   The ttk styles built from the tokens
  parts.py                   Status line, expander, chips, cards, empty states, toolbars
  lookups.py                 Browse 'Show value from linked table'
  forensics_tab.py           Forensics tab (recovered records, freed pages, history, dropped
                             tables, journal, audit)
  wal_tab.py                 WAL tab (frames, records compared with the database, row detail)
  jobs.py                    Long jobs with progress and Stop; the export dialog and writer
  timeline_tab.py            Timeline tab (date columns, events, exports)
  date_columns.py            Browse 'Show as date' column formats
  appicons.py                The logo and illustrations (assets/, PNG read by Tk)
  assets/                    Logo sizes, welcome and empty-state illustrations
  datamap_ui.py              Copy with related, Export Database Map and Limits windows
  tagging.py                 Tag menus, keys, bulk tagging, saving and the Recent menu
  tags_tab.py                Tagged tab, tag manager and export dialog
  grid.py                    Virtual data grid (Browse): draws only the cells in view
  browse_sources.py          Row sources for the grid (engine tables, in-memory rows)
  value_viewer.py            View value: a whole text value with find, wrap, pretty-print
  database.py                UI-facing facade over engine.session
  wal_parser.py              WAL tab adapter over engine.fileformat
  search_results.py          Search hits grouped one line per row (WAL copies folded in)
  inspector.py               BLOB Inspector window (decoded tree, linked hex, timestamps)
  previews.py                Image previews within the pixel budget
  hexview.py                 Virtual hex viewer widget
  dialogs.py                 Help, the one Scope dialog (a database or a case), row detail and
                             text windows
  widgets.py                 ToolTip, flow rows that wrap, shortened labels, theme setup
  utils.py                   Utility functions, schema HTML generator
  constants.py               Shared constants, colors, BLOB signatures
test_data/
  wal_demo.db                Demo database with WAL forensic data
tests/                       unittest suite (fixtures built at test time; stdlib only);
                             SGA_STRESS=1 also runs the multi-million-row stress tests
tools/
  ui_smoke.py                Scripted run of every tab against databases (temp copies)
  fuzz.py                    Mutation fuzzer of every parser and decoder (stdlib, child
                             processes with a memory cap)
  ux_probe.py                A 1-60 database case off the screen: header, clipping and text
                             walls per tab; --engine-only: a real folder read-only, counts only
  baseline_probe.py          Corpus probe: empty tables, search misses, undecoded plists
  decode_probe.py            Corpus probe: decodes every BLOB, reports failures and timings
  perf_check.py              Open + first-page timing on a large database, evidence check
  grid_perf.py               Browse grid timings on generated 250-column / 1M-row tables
  scroll_perf.py             Window latency with/without position indexes, scrollbar drags
  release.py                 Version, version resource, tag check, zip, checksums, release notes
  run_self_test.ps1          Runs a built exe's --self-test and checks its exit code
  build-requirements.txt     PyInstaller and Pillow versions for the Windows builds
sqlite-gui-analyzer.spec     PyInstaller build (folder and single-file programs)
installer/
  sqlite-gui-analyzer.iss    Inno Setup script (per-user or all-users installer)
.github/workflows/           CI (tests on Windows and Linux, Python 3.8-3.14) and Release
CHANGELOG.md                 Changes per version (also the release notes)
```

## License

MIT
