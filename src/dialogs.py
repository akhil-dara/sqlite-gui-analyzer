"""Dialog windows for SQLite GUI Analyzer."""

import os
import sys
import io
import json
import binascii
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from constants import C, HAS_PIL, VERSION, WAL_STATES, _EXT_MAP, mode_label
from tokens import COLOR as K, FONT as F
if HAS_PIL:
    from constants import PILImage, ImageTk
from utils import (blob_type, is_image, fmtb, _build_schema_text, fmt_count,
                   _int_count, write_allowed, blob_file_name, create_new_file, ROW_FLAGS)
from engine.decode import summary as blob_summary
from engine.decode import timestamps
from engine.fileformat.record import InvalidText
from inspector import BlobInspector
import previews
from widgets import FlowFrame, SearchBox, ToolTip, TreeFilter, fit_geometry, place_over


# ── HelpDialog ───────────────────────────────────────────────────────────
HELP = [
    ("Overview",
     "SQLite GUI Analyzer opens SQLite databases read-only to search, browse and examine them, "
     "including what the database no longer shows: row versions in the WAL, records in freed "
     "pages and free space, the rollback journal. Nothing is ever written next to the evidence "
     "(see Evidence safety).\n\n"
     "The header is one line, however many databases are open: ☰ (Ctrl+B), or the ◀ / ▶ strip at the panel's edge, shows or hides the "
     "databases panel; then the case name (the app or folder the databases share) and its "
     "summary ('16 databases · 1.2 GB · 247 tables · active: accounts_db', or for one database "
     "its path, size, tables and load time); one chip for the evidence state ('16 read-only · 7 "
     "WAL merged') and, only when there are some, a warnings chip ('⚠ 2 warnings': a hot "
     "journal, a WAL SQL cannot see…) - click either for the status of every database, those "
     "with warnings first. Then 'Go to…  Ctrl+K' (the command palette), Open ▾ (Open database… "
     "Ctrl+O, Open folder…, Add database(s)… to the case, Recent), Database ▾ (Info, Evidence and "
     "verification…, Issues…, Activity log…, Schema report (HTML)…, Database Map…, Limits…), "
     "Issues (N) (only when the engine had to skip, substitute or guess something), Help and "
     "Close (Close case with several databases, after asking).\n\n"
     "The databases panel on the left (the Case navigator, see its own section) lists the "
     "databases, their tables and columns. The tabs keep plain names: Overview (a case of "
     "several databases), Search, Browse, WAL (only with a -wal file), SQL, Forensics, Timeline, "
     "Tagged, Relationships. The tabs that show one database (Browse, WAL, SQL, Forensics) say "
     "which in a breadcrumb bar at their top ('● accounts_db ▾ › account ▾'); in a case its "
     "dropdown makes another database the active one.\n\n"
     "Every list of the tool has the same search field (grey hint while empty, × to clear, 'N "
     "of M' or 'No … matches') and Ctrl+F goes to the one in view; every dropdown can be "
     "searched by typing (the matching letters are highlighted, the most recent choices come "
     "first). A status line is one line; its 'Details ▸' folds out the rest."),
    ("Case navigator",
     "The panel on the left lists the open databases grouped by the app or folder they come "
     "from ('com.whatsapp (3)'; with many databases the groups are folded except the active "
     "database's; Pinned comes first). Each database line has its colour dot (the active one "
     "ringed and bold), its status (WAL: its WAL merged; WAL!: a WAL SQL cannot see; ⚠: a "
     "warning), its tables and its rows; hover for its path, size, SHA-256, how it was opened "
     "and its warnings. Expand a database for its tables with their rows (then Views, Triggers "
     "and WAL-only tables when it has them), a table for its columns with their types, foreign "
     "keys, indexes and CHECK constraints. Click a table to open it in Browse (its database "
     "becomes the active one); double-click or Enter on a database makes it active.\n\n"
     "The search box finds databases, tables and columns of every database as you type (the "
     "tables holding a matching column are opened on it) and says how many match; Enter goes to "
     "the next match. The chips keep only the databases with rows, with a WAL, with dates (once "
     "known), with hits of the last search, or with warnings - each shown only when it applies. "
     "Sort ▾ orders them by name, size, rows, search hits or when last used, and folds or opens "
     "every group; its 'Hide empty tables (0 rows)' leaves tables without rows out (a line "
     "says how many are hidden; click it to list them again). A table's columns show their "
     "full declared type.\n\n"
     "Select several databases (Ctrl or Shift+click): 'Use as scope' makes them the databases "
     "Search, Timeline, Relationships, Find everywhere and the Database Map cover. Right-click a "
     "database: Make active, Open in Browse, Pin to top, Show in folder, Copy path, Hash "
     "details…, Remove from case; a table: Browse, Copy CREATE SQL, Copy table name, Search in "
     "it, and for a column Column relationships…. 'CREATE statement' at the bottom shows the "
     "selected table's with Copy CREATE and Copy schema."),
    ("Overview tab",
     "The first tab of a case of several databases, and the one shown after Open folder…. "
     "Cards give the range of the dates found (sampled from the first and last rows of each "
     "table, as the Timeline samples them), the biggest tables, the links between the "
     "databases (click: Relationships) and the warnings (click: the status of every database). "
     "The table lists every database: its app or folder, size, tables, rows, WAL state, "
     "SHA-256, the dates found and the links in and out; click a heading to sort, type in the "
     "box to filter, double-click to browse a database, right-click for the navigator's menu. "
     "The dates are looked for in the background, database after database (Stop stops it, "
     "Find dates starts again, limit overview_date_tables), and the Timeline reuses what was "
     "found."),
    ("Command palette",
     "Ctrl+K (or 'Go to…' in the header) opens one search over every database, table and "
     "column of the case, the tabs and the actions (Build timeline, Export Database Map…, Find "
     "value…, Evidence and verification…, Limits…). Type part of a name or its letters in order "
     "('msgdb' finds msgstore.db); ↑/↓ choose, Enter opens (a table in Browse, a database made "
     "active, a tab shown, an action run), Escape closes. With nothing typed the recent choices "
     "come first (limit palette_recent); at most palette_results are listed and it says how many "
     "more match."),
    ("Scope (which databases)",
     "Search, Timeline, Relationships, Find everywhere and the Database Map each have the same "
     "scope button: 'All 16 databases ▾' or '3 of 16 databases ▾'. Its popover lists the "
     "databases grouped by app or folder with a tick each (a group's tick ticks all of its "
     "databases; ◪ when some are ticked), a search box, presets (All, Only active, Selected in "
     "navigator, With hits, With dates - offered only when they apply) and Saved scopes (save "
     "the ticked databases under a name, such as 'Messaging DBs', and pick it again). The scope "
     "is chosen once per case and remembered with it: every feature follows it, unless 'Only "
     "for <feature>' gives that one its own choice - then 'Follow global scope' beside its "
     "button puts it back. Done (or Escape) applies, Cancel drops the changes. A Timeline "
     "already built stays until Build timeline reads the new scope; an open Find everywhere "
     "window searches again only when its own button is changed."),
    ("Search tab",
     "Search: the term, the mode (see Search modes), Search (Enter) and Stop (Escape, in the "
     "Search tab). Searches every cell of the tables in the scope, several tables at once. With "
     "'One line per row' (the default) each row appears once, however many of its cells match; "
     "untick it for one line per cell. Double-click a result to open its row; right-click for "
     "Open row detail, Copy matched value, Go to WAL frame (the frames holding it), Row history "
     "(every version), Tag, Related rows, Find this value everywhere, Copy with related, and "
     "Expand all / Collapse all.\n\n"
     "The options row: in a case the scope button (which databases, see Scope), 'All tables ▾' "
     "(the tables and views searched: one dialog for one database or, in a case, each "
     "database's tables under its name; Hide empty never hides a table still being counted; "
     "All listed, None listed, Invert and Only with rows act on the tables listed; the button "
     "then says '12 of 19 tables ▾' and Reset searches all again), WAL row versions and Freed "
     "pages (offered only when a searched database has them), and Advanced ▾ (it says how many "
     "of its options are on): Include BLOB bytes (search BLOBs as bytes: text in UTF-8/UTF-16, "
     "and hex patterns), Include decoded BLOBs (plists, protobuf, keyed archives, compressed "
     "data…), Include views (off by default: views repeat their tables' rows) and Max matching "
     "rows per table (100, 500, 1000, 5000 or All: no limit).\n\n"
     "One row more than the maximum is read, so a table is said to have stopped at the limit "
     "only when it had more: the status names it ('stopped at Max rows/table in 2 places', "
     "Details lists them) and the Table filter shows '(100+)'. The status is one line - "
     "'Complete: 3 matches in 2 rows… · 2 databases with matches', or 'Stopped after 3 of 12 "
     "tables' - and its Details list each database with matches and its rows, then one line "
     "for the databases searched without a match ('14 databases: searched, nothing found') "
     "and one for those not searched (stopped).\n\n"
     "The results are filtered by Database (a case), Source (DB, WAL, Freed pages), Table, "
     "Column and Type (searchable dropdowns), and paged with ◀ ▶. 'N errors' appears only when "
     "a table could not be searched, and lists why. Export ▾: Matches (CSV or JSON)…, Matches "
     "with their whole rows (JSON)…, or Copy results. After a search the navigator offers 'Has "
     "hits' and the scope 'With hits'."),
    ("Search modes",
     "The modes come in three groups in the mode menu.\n"
     "Text (6): Case-Insensitive (default), Case-Sensitive, Exact Match, Starts With, Ends With, "
     "Regex (Python regular expressions; type a single backslash: \\d).\n"
     "Binary (2): Text in BLOBs (the term inside BLOBs as UTF-8, UTF-16LE and UTF-16BE; with "
     "'Include BLOB bytes' a term like 'ff d8 ff' also as bytes), Byte pattern (hex) ('ff d8 ff', "
     "'0x1f8b', '\\x89PNG', 'de ad ?? ef' with ?? for any byte; BLOBs and text as stored bytes).\n"
     "Schema (1): Column Name (columns whose name contains the term).\n\n"
     "Regex: a literal part of the pattern ('foo@bar\\.com' gives 'foo@bar.com') lets SQLite "
     "pre-filter the rows; a pattern without one reads every row. Examples: phone "
     "\\b[6-9]\\d{9}\\b; e-mail (?i)\\b[a-z0-9._%+-]+@[a-z0-9.-]+\\.[a-z]{2,}\\b; URL "
     "https?://[^\\s<>\"]+."),
    ("Browse tab",
     "The breadcrumb ('● msgstore.db ▾ › message ▾') chooses the database (in a case) and the "
     "table or view (a table only the WAL still holds is listed as 'WAL: name' and read on a "
     "worker, with the values as stored). The grid scrolls through all rows (read as you "
     "scroll, however large; a jump far into a sorted or filtered view says which rows it "
     "reads and for how long, and is read again through the row index once that is built). "
     "Click a header to sort; drag a border to resize, double-click it to fit.\n\n"
     "Filter all columns keeps rows where every word occurs in some column, as you type; the "
     "words found are highlighted in the cells (? shows the syntax). Each column header has a "
     "funnel (Alt+Down on the current column): its filter window sorts A→Z / Z→A, shows the "
     "column's values with their counts (most frequent first, searchable, Select all / none, "
     "'(Blanks)'; the top values with bars; read in the background and it says when the "
     "limit filter_distinct_values or filter_distinct_scan_rows cut the list) and offers the "
     "conditions that suit the column: for text contains / does not contain / equals / starts "
     "with / ends with / regex / is empty, for numbers = ≠ > ≥ < ≤ between / top N / bottom "
     "N, for dates (a detected Unix, Cocoa, WebKit... column) before / after / between / on / "
     "last N days / this month with a calendar and a histogram of the column's dates to drag "
     "across, a few values as chips; two conditions joined with AND / OR. It counts the rows "
     "a condition keeps before you apply it. Apply, Clear filter, Cancel.\n\n"
     "The filters in force are chips above the grid ('status = 3 ×', 'timestamp: 1 Mar – 5 Mar "
     "2026 ×'): click one to edit it, × or Delete removes it; the bar counts the rows kept "
     "('1,204 of 2,460,000 rows') and has Clear all, Back / Forward (and Ctrl+Z / Ctrl+Y) "
     "through the filters you used, Save filter… and Saved ▾ (saved per table name, so a "
     "filter saved on one database's table applies to the same table of another), Copy as "
     "SQL WHERE and Copy as filter text. A filter that leaves no row says so, with one click "
     "to remove the last condition. The filter row under the headers is still there to type "
     "filters (header menu › Filter row under the headers):\n"
     "  text  contains (any case)     !text  does not contain     >5 >=5 <5 <=5 =x <>x  compare\n"
     "  5~10  range     a%b_c  LIKE     /regex/ or /regex/i     NULL  NOT NULL     \"\" empty\n"
     "  ^=text starts with   $=text ends with   EMPTY   IN (a, b)   {a} AND {b}   {a} OR {b}\n"
     "The same filters work in every grid (SQL, WAL, Forensics, Timeline, Tagged). Row panel "
     "shows every column of the current row beside the grid; Columns… hides and shows "
     "columns; Limits… opens the limits.\n\n"
     "Click a cell, Shift+click to select rows; Ctrl+C copies the cell, Ctrl+Shift+C the rows. "
     "Right-click a cell for Copy cell, Copy raw value (a column shown as dates), Copy rows as "
     "TSV / CSV / JSON, Filter to this value, Exclude this value, Filter to this day / hour "
     "(dates), Show rows with the same…, Filter to these values (several cells), Clear all "
     "filters, Open row detail (Enter), View "
     "value… (Shift+Enter), Inspect BLOB…, Hide column, Column chooser…, Row history (every "
     "version), Related rows, Find this value everywhere, Copy with related and Tag (only what "
     "applies to the cell is offered). Right-click a header for Sort ascending / descending, "
     "Autosize column, Hide column, Column chooser…, Show as date, Show value from linked "
     "table… and Column relationships….\n\n"
     "Export ▾: Rows (CSV or JSON)… writes the rows the filters keep (or the selected rows) on a "
     "worker thread with progress and Stop; BLOBs as files… (offered when the table holds BLOB "
     "values, in any column) writes each BLOB as a file with a manifest."),
    ("Row detail",
     "Double-click or Enter opens a row in its own window, titled 'Row detail — table, row N "
     "(database)'; its first line says where the row is read from (database, open mode, table, "
     "row). Each value shows beside its column, NULL in grey, with the dates a number plausibly "
     "is, 'Inspect BLOB…' for a BLOB and 'Inspect bytes…' for text that is not valid in the "
     "database's encoding. Copy copies one value; 'Related (N)' appears beside a value other "
     "tables hold (counted in the background) and right-click gives Related rows and Find this "
     "value everywhere. The bottom bar copies the row as JSON, CSV or text, the table's CREATE "
     "SQL or schema, exports its BLOBs, and offers Copy with related.\n\n"
     "Find at the top (Ctrl+F) lists only the columns whose name or value holds every word "
     "('12 of 250 columns match'); Enter goes to the next one and marks its name. It looks at "
     "up to 100,000 characters of a text and at a BLOB's summary and first 512 bytes (hex).\n\n"
     "A WAL record, a recovered record or a query result opens in the same kind of window, "
     "headed with where it was found (frame and page, page and offset with its confidence, or "
     "the query), with the same Find."),
    ("Rows and their related rows",
     "Related rows (right-click a cell) lists the rows of other tables that hold the value "
     "through a trusted link, per link; All related rows… opens them in a window. In a case, "
     "links to other databases are matched by value and listed apart.\n\n"
     "Find this value everywhere looks for the value in every column of every table (of the "
     "databases its scope button covers, see Scope), stopping at the limit value_search_rows "
     "per table and "
     "saying where it did; Find inside other values also finds it inside longer text.\n\n"
     "Copy with related (right-click rows, the row detail, or a search result) copies or exports "
     "the rows and the rows linked to them, up to 'Links to follow' links deep and 'Rows per "
     "link' rows per link (limits related_hops, related_rows_per_link), as Markdown, JSON or SQL, "
     "with a preview and the evidence SHA-256; Export… writes a file with a manifest.\n\n"
     "Show value from linked table… (right-click the header of a column that links to another "
     "table) shows that table's value beside the stored one; exports keep the stored value and "
     "add the linked one as its own column. Stop showing the linked value ends it.\n\n"
     "Database ▾ › Database Map… (or Relationships › Export ▾) writes a map of the database, or "
     "of the whole case: its tables, links, date and BLOB columns and ready-made queries, as HTML, "
     "Markdown or JSON, with a manifest."),
    ("SQL tab",
     "Runs statements that read: SELECT, WITH, VALUES, EXPLAIN and read-only PRAGMA (comments "
     "before them are fine). The connection is read-only, so anything that would change the "
     "database is refused, inline, and the rows of an earlier query are cleared. ▶ Run (Ctrl+Enter) "
     "runs, ■ Stop ends a long query, Clear empties the editor, Copy SQL copies it. Limit (100 to "
     "All) keeps that many rows and says when the query had more. Query history lists the last "
     "100 queries (limit sql_history); Alt+Up/Down walks it in the editor; Clear history empties "
     "it. Ctrl+E goes to the editor from anywhere. Export ▾: Export CSV… or Export JSON…, with "
     "the query in the provenance.\n\n"
     "When the database is read in main-only mode (a WAL too large for the in-memory view, limit "
     "ram_overlay_bytes, or Python before 3.11) a notice above the results says that SQL sees "
     "the main file only and how many committed WAL frames it misses, with 'Browse (WAL "
     "applied)' and 'Limits…'; Browse, Search and the Timeline include them."),
    ("WAL tab",
     "Shown for every database with a -wal file, right after Browse; when the file cannot be "
     "read, the tab says why and that its frames are not applied. The tool parses every frame "
     "itself, verifies the checksum chain like SQLite, and labels each frame:\n"
     "@WAL_STATES@\n"
     "A summary line gives the frames per state, commits, checksum failures and sizes; '▶ "
     "Per-table statistics' opens a table per table; 'Technical details' shows the WAL header.\n\n"
     "Frames lists each frame with its table, state, page type, records (counted in the "
     "background), checksum and transaction; Status, Table, Page type and Page # filter it and "
     "the headers sort it. Select a frame for its summary, the records on the page (double-click "
     "opens one, right-click tags it or opens its row history) and its bytes.\n\n"
     "Records reads every record of the frames (the Status and Table filters apply) and compares "
     "it with the database's current row: same, different (which columns), not in the database, "
     "a table only the WAL has, or 'could not compare' with the reason in the Note column (a "
     "read error is never taken for a missing row). Show: filters by that result. Changing "
     "Table or Status afterwards filters the compared records at once; asking for records that "
     "were not compared says to compare again. All tables share one 'Values' column; choose a "
     "table to see its own columns. Export ▾: Frames (CSV/JSON)…, Records (CSV/JSON)…, BLOBs as "
     "files…\n\n"
     "Search: 'Include WAL row versions' also searches every row version kept in WAL frames. A "
     "row copied into many frames is one result listing its frames (e.g. 'WAL stale ×3'); a WAL "
     "copy identical to a matching database row is shown on that row's line."),
    ("Forensics tab",
     "Recovered Records recovers rows the database no longer shows. Table: one table or all; "
     "Look in: Freeblocks, Unallocated space, Freed pages, Orphan pages, WAL frames, Replaced "
     "pages, Rollback journal; Time limit: 1 minute, 5 minutes or No limit; 'Index entries' also "
     "recovers deleted index entries. Each record says where it was found and how sure the match "
     "to a table is (high / medium / low, with the reasons); Show: keeps All, Medium and high, "
     "or High. Rows identical to a live row are left out; a table (or index) too large to "
     "compare with its live rows (limits live_hash_rows, live_index_entries) is named in the "
     "status. If a recovery stops early the status says why (Stop, the time limit, or the limit "
     "carve_max_records). Double-click a record for its row detail; Tag ▾ tags records.\n\n"
     "Freed Pages (only for a database with freed pages) lists the pages of the freelist with "
     "the records still on them, the same carver and the same confidence as Recovered Records "
     "and as Search's 'Include freed pages', and each page's bytes.\n\n"
     "Row History: picking a table lists the rows with several versions (limit "
     "forensics_history_keys); picking a row, or a row id or key with Show history, lists "
     "every version of the row across the main file, the WAL and the journal (and says when "
     "the main file could not be read). "
     "Dropped Tables: Find dropped tables recovers dropped tables' "
     "CREATE statements and rows. Rollback Journal (only with a -journal file) summarises the "
     "journal and, with Show rows, shows a table as it was before the journaled transaction. "
     "Audit (Run audit) checks the file for inconsistencies. Each list that stops at a limit "
     "says which; a job that fails says why and is logged under Issues.\n\n"
     "Switching the active database clears these results and each sub-tab says so. Export ▾: "
     "Forensic report (everything found so far)… or Recovered records listed…, as HTML, CSV or "
     "JSON, with the evidence hashes and a manifest."),
    ("Timeline tab",
     "Every dated row in time order. The top bar: in a case the scope button (which databases, "
     "see Scope), the date range (From and To in UTC, typed or picked from a calendar; the "
     "range ▾ presets - last hour, 24 hours, 7 days, 30 days, this month - count back from the "
     "newest date found, not from today), UTC / Local (Local adds a column at a UTC offset, the "
     "Time column stays UTC), Build timeline, Stop and Options ▾: Include WAL row versions, "
     "Include recovered records (the Forensics tab's; it says how many), Max events per column "
     "(default: limit timeline_column_events) and Look for date columns again.\n\n"
     "The date columns panel (◂ folds it away) lists the columns found by name and by their "
     "values, which must read as dates between 1990 and 2040 in one format: Unix seconds, "
     "milliseconds, microseconds or nanoseconds, Cocoa, WebKit / Chrome, FILETIME, HFS+, .NET, "
     "OLE, GPS, or ISO 8601 / RFC 2822 text; its heading counts them ('100 date columns in 12 "
     "databases'). In a case they are grouped under their database, with a tick for the whole "
     "database (◪ when some are ticked) and its count; the databases without date columns "
     "share one line; hover a database for what was looked at and its notes. Click a tick to "
     "leave a column out (Tick shown / Untick shown for the columns listed); right-click it to "
     "read it as another format. The databases the Overview already looked at are not looked "
     "at again.\n\n"
     "Build timeline reads the events, newest first, up to 'Max events per column'. The density "
     "chart above the events shows how many fall in each stretch of time (in a case in the "
     "databases' colours; 'Colour by database' turns it off); hover a bar for its count, drag "
     "across the bars to keep only that range in the grid, click the chart (or ×) to clear it. "
     "The line under it says how many events, from how many databases, of which sources, and "
     "their span. The status is one line; Details lists each database with its events, one "
     "line for those without, and every note (caps, tables that could not be read). In a case "
     "each event's row is marked in its database's colour. Sorting and filtering run on a "
     "worker thread; the search field keeps the events holding every word as you type. "
     "Double-click opens an event's row; right-click for Open row, Row history and Tag. "
     "Removing a database drops only its events. Export ▾: Export HTML…, Export JSON…, Export "
     "CSV… of the events shown, with the export writer every tab uses (provenance, Stop, "
     "manifest)."),
    ("Dates",
     "One decoder serves the whole tool, always in UTC: the row detail shows beside a number the "
     "dates it plausibly is ('Unix milliseconds: 2020-09-13 12:26:40.123 UTC'); 'Show as date' "
     "(right-click a Browse header: Auto, a format, or Off) shows a column as dates while "
     "sorting, filters, copies and exports keep the stored values (Auto reads a sample of the "
     "column in the background, 'reading a sample…' meanwhile); the BLOB Inspector reads a "
     "number in every epoch."),
    ("Tags",
     "Right-click a row and choose Tag (New tag…, Edit note…, Remove all tags), or press Ctrl+T "
     "(the first tag) or Ctrl+1..9. Tagging more than 100 rows at once asks first. The Tagged "
     "tab lists every tagged row with its tags, note and where it was found; its search field "
     "keeps the rows holding every word (and says how many); Edit note, Remove tag (or Delete), Manage tags…, Export… (an HTML "
     "report, a CSV folder or JSON with the evidence hashes and each row's values as they were "
     "when tagged; BLOBs over the limit tag_blob_bytes keep their size, SHA-256 and first part), "
     "Save tags as… and Load tags…. Tags are saved in the application data folder, never next to "
     "the evidence (hover the status line for the file). Tagging, untagging, notes and Tagged "
     "exports are noted in the activity log."),
    ("Several databases (a case)",
     "Open ▾ › Add database(s)… adds files from any folders to the case (Open database… "
     "replaces the open databases, after asking when there are several); Open folder… lists "
     "the SQLite files of a folder (with its subfolders if asked) to tick, and shows the "
     "Overview. The databases open one after the other in the background: each joins the "
     "navigator when it is open, the first is shown at once, and when opening takes a while a "
     "small window says which one is opening, with Stop (those already open stay). With two or "
     "more, the header says so in one line, the Case navigator lists "
     "them grouped by app or folder, and the one-database tabs show which is active in their "
     "breadcrumb. Make another active from the navigator (double-click), the breadcrumb's "
     "dropdown or the command palette. Removing a database (right-click it) stops only the "
     "work that reads it, verifies it like a close, and removes its results; the others' search "
     "results, timeline events and running exports stay (the search status says whose results "
     "went). The scope button of each feature chooses which databases it covers (see Scope); "
     "every result names its database. Links between databases are found by their values and "
     "labelled 'matched by value'; the Overview's card says how many were found. The case is "
     "saved in the application data folder with its scopes, saved scopes and pinned "
     "databases, and listed under Open ▾ › Recent."),
    ("Relationships tab",
     "Links come from declared FOREIGN KEYs, from names (message_id to 'message'; table prefixes "
     "like moz_ are left out; Core Data ZFOO to ZFOO.Z_PK), parent/fk columns, the same id-like "
     "column names, and between databases from values. A link is trusted only when its values "
     "agree: at least 3 distinct values for a name link, 10 for same-named columns (limits "
     "relations_min_name_values, relations_min_same_name_values); the others are listed as "
     "weaker, with the reason, and a check that read only part of a column says so. A table "
     "without rows cannot confirm a link by values: such links are weaker ('unverified — table "
     "is empty'), declared keys to it say 'declared, no rows', and empty tables are hidden "
     "unless 'Show empty tables (N)' is ticked.\n\n"
     "One search field (live as you type; Enter or Find: next match; × or Escape clears it; "
     "it says how many match) works in the view shown. Tables: the tables with links and the "
     "selected table's card - 'Refers to', 'Referred by' (tables linked alike grouped, e.g. "
     "'142 tables via message_row_id → _id'), 'Shares values with' and a collapsed 'Weaker "
     "links (N)', each line with its strength (Declared, Strong: values found for 95% of the "
     "sample, Likely, Weak), values found and rows; Browse, Column relationships…, Show in "
     "diagram. Diagram: an entity-relationship diagram of the selected table and the tables "
     "linked to it (1 or 2 links away; Overview: every linked table), drawn with the trusted "
     "links (weaker ones are listed, not drawn) when the tab is shown. Each table is a card "
     "listing its columns with their types and PK / FK markers (up to the limit "
     "diagram_card_columns, then 'N more columns'; 'Linked columns only' folds the cards). A "
     "connector runs from the exact column to the exact column, with its cardinality (1 or * "
     "at each end: from the schema, or from the values when they were checked); several "
     "relationships between the same two tables are separate connectors, a table referring to "
     "itself has a loop, and a junction table (two or more columns referring to other tables) "
     "can be drawn as one N:M line ('N:M'). Tables linked alike to one table are a group card "
     "listing them (limit diagram_group_min). Drag a card to move it (its place is kept per "
     "database), Tidy removes overlaps, Auto-arrange lays everything out again with as few "
     "crossings as possible; wheel or − / + zoom, drag the background to pan, Fit shows "
     "everything, the minimap moves the view. Hover a connector to follow it end to end; "
     "click it for its side panel (the columns, the evidence, the cardinality, a sample JOIN, "
     "Browse both sides); click a table for its links, double-click to browse it. All links: "
     "every link, 'Include weaker links', 'Only <table>'. "
     "Map again maps them again. Export ▾: Links listed (CSV)…, Diagram (SVG)… (every group "
     "listed table by table), Database Map…."),
    ("BLOB Inspector",
     "'Inspect BLOB…' opens a BLOB decoded: property lists, keyed archives, protobuf, gzip / zlib "
     "/ bz2 / xz, LZ4, zstd and Apple LZFSE / LZVN (pure Python, on every Python), base64, "
     "JSON, text, typedstream and images, nested data decoded again. Decoding is held to "
     "named budgets (decode_*, summary_*, decode_lzma_memory: an xz / lzma stream declaring a "
     "larger dictionary is described, not decoded); the note says which one it reached. An "
     "image is previewed only up to preview_pixels pixels (zoom included); a larger one says "
     "why it is not drawn. "
     "Selecting a value highlights its bytes in the Hex tab. The Timestamp tab reads a number in "
     "every epoch (UTC) and marks plausible dates; Find narrows its readings. Copy value, Copy "
     "hex, Copy base64; Save "
     "BLOB… and Save decoded JSON… write a file with a manifest, noted in the activity log.\n\n"
     "View value… (Shift+Enter) shows a long text whole: Find (Ctrl+F, F3 / Shift+F3 for the "
     "next / previous match), Wrap, Line numbers, Pretty-print, Copy; its title names the column, "
     "row, table and database."),
    ("Exports",
     "Every export (Browse, Search, SQL, WAL, BLOBs, Timeline, Relationships, Tagged, Forensic "
     "report, Copy with related, Database Map, schema report, BLOB Inspector saves) states its "
     "provenance: the tool and version, the export time (UTC), the Python and SQLite versions, "
     "each database's files with size, modification time and SHA-256 (waited for or computed), "
     "what was exported, the scope and filters. The row exports run on a worker thread with "
     "progress and Stop. CSV files stay clean: the provenance is in '<file>.manifest.json' "
     "beside them, with the file's own SHA-256; JSON and HTML files carry it inside and get the "
     "manifest too; a folder of BLOB files gets export_manifest.json. A stopped export is kept "
     "and marked incomplete. Every export ends with one message saying what was written, where, "
     "and its manifest (or that the manifest could not be written, also under Issues), and is "
     "noted in the activity log.\n\n"
     "Every HTML export (rows, tagged rows, forensic report, Database Map, schema report, "
     "timeline) is one self-contained report in the same design, readable offline in any "
     "browser, on a phone too: a cover with the evidence files and their SHA-256, a summary "
     "(numbers, an activity chart over time, top values), contents with a search across the "
     "report, and tables that stay fast with hundreds of thousands of rows (drawn as you "
     "scroll; sort, column filters, search with highlights, a row drawer with every value, "
     "copy, download of the rows shown as CSV or JSON, a link that reopens the same view, "
     "light / dark, print). Larger exports are split into parts with an index (limit "
     "html_rows_per_part), and the report says so.\n\n"
     "Values: CSV writes NULL as the word NULL, REAL exactly (repr), text whole; a cell holding "
     "several values (a record's values) is the JSON of them; JSON writes NULL as null. BLOBs: "
     "as hex (lossless, the default), base64 (lossless) or a summary (type, size, SHA-256). "
     "Nothing is written into the evidence folder.\n\n"
     "Spreadsheet-safe CSV: every CSV (exports, tagged rows, timeline, forensic report, "
     "activity log, links, copied rows, the report's CSV download) puts a ' before text that "
     "starts with = + - @, a tab or a carriage return, so a spreadsheet shows it instead of "
     "running it as a formula; numbers are never changed. The export dialog can turn this off "
     "for a CSV; the manifest says which was used. The character NUL is written as \\x00 in "
     "CSV. A CSV that fails while being written is removed (the message says so).\n\n"
     "SQL from the evidence: Copy with related as SQL writes CREATE TABLE statements rebuilt "
     "from the columns (names, declared types, NOT NULL, primary key, WITHOUT ROWID), never "
     "the text stored in the database; anything else is a -- comment. The Database Map and the "
     "schema report show a stored statement up to its end; any text stored after it is shown "
     "as -- comments. The HTML reports run only their own script."),
    ("Evidence safety",
     "The database is never copied and nothing is written next to it: the original is opened "
     "read-only (SQLite's immutable mode) and parsed by the tool itself; a WAL is merged in "
     "memory only. Writing into an evidence folder is refused, also through a junction, a "
     "substituted drive or a network alias of it. In a case that covers the folder a case was "
     "opened from, every folder a database was found in (chosen or not) and the folder of a "
     "database that failed to open; writing over a file that has another name (a hard link) "
     "is refused too.\n\n"
     "Network paths: a case file or the Recent list can name a network path (\\\\server\\"
     "share, a mapped network drive). Checking it would make Windows connect to that computer "
     "with your sign-in, so the tool does not: Recent shows it as 'network path, not checked', "
     "and reopening such a case lists every path first and asks whether to open the network "
     "ones too. A colour in a case file must be #rrggbb; another is replaced and listed under "
     "Issues.\n\n"
     "Database ▾ › Evidence and verification… lists every file with size, modification time "
     "(UTC) and SHA-256, the files next to the database the tool does not use (e.g. "
     "'x.db-wal.bak'), 'Verify now' (on a worker thread, with progress and Stop) and 'Compare "
     "with expected hash…' (paste the acquisition hash or a sha256sum list). Closing verifies "
     "size and modification time, and the SHA-256 again for files up to the limit "
     "verify_rehash_bytes. Closing a case closes its databases at once and verifies their files "
     "afterwards in the background (the header says it is verifying; a re-hash that takes a "
     "while shows a progress window with 'Skip SHA-256 (size and time only)'); the header then "
     "says what was verified, the activity log gets each result and a changed file is warned "
     "about. Quitting verifies before the app exits. Database ▾ › "
     "Activity log… lists what was done with the case (opened with hashes, searches, tags, "
     "exports with their paths and SHA-256, verification, close), kept in the application data "
     "folder and exportable.\n\n"
     "Evidence held by another program: the bytes of SQLite's lock-byte page (at 1 GiB, it "
     "never holds data) that a live SQLite user keeps locked are hashed as zeros, and the "
     "evidence list says so beside the SHA-256; a file another program opened without letting "
     "others read it is reported as 'in use by <program> (PID n)', with what to do (close that "
     "program, or read a copy made with an acquisition tool or a shadow copy)."),
    ("Untrusted files",
     "Every byte of an evidence file is treated as written by an adversary. Nothing in a file "
     "can run code, write a file or change the evidence: the tool never executes SQL a "
     "database supplies with any power. The CREATE statements stored in a database are cut at "
     "the end of their first statement and replayed only in an empty in-memory database that "
     "allows creating that one object and reading its columns, within a step budget "
     "(schema_replay_steps); a CREATE TABLE ... AS SELECT is never run. Views and computed "
     "columns run read-only, without ATTACH, within sql_view_steps steps, values up to "
     "sql_value_bytes (Python 3.11 or later) and sql_window_bytes per grid window; a view that "
     "reaches one stops and the grid says which limit, with its name. On Python 3.8-3.10 a "
     "view may not call functions that build values of any size, nor a recursive query.\n\n"
     "Sizes a file declares (page counts, payload lengths, WAL database sizes, compression "
     "dictionaries, image dimensions) never size memory: the native reader uses what the files "
     "hold, and every cut says so with the limit's name.\n\n"
     "Safe parse (Open ▾ › Open with Safe parse…, kept in the case file): only the tool's own "
     "parser reads the file and SQLite never opens it; views, virtual tables and the SQL tab "
     "are then not available. It is chosen automatically when SQLite cannot read the file or "
     "the built-in check finds a damaged header or schema, and the header chip says 'Safe "
     "parse'. When this Python bundles an old SQLite (Python 3.10 has 3.37.2) a chip says so: "
     "open untrusted files with Safe parse there, or use a newer Python."),
    ("Limits",
     "Every cap of the tool (rows kept, samples, memory, time) is a named limit: Database ▾ › "
     "Limits… lists them in one list with their value, default and what they limit (Find: "
     "narrows the list); the editor below shows the limit selected (double-click or Enter "
     "edits it). A value that is not the default is marked 'changed', a wrong one is red and "
     "says why, and Save refuses to keep wrong values. Defaults puts every default back. Whatever a limit leaves out is "
     "said where it happens, with the limit's name."),
    ("Keyboard shortcuts",
     "Ctrl+O  open a database            Ctrl+F  the search field of the tab or window shown "
     "(the Search tab's when it has none)\n"
     "Ctrl+K  the command palette         Ctrl+B  show or hide the databases panel\n"
     "Dropdowns: type to filter, ↑/↓ choose, Enter pick, Escape close; Alt+Down opens one "
     "(an open list or calendar closes when you move to another field, not when you switch "
     "to another program)\n"
     "Date fields: Alt+Down opens the calendar (arrows move the day, PageUp/PageDown the month, "
     "Shift+PageUp/PageDown the year, Enter picks, Escape closes)\n"
     "Navigator: Enter on a database makes it active, on a table opens it; Shift+F10 its menu\n"
     "In a search field: Enter or F3 next match, Shift+Enter previous, Escape clear\n"
     "Enter   search (in the search entry)   Escape  stop the search (in the Search tab)\n"
     "Ctrl+E  go to the SQL editor       Ctrl+Enter  run the query    Alt+Up/Down  query history\n"
     "Ctrl+T  tag or untag the selected rows (first tag); Ctrl+1..9 tag 1 to 9\n"
     "Delete  remove a tag (Tagged tab)\n"
     "Grids: arrows move, Shift+Up/Down extend the selection, PageUp/PageDown, Home/End and "
     "Ctrl+Home/End first/last row, Ctrl+Left/Right first/last column, Ctrl+A all rows, Ctrl+C "
     "copy the cell, Ctrl+Shift+C copy the rows, Enter open the row detail, Shift+Enter view the "
     "whole value; Escape in a column filter clears it\n"
     "View value: Ctrl+F find, F3 / Shift+F3 next / previous, Escape close"),
]


def help_text():
    """The Help as (title, text) sections (the WAL states as the WAL tab shows them)."""
    states = "\n".join("- %s: %s" % (v[0], v[3]) for v in WAL_STATES.values())
    return [(t, body.replace("@WAL_STATES@", states)) for t, body in HELP]


class HelpDialog(tk.Toplevel):
    """The Help: its sections listed on the left, a Find box, Copy."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("SQLite GUI Analyzer - Help")
        fit_geometry(self, 900, 620)            # never larger than the screen
        place_over(self, self.master)
        self.configure(bg=C["bg"])
        self.transient(parent)
        btnf = ttk.Frame(self)
        btnf.pack(fill="x", padx=8, pady=8, side="bottom")
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(8, 2))
        self.search = SearchBox(top, placeholder="Find in the help…", primary=True,
                                on_change=lambda t: self._find_all(),
                                on_next=lambda f: self.find_next(f), width=32)
        self.search.pack(side="left", fill="x", expand=True)
        self.find_var = self.search.var
        self.find_note = self.search.count_label
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=8, pady=4)
        self.toc = tk.Listbox(body, width=26, activestyle="none",
                              bg=C["bg2"], fg=C["text"],
                              selectbackground=K["selection"], selectforeground=K["text"],
                              relief="flat", font=F["body"],
                              highlightthickness=0)
        self.toc.pack(side="left", fill="y")
        txt = self.text = tk.Text(body, wrap="word", font=F["label"], bg=C["bg"],
                                  fg=C["text"], relief="flat", padx=16, pady=12)
        sb = ttk.Scrollbar(body, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.tag_configure("h1", font=F["display"], foreground=C["accent"],
                          spacing1=8, spacing3=6)
        txt.tag_configure("h2", font=F["title"], foreground=C["text"],
                          spacing1=12, spacing3=4)
        txt.tag_configure("found_all", background=K["find_all"])
        txt.tag_configure("found", background=C["yellow"])
        txt.tag_raise("found")
        txt.insert("end", "SQLite GUI Analyzer v%s\n" % VERSION, "h1")
        self.marks = []
        for title, text in help_text():
            self.marks.append(txt.index("end-1c"))
            txt.insert("end", title + "\n", "h2")
            txt.insert("end", text + "\n")
            self.toc.insert("end", title)
        txt.insert("end", "\nSQLite GUI Analyzer v%s, Python %s, Pillow %s\n" % (
            VERSION, sys.version.split()[0],
            "installed" if HAS_PIL else "not installed (optional: JPEG/WEBP previews)"))
        txt.configure(state="disabled")
        self.toc.bind("<<ListboxSelect>>", self._goto)

        def copy_help():
            self.clipboard_clear()
            self.clipboard_append(txt.get("1.0", "end"))
        ttk.Button(btnf, text="Copy", command=copy_help).pack(side="left", padx=4)
        ttk.Button(btnf, text="Close", command=self.destroy).pack(side="right", padx=4)
        self._from = "1.0"

    def _goto(self, _e=None):
        sel = self.toc.curselection()
        if sel:
            self.text.see(self.marks[sel[0]])
            self.text.yview(self.marks[sel[0]])

    def _find_all(self):
        """Mark every place the words occur, say how many, and go to the first."""
        needle = self.find_var.get().strip()
        self.text.tag_remove("found", "1.0", "end")
        self.text.tag_remove("found_all", "1.0", "end")
        self._hits, self._hits_for = [], needle
        if not needle:
            self.search.set_status("")
            return
        self._find_all_quiet(needle)
        for pos in self._hits:
            self.text.tag_add("found_all", pos, "%s+%dc" % (pos, len(needle)))
        self._from = "1.0"
        if not self._hits:
            self.search.set_status("Not found in the help: “%s”" % needle, error=True)
            return
        self.find_next()

    def find_next(self, forward=True):
        """Highlight the next (previous) place the Find text occurs; says 'N of M'."""
        needle = self.find_var.get().strip()
        if not needle:
            self.search.set_status("")
            return None
        if getattr(self, "_hits_for", None) != needle:
            self._hits_for = needle
            self._find_all_quiet(needle)
        if not self._hits:
            self.search.set_status("Not found in the help: “%s”" % needle, error=True)
            return None
        self._at = (self._at + (1 if forward else -1)) % len(self._hits)
        pos = self._hits[self._at]
        end = "%s+%dc" % (pos, len(needle))
        self.text.tag_remove("found", "1.0", "end")
        self.text.tag_add("found", pos, end)
        self.text.see(pos)
        self._from = end
        self.search.set_status("%d of %d" % (self._at + 1, len(self._hits)))
        return pos

    def _find_all_quiet(self, needle):
        self._hits, pos = [], "1.0"
        while True:
            pos = self.text.search(needle, pos, stopindex="end", nocase=True)
            if not pos:
                break
            self._hits.append(pos)
            pos = "%s+%dc" % (pos, len(needle))
        self._at = -1


# ── TextWindow ───────────────────────────────────────────────────────────
class TextWindow(tk.Toplevel):
    """A read-only, scrollable text (errors, verification reports, lists) with Copy: never a
    message box cut at a few lines."""

    def __init__(self, parent, title, intro, body, geometry="760x420"):
        tk.Toplevel.__init__(self, parent)
        self.title(title)
        self.geometry(geometry)
        self.configure(bg=C["bg"])
        self.transient(parent)
        if intro:
            lbl = tk.Label(self, text=intro, bg=C["bg"], fg=C["text"], anchor="w",
                           justify="left", font=F["body"])
            lbl.pack(fill="x", padx=8, pady=(8, 4))
            lbl.bind("<Configure>", lambda e: lbl.configure(wraplength=max(100, e.width - 8)))
        box = ttk.Frame(self)
        box.pack(fill="both", expand=True, padx=8)
        self.text = tk.Text(box, wrap="word", font=F["mono"], bg=C["bg2"], relief="flat")
        sb = ttk.Scrollbar(box, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True)
        self.text.insert("1.0", body)
        self.text.configure(state="disabled")
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=8)

        def copy():
            self.clipboard_clear()
            self.clipboard_append(body)
        ttk.Button(bar, text="Copy", command=copy).pack(side="left")
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right")

    def body(self):
        return self.text.get("1.0", "end-1c")


# ── ValuesWindow ─────────────────────────────────────────────────────────
class ValuesWindow(tk.Toplevel):
    """Row detail of a row that is no table row (a query result, a record known only from a
    search hit): where it comes from, each column's value and storage class; double-click a
    BLOB for the BLOB Inspector, a long text for View value; copies keep every value
    (JSON with BLOBs as hex)."""

    def __init__(self, app, title, source, columns, values, match_col=""):
        tk.Toplevel.__init__(self, app)
        self.app = app
        self.title(title)
        fit_geometry(self, 780, 480)
        place_over(self, app)
        self.configure(bg=C["bg"])
        self.transient(app)
        self.columns = list(columns) + ["col%d" % i for i in range(len(columns), len(values))]
        self.values = list(values)
        self.source = source
        head = tk.Label(self, text=source, bg=C["acl"], fg=C["accent"], anchor="w",
                        justify="left", font=F["body_bold"], padx=10, pady=6)
        head.pack(fill="x")
        head.bind("<Configure>", lambda e: head.configure(wraplength=max(100, e.width - 20)))
        fbar = ttk.Frame(self)
        fbar.pack(fill="x", padx=8, pady=(6, 0))
        self.find = SearchBox(fbar, placeholder="Find a column or value…", delay=100,
                              primary=True, width=30)
        self.find.pack(side="left", fill="x", expand=True)
        box = ttk.Frame(self)
        box.pack(fill="both", expand=True, padx=8, pady=6)
        self.tree = ttk.Treeview(box, columns=("col", "value", "type"), show="headings")
        for c, text, w in (("col", "Column", 180), ("value", "Value", 460), ("type", "Type", 90)):
            self.tree.heading(c, text=text)
            self.tree.column(c, width=w, stretch=(c == "value"))
        ysb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("match", background=C["hl"])
        from engine.search import value_type
        for i, (c, v) in enumerate(zip(self.columns, self.values)):
            self.tree.insert("", "end", iid=str(i), tags=("match",) if c == match_col else (),
                             values=(c, vb_text(v), value_type(v)))
        self.filter = TreeFilter(self.tree, self.find, "column", "columns")
        self.tree.bind("<Double-1>", self._open_value)
        self.tree.bind("<Button-3>", self._menu)
        self.tree.bind("<Control-c>", lambda e: self._copy_line("both"))
        ttk.Label(self, text="Double-click a BLOB to inspect it, a long text to view it whole "
                             "(select any part there); right-click to copy a name or value.",
                  style="M.TLabel").pack(anchor="w", padx=10)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=6)
        ttk.Button(bar, text="Copy as JSON", command=self._copy_json).pack(side="left")
        ttk.Button(bar, text="Copy as text", command=self._copy_text).pack(side="left", padx=4)
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right")

    def _menu(self, e):
        """Right-click a line: copy its column name, its value, or both."""
        iid = self.tree.identify_row(e.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Copy value", command=lambda: self._copy_line("value"))
        m.add_command(label="Copy column name", command=lambda: self._copy_line("name"))
        m.add_command(label="Copy name: value", command=lambda: self._copy_line("both"))
        m.add_separator()
        m.add_command(label="View value…", command=self._open_value)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _copy_line(self, what):
        """Copy the selected line's column name, value (whole: BLOBs as hex) or both."""
        from engine.export import csv_cell
        sel = self.tree.selection()
        if not sel:
            return "break"
        i = int(sel[0])
        name, value = self.columns[i], csv_cell(self.values[i])
        text = {"name": name, "value": value}.get(what, "%s: %s" % (name, value))
        self.clipboard_clear()
        self.clipboard_append(text)
        return "break"

    def _open_value(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            return
        i = int(sel[0])
        v = self.values[i]
        if isinstance(v, (bytes, bytearray)) and not isinstance(v, InvalidText):
            BlobViewer(self, bytes(v), self.columns[i], "%s (%s)" % (self.columns[i],
                                                                     self.source))
        elif isinstance(v, str):
            from value_viewer import ValueViewer
            ValueViewer(self, v, "%s (%s)" % (self.columns[i], self.source))

    def _copy_json(self):
        from engine.export import json_cell
        d = {"source": self.source,
             "values": dict((c, json_cell(v)) for c, v in zip(self.columns, self.values))}
        self.clipboard_clear()
        self.clipboard_append(json.dumps(d, indent=2, ensure_ascii=False, default=str))

    def _copy_text(self):
        from engine.export import csv_cell
        self.clipboard_clear()
        self.clipboard_append("\n".join([self.source] + [
            "%s: %s" % (c, csv_cell(v, formulas=False)) for c, v in zip(self.columns,
                                                                        self.values)]))


FIND_TEXT_CHARS = 100000    # characters of a long text value a row window's Find looks in
FIND_HEX_BYTES = 512        # leading bytes of a BLOB it looks in (as hex), after its summary


def find_text(v):
    """What a row window's Find looks in for a value: the text (up to FIND_TEXT_CHARS),
    NULL, the number, or a BLOB's summary and its first FIND_HEX_BYTES bytes as hex."""
    if v is None:
        return "NULL"
    if isinstance(v, (bytes, bytearray)):
        b = bytes(v)
        return "%s %s" % (blob_summary(b), binascii.hexlify(b[:FIND_HEX_BYTES]).decode())
    return str(v)[:FIND_TEXT_CHARS]


def vb_text(v):
    """A value as the row windows list it (vb, one line)."""
    from utils import vb
    return vb(v).replace("\r\n", " ↵ ").replace("\n", " ↵ ")


# ── ScopeDlg ─────────────────────────────────────────────────────────────
class ScopeDlg(tk.Toplevel):
    """The one 'Search scope' dialog: choose the tables (and views) a search covers, of one
    database or, in a case, of each database searched (listed under its name, which ticks or
    clears all of its tables listed). result: the chosen names (one database) or {database
    key: names} (a case), or None when cancelled.

    Views are listed in their own section; they are searched only when the Search tab's
    'Include views' is on. When the selection names no view, every view starts selected.
    'Hide empty' hides only tables counted as empty, never one still being counted."""

    def __init__(self, parent, tables=None, counts=None, selected=None, views=(),
                 groups=None):
        super().__init__(parent)
        self.title("Search scope")
        self.configure(bg=C["bg"])
        self.transient(parent)
        self.result = None
        if groups is None:          # one database
            groups = [{"key": None, "name": "", "tables": list(tables or ()),
                       "views": list(views), "counts": counts if counts is not None else {},
                       "selected": list(selected or ())}]
        self._groups = groups
        self._multi = len(groups) > 1 or groups[0]["key"] is not None
        self.geometry("560x600" if self._multi else "450x550")
        self._vars = {}             # key -> BooleanVar; key: name, or (group key, name)
        self._kinds = {}            # key -> (group, name, is a view)
        self._visible_tables = []   # keys listed now
        for g in groups:
            chosen = set(g["selected"])
            any_view = any(v in chosen for v in g["views"])
            for t in g["tables"]:
                k = self._key(g, t)
                self._vars[k] = tk.BooleanVar(value=(t in chosen))
                self._kinds[k] = (g, t, False)
            for v in g["views"]:
                k = self._key(g, v)
                self._vars[k] = tk.BooleanVar(value=(v in chosen or not any_view))
                self._kinds[k] = (g, v, True)

        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(8, 4))
        bot = ttk.Frame(self)
        bot.pack(fill="x", padx=8, pady=8, side="bottom")
        self.search = SearchBox(top, placeholder="Find a table…", delay=0, find_button=False,
                                on_change=lambda t: self._rebuild_list(), primary=True,
                                width=22)
        self.search.pack(side="left", padx=(0, 6), fill="x", expand=True)
        self._filter_var = self.search.var
        self._hide_empty = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Hide empty", variable=self._hide_empty,
                        command=self._rebuild_list).pack(side="left")
        # the buttons act on the tables listed now (the filter and Hide empty apply)
        btnrow = ttk.Frame(self)
        btnrow.pack(fill="x", padx=8, pady=4)
        for text, cmd in (("All listed", self._sel_all), ("None listed", self._sel_none),
                          ("Invert", self._sel_invert), ("Only with rows", self._sel_nonempty)):
            ttk.Button(btnrow, text=text, command=cmd, style="Sm.TButton").pack(side="left",
                                                                               padx=2)
        self._stats_label = ttk.Label(self, text="", style="M.TLabel")
        self._stats_label.pack(fill="x", padx=8, pady=(0, 2))
        self._refresh_after = None
        self._counts_seen = None

        container = ttk.Frame(self)
        container.pack(fill="both", expand=True, padx=8, pady=4)
        self._canvas = tk.Canvas(container, bg=C["bg"], highlightthickness=0)
        sb = ttk.Scrollbar(container, orient="vertical", command=self._canvas.yview)
        self._inner = ttk.Frame(self._canvas)
        self._inner.bind("<Configure>",
                         lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas_win = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._canvas.configure(yscrollcommand=sb.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._canvas.bind("<Configure>",
                          lambda e: self._canvas.itemconfigure(self._canvas_win, width=e.width))

        def _scope_scroll(e):
            try:
                self._canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
            except Exception:
                pass
        self._scope_scroll_fn = _scope_scroll
        self._canvas.bind("<MouseWheel>", _scope_scroll)
        self._inner.bind("<MouseWheel>", _scope_scroll)

        self._summary = ttk.Label(self, style="M.TLabel")
        self._summary.pack(fill="x", padx=8, after=bot, side="bottom")
        ttk.Button(bot, text="Apply", style="P.TButton", command=self._apply).pack(
            side="right", padx=4)
        ttk.Button(bot, text="Cancel", command=self.destroy).pack(side="right", padx=4)
        self._rebuild_list()
        self._watch_counts()
        try:
            self.grab_set()
        except tk.TclError:
            pass

    @classmethod
    def for_case(cls, parent, members):
        """The dialog for the databases of a case (each a group under its name); result
        {member uid: names}."""
        groups = []
        for m in members:
            tables, views = list(m.db.tables()), list(m.db.views())
            groups.append({"key": m.uid, "name": m.name, "tables": tables, "views": views,
                           "counts": m.counts,
                           "selected": list(m.scope_tables or ()) or tables + views})
        return cls(parent, groups=groups)

    def _key(self, g, name):
        return name if not self._multi else (g["key"], name)

    def _get_count(self, key):
        """A table's row count: an int when counted (an estimate '~N' counts as N), None
        while it is still being counted ('?')."""
        g, name, _view = self._kinds[key] if key in self._kinds else (self._groups[0], key, 0)
        raw = g["counts"].get(name)
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str) and raw.startswith("~"):
            n = _int_count(raw, None)
            return n if n else None         # '~0' is no proof of an empty table
        return None

    def _tables(self):
        return [k for k, (_g, _n, view) in self._kinds.items() if not view]

    def _stats_text(self):
        tables = self._tables()
        counts = [self._get_count(k) for k in tables]
        empty = sum(1 for c in counts if c == 0)
        unknown = sum(1 for c in counts if c is None)
        text = "Total: %d tables%s  |  %d with rows  |  %d empty" % (
            len(tables), (" in %d databases" % len(self._groups)) if self._multi else "",
            len(tables) - empty - unknown, empty)
        if unknown:
            text += "  |  %d not counted yet (shown; counting…)" % unknown
        return text

    def _watch_counts(self):
        """Counts still running: refresh the list when they change (the counts dicts are
        the databases' own, filled by the row-count worker)."""
        self._refresh_after = None
        if not self.winfo_exists():
            return
        seen = tuple(repr(self._get_count(k)) for k in self._tables())
        if seen != self._counts_seen:
            if self._counts_seen is not None:
                self._rebuild_list()
            self._counts_seen = seen
        if any(self._get_count(k) is None for k in self._tables()):
            self._refresh_after = self.after(1000, self._watch_counts)

    def destroy(self):
        if self._refresh_after is not None:
            try:
                self.after_cancel(self._refresh_after)
            except tk.TclError:
                pass
            self._refresh_after = None
        tk.Toplevel.destroy(self)

    def _listed(self, g):
        """(tables, views) of a group listed now: the filter (on the name, or in a case on
        the database's name too) and Hide empty apply."""
        filt = self._filter_var.get().lower().strip()
        he = self._hide_empty.get()
        in_db = bool(filt) and self._multi and filt in g["name"].lower()
        tables = [t for t in g["tables"]
                  if (not filt or in_db or filt in t.lower())
                  and not (he and self._get_count(self._key(g, t)) == 0)]
        views = [v for v in g["views"] if not filt or in_db or filt in v.lower()]
        return tables, views

    def _check(self, key, text):
        cb = ttk.Checkbutton(self._inner, text=text, variable=self._vars[key],
                             command=self._update_summary)
        cb.pack(anchor="w", pady=1, padx=(20 if self._multi else 4, 4))
        cb.bind("<MouseWheel>", self._scope_scroll_fn)
        return cb

    def _rebuild_list(self):
        for w in self._inner.winfo_children():
            w.destroy()
        self._stats_label.configure(text=self._stats_text())
        self._visible_tables = []
        self._headers = []
        for g in self._groups:
            tables, views = self._listed(g)
            keys = [self._key(g, n) for n in tables + views]
            if self._multi:
                ticked = sum(1 for k in keys if self._vars[k].get())
                hdr = ttk.Label(self._inner, text="%s  (%d of %d listed ticked: click to tick "
                                                  "or clear them)" % (g["name"], ticked,
                                                                      len(keys)),
                                style="B.TLabel", cursor="hand2")
                hdr.pack(anchor="w", pady=(6, 1), padx=4)
                hdr.bind("<Button-1>", lambda e, ks=keys: self._toggle(ks))
                hdr.bind("<MouseWheel>", self._scope_scroll_fn)
                self._headers.append(hdr)
            for t in tables:
                k = self._key(g, t)
                cnt = self._get_count(k)
                self._check(k, "%s  (%s)" % (t, fmt_count(g["counts"].get(t, "?"))
                                             if cnt is not None else "counting…"))
                self._visible_tables.append(k)
            if views:
                lbl = ttk.Label(self._inner, text="Views (searched when 'Include views' is on)",
                                style="M.TLabel" if self._multi else "B.TLabel")
                lbl.pack(anchor="w", pady=(4, 1), padx=(20 if self._multi else 4, 4))
                lbl.bind("<MouseWheel>", self._scope_scroll_fn)
                for v in views:
                    k = self._key(g, v)
                    self._check(k, v)
                    self._visible_tables.append(k)
        self.search.set_count(len(self._visible_tables), len(self._kinds), "table",
                              "tables and views", "table or view")
        self._update_summary()

    def group_titles(self):
        """The database headers listed now (a case)."""
        return [h.cget("text") for h in getattr(self, "_headers", [])]

    def _toggle(self, keys):
        on = not all(self._vars[k].get() for k in keys)
        for k in keys:
            self._vars[k].set(on)
        self._rebuild_list()

    def visible_views(self):
        return [self._kinds[k][1] for k in self._visible_tables if self._kinds[k][2]]

    def _sel_all(self):
        for k in self._visible_tables:
            self._vars[k].set(True)
        self._update_summary()

    def _sel_none(self):
        for k in self._visible_tables:
            self._vars[k].set(False)
        self._update_summary()

    def _sel_invert(self):
        for k in self._visible_tables:
            self._vars[k].set(not self._vars[k].get())
        self._update_summary()

    def _sel_nonempty(self):
        """Tick the tables with rows; a table still being counted stays ticked, and views
        (whose row counts are not known here) are left as they are."""
        for k in self._visible_tables:
            if not self._kinds[k][2]:
                cnt = self._get_count(k)
                self._vars[k].set(cnt is None or cnt > 0)
        self._update_summary()

    def _update_summary(self):
        sel = sum(1 for v in self._vars.values() if v.get())
        vis = len(self._visible_tables)
        vis_sel = sum(1 for k in self._visible_tables if self._vars[k].get())
        self._summary.configure(text="%d of %d listed ticked  |  %d of %d ticked in all" % (
            vis_sel, vis, sel, len(self._vars)))
        if self._multi:
            for hdr, g in zip(getattr(self, "_headers", []), self._groups):
                tables, views = self._listed(g)
                keys = [self._key(g, n) for n in tables + views]
                hdr.configure(text="%s  (%d of %d listed ticked: click to tick or clear "
                                   "them)" % (g["name"], sum(1 for k in keys
                                                             if self._vars[k].get()),
                                              len(keys)))

    def _apply(self):
        if self._multi:
            self.result = dict((g["key"], [n for n in g["tables"] + g["views"]
                                           if self._vars[self._key(g, n)].get()])
                               for g in self._groups)
        else:
            self.result = [self._kinds[k][1] for k, v in self._vars.items() if v.get()]
        self.destroy()

    apply = _apply


# ── BlobViewer ───────────────────────────────────────────────────────────
def BlobViewer(parent, data, col_name="BLOB", context=""):
    """Open the BLOB inspector (decoded tree, hex, text, timestamps, image) for `data`."""
    return BlobInspector(parent, data, col_name, context)


# ── RowWin ───────────────────────────────────────────────────────────────
ROWWIN_BATCH = 20       # column lines a row window builds per turn of the event loop
ROWWIN_CLOSE_BATCH = 15  # ... and destroys per turn once it is closed


def _paint_name(entry, bg, fg):
    """Colour a row window's column name (a read-only Entry, so it can be selected)."""
    try:
        entry.configure(readonlybackground=bg, bg=bg, fg=fg)
    except tk.TclError:
        entry.configure(bg=bg, fg=fg)


def _binary_text(s):
    """True for text that holds binary data (control characters other than tab and line
    breaks): a serialized protobuf kept in a TEXT column, for instance."""
    sample = s[:512]
    return any(ord(ch) < 32 and ch not in "\t\n\r" for ch in sample) or "\x7f" in sample


def escaped_text(s, limit):
    """s with control characters as \\xNN, cut at `limit` characters with an ellipsis."""
    out = []
    for ch in s[:limit]:
        o = ord(ch)
        out.append(ch if o >= 32 and o != 0x7f else "\\x%02x" % o)
    return "".join(out) + ("…" if len(s) > limit else "")


class RowWin(tk.Toplevel):
    _pool = {}

    @staticmethod
    def pool_key(tbl, rid, db=None):
        """Window identity for a row (of one database: two databases of a case can hold the
        same table and row id). Ordinal locators (views) are positions in one particular
        read, so '#0' of a sorted view is a different row than '#0' of the unsorted view: key
        them by the row snapshot they carry (kept alive by the open window)."""
        snap = getattr(rid, "snapshot", None)
        base = (tbl, rid) if db is None else (id(db), tbl, rid)
        return base if snap is None else base + (id(snap),)

    @classmethod
    def show(cls, parent, db, tbl, rid, search_term="", match_col=""):
        key = cls.pool_key(tbl, rid, db)
        if key in cls._pool:
            try:
                w = cls._pool[key]
                w._search_term = search_term
                w._match_col = match_col
                w.lift()
                # Re-highlight if search term changed
                if search_term:
                    w._highlight_match()
                return w
            except Exception:
                pass
        w = cls(parent, db, tbl, rid, search_term, match_col)
        cls._pool[key] = w
        return w

    def __init__(self, parent, db, tbl, rid, search_term="", match_col=""):
        super().__init__(parent)
        # in a case of several databases the title says which one the row is from
        case = getattr(parent, "case", None)
        member = case.of_db(db) if case is not None and getattr(case, "multi", False) else None
        self._where = member.label(tbl) if member is not None else tbl
        rid_text = rid.display() if hasattr(rid, "display") else str(rid)
        dbname = member.name if member is not None else os.path.basename(
            getattr(getattr(db, "evidence", None), "main", "") or "")
        self._source_text = "Database %s (%s) › table %s › row %s" % (
            dbname, mode_label(getattr(db, "mode", None)), tbl, rid_text)
        self.title("Row detail — %s, row %s%s" % (tbl, rid_text,
                                                  " (%s)" % dbname if dbname else ""))
        fit_geometry(self, 720, 500)
        place_over(self, parent)
        self.configure(bg=C["bg"])
        self._db = db
        self._tbl = tbl
        self._rid = rid
        self._search_term = search_term
        self._match_col = match_col
        self._tk_imgs = []
        self._col_widgets = {}  # col_name -> (row_frame, value_widget)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Bottom toolbar — grouped, styled buttons
        bot = tk.Frame(self, bg=C["bg3"], bd=0)
        bot.pack(side="bottom", fill="x")
        tk.Frame(bot, bg=C["border"], height=1).pack(fill="x", side="top")

        bot_inner = tk.Frame(bot, bg=C["bg3"])
        bot_inner.pack(fill="x", padx=6, pady=4)

        btn_cfg = dict(font=F["small"], relief="flat", bd=0, highlightthickness=0,
                       cursor="hand2", padx=8, pady=3)

        # Close first, on the right: a narrow window wraps the other groups, never squashes it
        tk.Button(bot_inner, text="Close", command=self._on_close,
                  bg=C["bg4"], fg=C["text"], activebackground=C["border"],
                  **btn_cfg).pack(side="right", padx=1, anchor="n")
        flow = FlowFrame(bot_inner)
        flow.pack(side="left", fill="x", expand=True)
        outer_bar = bot_inner
        bot_inner = flow

        # Copy group
        grp1 = tk.Frame(bot_inner, bg=C["bg3"])
        bot_inner.add(grp1, gap=0)
        tk.Label(grp1, text="Copy:", font=F["tiny"], fg=C["text2"],
                 bg=C["bg3"]).pack(side="left", padx=(0, 2))
        for txt, cmd in [("JSON", self._copy_json), ("CSV", self._copy_csv),
                         ("Text", self._copy_text)]:
            b = tk.Button(grp1, text=txt, command=cmd, bg=C["bg"], fg=C["accent"],
                          activebackground=C["acl"], **btn_cfg)
            b.pack(side="left", padx=1)

        # Schema group
        grp2 = tk.Frame(bot_inner, bg=C["bg3"])
        bot_inner.add(grp2, gap=12)
        tk.Label(grp2, text="Schema:", font=F["tiny"], fg=C["text2"],
                 bg=C["bg3"]).pack(side="left", padx=(0, 2))
        for txt, cmd in [("CREATE SQL", self._copy_create_sql), ("Schema", self._copy_schema_text)]:
            b = tk.Button(grp2, text=txt, command=cmd, bg=C["bg"], fg=C["purple"],
                          activebackground=C["bg2"], **btn_cfg)
            b.pack(side="left", padx=1)

        # Export group
        b = tk.Button(bot_inner, text="Export BLOBs", command=self._export_blobs,
                      bg=C["bg"], fg=C["green"], activebackground=C["gl"], **btn_cfg)
        bot_inner.add(b, gap=12)
        # the row and the rows related to it (datamap_ui), for a row with a key
        dmu = getattr(parent, "datamap", None)
        case = getattr(parent, "case", None)
        rw_member = case.of_db(db) if case is not None and hasattr(case, "of_db") else None
        if dmu is not None and (case is None or rw_member is not None) and \
                dmu.offers(tbl, [rid], rw_member):
            bot_inner.add(tk.Button(bot_inner, text="Copy with related",
                                    command=lambda: dmu.copy_with_related(tbl, [rid],
                                                                          rw_member),
                                    bg=C["bg"], fg=C["purple"], activebackground=C["acl"],
                                    **btn_cfg), gap=6)
        bot_inner = outer_bar

        # Find: the columns whose name or value holds every word stay listed
        fbar = tk.Frame(self, bg=C["bg"])
        fbar.pack(side="top", fill="x", padx=6, pady=(6, 2))
        self.find = SearchBox(fbar, placeholder="Find a column or value…", delay=150,
                              primary=True, width=30,
                              on_change=lambda _t: self._filter(),
                              on_next=self._find_next,
                              tooltip="Type to list only the columns whose name or value "
                                      "holds the words; Enter goes to the next one; Escape "
                                      "or × lists every column again (Ctrl+F comes here).")
        self.find.pack(side="left", fill="x", expand=True)
        self._find_at = -1
        self._keys = {}             # line widget -> (column, its text for Find)

        # Scrollable body
        outer = tk.Frame(self, bg=C["bg"])
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=C["bg"], highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        # Every line of the row is its own window on the canvas (stacked by _place): the
        # canvas maps only the lines in view, so a row of hundreds of columns creates native
        # windows for one screenful instead of all of them (seconds with the window frozen).
        self._body = canvas
        self._lines = []            # (widget, canvas item, y, height)
        self._pads = {}             # canvas item -> its (top, bottom) padding
        self._pending = []          # lines made, not placed yet
        self._stack_h = 0
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)

        def _width(e):
            for _w, item, _y, _h, padx in self._lines:
                canvas.itemconfigure(item, width=max(1, e.width - 2 * padx))
            self._set_region(e.width)
        canvas.bind("<Configure>", _width)
        self._rw_canvas = canvas
        def _rw_scroll(e):
            try:
                if self._stack_h <= canvas.winfo_height():
                    canvas.yview_moveto(0)      # it all fits: nothing to scroll
                else:
                    canvas.yview_scroll(int(-3 * (e.delta / 120)), "units")
            except Exception:
                pass
            return "break"
        self._scroll_fn = _rw_scroll

        self._populate()

    def _count_related(self, rel, asks):
        """Count the rows of other tables holding each value on a worker thread; a
        'Related (N)' button appears beside each value that has some."""
        from grid import Runner
        app = self.master
        runner = self._related_runner = Runner(
            self, "row-related", release=getattr(app, "_release_worker_connection", None))
        tbl = self._tbl

        def work():
            out = []
            for row_f, key, kval in asks:
                try:
                    n = sum(c for _r, c in rel.related_counts(tbl, key, kval))
                except Exception:       # noqa: BLE001 - no button for that value
                    n = 0
                out.append((row_f, key, kval, n))
            return out

        def done(result, error):
            if error is not None or not self.winfo_exists():
                return
            for row_f, key, kval, n in result:
                if n and row_f.winfo_exists():
                    tk.Button(row_f, text="Related (%s)" % format(n, ","),
                              font=F["tiny_bold"],
                              fg=C["purple"], bg=C["bg2"], activebackground=C["acl"],
                              relief="flat", bd=0, highlightthickness=0,
                              cursor="hand2", padx=4, pady=1,
                              command=lambda k=key, v=kval: rel.related(tbl, k, v)
                              ).grid(row=0, column=3, sticky="ne", padx=2, pady=1)
        runner.submit("related", work, done)

    def _set_region(self, width=None):
        """The scroll region: the stacked lines, and never shorter than the window (a
        shorter region lets a scroll push the lines down, leaving a gap above them)."""
        cv = self._rw_canvas
        if width is None:
            width = max(1, cv.winfo_width())
        cv.configure(scrollregion=(0, 0, width, max(self._stack_h, cv.winfo_height())))

    def _line(self, widget, padx=0, pady=(0, 0)):
        """Queue one line of the window (placed by _place, below the lines before it)."""
        self._pending.append((widget, padx, pady))

    def _place(self):
        """Stack the queued lines on the canvas under the ones placed already, each as its
        own canvas window at its requested height (worked out before it is placed, so no
        line is mapped until it scrolls into view)."""
        if not self._pending:
            return
        cv = self._rw_canvas
        cv.update_idletasks()
        width = cv.winfo_width()
        if width <= 1:
            width = max(cv.winfo_reqwidth(), 1)
        for w, padx, pady in self._pending:
            y = self._stack_h + pady[0]
            h = w.winfo_reqheight()
            item = cv.create_window(padx, y, window=w, anchor="nw",
                                    width=max(1, width - 2 * padx))
            self._lines.append((w, item, y, h, padx))
            self._pads[item] = pady
            self._stack_h = y + h + pady[1]
        self._pending = []
        self._set_region(width)
        if self.find.get():
            self._filter()              # lines built while a Find is typed

    def _matches(self, w, words):
        key = self._keys.get(w)
        if key is None:
            return True                 # where the row is from, its warnings: always shown
        return all(x in key[1] for x in words)

    def _filter(self):
        """List only the columns whose name or value holds every word of the Find field
        (the lines are stacked again without the others)."""
        words = self.find.get().lower().split()
        cv = self._rw_canvas
        y, n, total = 0, 0, 0
        lines = []
        for w, item, _y, h, padx in self._lines:
            pady = self._pads.get(item, (0, 0))
            show = self._matches(w, words)
            if w in self._keys:
                total += 1
                n += show
            if show:
                y += pady[0]
                cv.coords(item, padx, y)
                cv.itemconfigure(item, state="normal")
                lines.append((w, item, y, h, padx))
                y += h + pady[1]
            else:
                cv.itemconfigure(item, state="hidden")
                lines.append((w, item, -1, h, padx))
        self._lines = lines
        self._stack_h = y
        self._set_region()
        cv.yview_moveto(0)
        self._find_at = -1
        self.find.set_count(n, total, "column", "columns")
        return n

    def shown_columns(self):
        """The columns listed now (Find hides the others)."""
        return [self._keys[w][0] for w, _i, y, _h, _p in self._lines
                if w in self._keys and y >= 0]

    def _find_next(self, forward=True):
        """Enter in Find: scroll to the next column listed and mark its name."""
        shown = [(w, y) for w, _i, y, _h, _p in self._lines if w in self._keys and y >= 0]
        if not shown:
            return None
        self._find_at = (self._find_at + (1 if forward else -1)) % len(shown)
        w, y = shown[self._find_at]
        if self._stack_h > 0:
            self._rw_canvas.yview_moveto(max(0.0, min(1.0, (y - 4) / float(self._stack_h))))
        col = self._keys[w][0]
        for c, (_row_f, lbl, _v) in self._col_widgets.items():
            _paint_name(lbl, C["hl"] if c == col else lbl.master.cget("bg"),
                        C["hbg"] if c == col else C["accent"])
        self.find.set_status("%d of %d" % (self._find_at + 1, len(shown)))
        return col

    def _populate(self):
        # where the row is read from, always said
        self._line(tk.Label(self._body, text=self._source_text, anchor="w", justify="left",
                 wraplength=660, bg=C["bg"], fg=C["text2"],
                 font=F["small"]), 6, (4, 2))
        data, cols = self._db.full_row(self._tbl, self._rid)
        if not data:
            self._line(tk.Label(self._body, text="Row not found: no row with this key in table %s now "
                                      "(%s). It may have been read from a view or a filter "
                                      "that changed since." % (self._tbl, self._source_text),
                     fg=C["red"], bg=C["bg"], wraplength=660, justify="left",
                     anchor="w"), 6, (6, 6))
            self._place()
            return
        self._row_data = data
        self._row_cols = cols
        # the App's column relationships (relations_view.RelationWindows)
        rel = getattr(self.master, "relations", None)
        supported = rel is not None and rel.supported(self._tbl)
        related_asks = []           # (row frame, key column, value): counted on a worker
        for flag in sorted(getattr(data, "flags", None) or ()):
            if flag in ROW_FLAGS:            # what the engine knows about this row
                self._line(tk.Label(self._body, text="⚠ " + ROW_FLAGS[flag][1], anchor="w",
                         justify="left", wraplength=660, bg=C["bg"],
                         fg=C["red"] if ROW_FLAGS[flag][2] == "flag_damaged" else C["orange"],
                         font=F["body_bold"]), 6, (4, 0))
        def add(i, col):
            """One column's line: its name, its value (with dates, BLOB tools), Copy."""
            val = data.get(col)
            bg = C["bg"] if i % 2 == 0 else C["alt"]
            row_f = tk.Frame(self._body, bg=bg, bd=0)
            self._line(row_f)
            row_f.columnconfigure(1, weight=1)

            # Column name - full name, no truncation; selectable (Ctrl+C copies it)
            col_lbl = tk.Entry(row_f, font=F["mono_small_bold"], fg=C["accent"],
                               readonlybackground=bg, bg=bg, relief="flat", bd=0,
                               highlightthickness=0, width=len(col) + 1)
            col_lbl.insert(0, col)
            col_lbl.configure(state="readonly")
            col_lbl.grid(row=0, column=0, sticky="nw", padx=(4, 2), pady=1)

            # Value
            vf = tk.Frame(row_f, bg=bg)
            vf.grid(row=0, column=1, sticky="nsew", padx=(0, 2), pady=1)
            val_widget = None  # Track widget for highlighting

            if val is None:
                tk.Label(vf, text="NULL", fg=C["text2"], bg=bg,
                         font=F["italic"]).pack(side="left")
            elif isinstance(val, InvalidText):
                tk.Label(vf, text=f"⚠ invalid text ({fmtb(len(val))}, not valid in the DB encoding)",
                         fg=C["red"], bg=bg, font=F["small_bold"]).pack(side="left")
                tk.Button(vf, text="Inspect bytes…", font=F["tiny"], padx=2, pady=0,
                          command=lambda v=val, c=col: BlobViewer(self, bytes(v), c, self._blob_context(c))
                          ).pack(side="left", padx=2)
            elif isinstance(val, bytes):
                what = blob_summary(val)
                if len(what) > 90:
                    what = what[:90] + "…"
                tk.Label(vf, text=f"{what}  ({fmtb(len(val))})",
                         fg=C["orange"], bg=bg, font=F["small_bold"]).pack(side="left")
                tk.Button(vf, text="Inspect BLOB…", font=F["tiny"], padx=2, pady=0,
                          command=lambda v=val, c=col: BlobViewer(self, v, c, self._blob_context(c))
                          ).pack(side="left", padx=2)
                tk.Button(vf, text="Export", font=F["tiny"], padx=2, pady=0,
                          command=lambda v=val, c=col: self._export_single(v, c)).pack(side="left", padx=2)
                if is_image(val) and HAS_PIL:
                    try:
                        pimg = previews.thumbnail(val, (80, 80))
                        tkimg = ImageTk.PhotoImage(pimg)
                        self._tk_imgs.append(tkimg)
                        tk.Label(vf, image=tkimg, bg=bg).pack(side="left", padx=4)
                    except previews.PreviewRefused as e:
                        tk.Label(vf, text=str(e), fg=C["text2"], bg=bg,
                                 font=F["tiny"]).pack(side="left", padx=4)
                    except Exception:   # noqa: BLE001 - no thumbnail for data Pillow cannot read
                        pass
            elif isinstance(val, str) and _binary_text(val):
                packed = val.encode("utf-8", "surrogateescape")
                what = blob_summary(packed)
                if len(what) > 90:
                    what = what[:90] + "…"
                tk.Label(vf, text="text holding binary data: %s  (%s)" % (what, fmtb(len(packed))),
                         fg=C["orange"], bg=bg, font=F["small_bold"]).pack(side="left")
                tk.Button(vf, text="Inspect…", font=F["tiny"], padx=2, pady=0,
                          command=lambda v=packed, c=col: BlobViewer(self, v, c,
                                                                   self._blob_context(c))
                          ).pack(side="left", padx=2)
                shown = escaped_text(val, 120)
                e = tk.Entry(vf, font=F["mono_small"], bg=bg, fg=C["text2"], relief="flat",
                             bd=0, highlightthickness=0, readonlybackground=bg,
                             width=min(len(shown) + 1, 60))
                e.insert(0, shown)
                e.configure(state="readonly")
                e.pack(side="left", padx=4)
                val_widget = e
            else:
                sv = str(val)
                is_multiline = '\n' in sv or '\r' in sv
                if is_multiline or len(sv) > 300:
                    # Multi-line or long text: Text widget with scrollbar
                    txt_frame = tk.Frame(vf, bg=bg)
                    txt_frame.pack(fill="x", expand=True)
                    line_count = sv.count('\n') + 1
                    h = min(8, max(2, line_count)) if is_multiline else min(6, max(2, len(sv) // 80))
                    t = tk.Text(txt_frame, height=h, wrap="word",
                                font=F["mono"], bg=bg, relief="groove", bd=1,
                                highlightthickness=0)
                    tsb = ttk.Scrollbar(txt_frame, orient="vertical", command=t.yview)
                    t.configure(yscrollcommand=tsb.set)
                    t.insert("1.0", sv)
                    # Read-only but selectable: block keys except Ctrl+C, Ctrl+A, arrows
                    t.bind("<Key>", lambda e: None if (e.state & 4 and e.keysym.lower() in ('c', 'a')) else "break")
                    tsb.pack(side="right", fill="y")
                    t.pack(side="left", fill="both", expand=True)
                    val_widget = t
                else:
                    # Short single-line text: Entry widget (selectable, copyable, read-only)
                    e = tk.Entry(vf, font=F["body"], bg=bg, fg=C["text"],
                                 relief="flat", bd=0, highlightthickness=0,
                                 readonlybackground=bg, width=min(len(sv) + 2, 100))
                    e.insert(0, sv)
                    e.configure(state="readonly")
                    e.pack(side="left")
                    val_widget = e
                # plausible dates of a number, beside the raw value (engine.decode.timestamps):
                # the best reading is shown in full; the rest are demoted to a tooltip so
                # a wrong alternative can never be mistaken for the answer (P0-1).
                _readings = self._date_readings(col, val)
                if _readings:
                    _kind, label, when = _readings[0]
                    text = "%s: %s" % (label, when)
                    re_ = tk.Entry(vf, font=F["tiny"], fg=C["green"], bg=bg,
                                   relief="flat", bd=0, highlightthickness=0,
                                   readonlybackground=bg,
                                   width=len(text) + 1)
                    re_.insert(0, text)
                    re_.configure(state="readonly")
                    re_.pack(side="left", padx=4)
                    ToolTip(re_, "The value as a date: select it and press Ctrl+C, or "
                                 "right-click \u203a Copy as for UTC / Unix / WebKit forms "
                                 "and \u203a Date format to write it another way")
                    re_.bind("<Button-3>", lambda e, c=col, v=val: self._value_menu(e, c, v))
                    if len(_readings) > 1:
                        _alt = "; ".join("%s: %s" % (lb, wh)
                                          for _k, lb, wh in _readings[1:])
                        _alt_lbl = tk.Label(vf, text="%d other possible date%s" % (
                            len(_readings) - 1, "" if len(_readings) == 2 else "s"),
                            font=F["tiny"], fg=C["text2"], bg=bg, cursor="question_arrow")
                        _alt_lbl.pack(side="left", padx=2)
                        ToolTip(_alt_lbl, "Other possible readings (less likely):\n" + _alt)

            # Copy button
            cpb = tk.Button(row_f, text="Copy", font=F["tiny_bold"],
                            fg=C["accent"], bg=C["bg2"], activebackground=C["acl"],
                            relief="flat", bd=0, highlightthickness=0,
                            cursor="hand2", padx=4, pady=1,
                            command=lambda v=val: self._copy_val(v))
            cpb.grid(row=0, column=2, sticky="ne", padx=2, pady=1)
            # Rows of other tables holding this value: a button only when there are some
            # (counted on a worker below, so a large table never holds the window up)
            if rel is not None:
                if supported:
                    key, kval = rel.key_column(self._tbl, col, val)
                    related_asks.append((row_f, key, kval))
                # right-click the name or value: Related rows, Find this value everywhere
                for w in [col_lbl, row_f] + ([val_widget] if val_widget is not None else []):
                    w.bind("<Button-3>", lambda e, c=col, v=val: self._value_menu(e, c, v))

            # Track widget for search highlight
            self._col_widgets[col] = (row_f, col_lbl, val_widget)
            self._keys[row_f] = (col, (col + " " + find_text(val)).lower())

        def finish():
            if related_asks:
                self._count_related(rel, related_asks)

            # Bind mousewheel to ALL widgets in this window for smooth scrolling
            def _bind_scroll_all(w):
                w.bind("<MouseWheel>", self._scroll_fn)
                for child in w.winfo_children():
                    _bind_scroll_all(child)
            _bind_scroll_all(self)

            # Highlight search match if opened from search results
            if self._search_term and self._match_col:
                self.after(150, self._highlight_match)
            self.complete = True

        # the lines are built a batch at a time: a row of hundreds of columns shows at
        # once and fills in without holding the window (the event loop runs between
        # batches; every column is always listed)
        self.complete = False
        todo = list(enumerate(cols))

        def batch(start):
            try:
                if getattr(self, "_closing", False) or not self.winfo_exists():
                    return
            except tk.TclError:
                return
            for i, col in todo[start:start + ROWWIN_BATCH]:
                add(i, col)
            self._place()
            if start + ROWWIN_BATCH < len(todo):
                self.after(1, lambda: batch(start + ROWWIN_BATCH))
            else:
                finish()
        batch(0)

    def _highlight_match(self):
        """Scroll to and highlight the matched column/term from search."""
        col = self._match_col
        term = self._search_term
        if not col or not term or col not in self._col_widgets:
            return
        row_f, col_lbl, val_widget = self._col_widgets[col]

        # Highlight column name label with accent background
        _paint_name(col_lbl, C["hl"], C["hbg"])

        # Scroll canvas so matched column row is visible
        try:
            # the line's place on the canvas (_place)
            ry = next(y for w, _i, y, _h, _p in self._lines if w is row_f)
            canvas_h = self._rw_canvas.winfo_height()
            scroll_h = self._stack_h
            if scroll_h > canvas_h:
                # Scroll so row is near top (with small offset)
                frac = max(0.0, min(1.0, (ry - 30) / scroll_h))
                self._rw_canvas.yview_moveto(frac)
        except Exception:
            pass

        # Highlight search term within the value widget
        if val_widget is None:
            return
        if isinstance(val_widget, tk.Text):
            # Tag-based highlight in Text widget
            val_widget.tag_configure("search_hl", background=K["accent"], foreground=K["white"])
            content = val_widget.get("1.0", "end-1c")
            tl = term.lower()
            cl = content.lower()
            start_idx = 0
            first_pos = None
            while True:
                pos = cl.find(tl, start_idx)
                if pos == -1:
                    break
                # Convert char offset to Text index
                line = content[:pos].count('\n') + 1
                col_off = pos - content[:pos].rfind('\n') - 1
                end_pos = pos + len(term)
                end_line = content[:end_pos].count('\n') + 1
                end_col = end_pos - content[:end_pos].rfind('\n') - 1
                tag_start = f"{line}.{col_off}"
                tag_end = f"{end_line}.{end_col}"
                val_widget.tag_add("search_hl", tag_start, tag_end)
                if first_pos is None:
                    first_pos = tag_start
                start_idx = pos + 1
            # Scroll Text widget to first match
            if first_pos:
                val_widget.see(first_pos)
        elif isinstance(val_widget, tk.Entry):
            # Entry widget: select the matched text
            content = val_widget.get()
            pos = content.lower().find(term.lower())
            if pos >= 0:
                val_widget.configure(state="normal")
                val_widget.selection_range(pos, pos + len(term))
                val_widget.icursor(pos)
                val_widget.xview(pos)
                val_widget.configure(state="readonly")

    def _chosen_kind(self, col):
        """The date kind the user chose for this column in Browse ('Show as date'), or
        None."""
        from engine import timeline as tl
        dates = getattr(self.master, "_browse_dates", None)
        try:
            kind = dates.choice(self._tbl, col) if dates is not None else None
        except Exception:               # noqa: BLE001 - no saved choices
            kind = None
        return kind if kind in tl.KINDS else None

    def _date_readings(self, col, val, guess=None):
        """[(kind, label, when)] for a field: the reading of the date kind chosen for the
        column in Browse first (it is what the grid shows), else the plausible readings,
        but only for a column whose name says date or time (a flags or counter column such
        as Chrome's visits.transition is never offered as a date; guess=True asks anyway);
        0 and negative sentinels give none (they are 'not set', not 1970 or 1601)."""
        from engine import timeline as tl
        if isinstance(val, bool) or not isinstance(val, (int, float, str)):
            return []
        if isinstance(val, (int, float)) and val <= 0:
            return []
        kind = self._chosen_kind(col)
        if kind is not None:
            fmt, custom = self._display_format()
            when = tl.formatter(kind, fmt, custom)(val)
            if when:
                rest = [r for r in timestamps.readings(val) if r[0] != kind]
                return [(kind, "%s (Show as date)" % tl.LABELS[kind], when)] + rest
        if guess is None:
            guess = tl.name_hint(col) in ("strong", "weak")
        if not guess:
            return []
        return timestamps.readings(val) if isinstance(val, (int, float)) else []

    def _display_format(self):
        """(format, custom strftime) chosen for dates (Browse › Date display), else ISO."""
        dates = getattr(self.master, "_browse_dates", None)
        try:
            return dates.display_format() if dates is not None else ("iso", "")
        except Exception:               # noqa: BLE001 - no saved choice
            return "iso", ""

    def _date_format_menu(self, menu, col=None, val=None):
        """'Date format' for the dates of this window (and Browse): each style with this
        value written in it as the example, then 'More styles or your own…' (the chooser with
        ready patterns and a pattern of your own); the row is shown again in the new format."""
        from engine import timeline as tl
        dates = getattr(self.master, "_browse_dates", None)
        if dates is None:
            return
        readings = self._date_readings(col, val) if col is not None else []
        sample = tl.to_utc(val, readings[0][0]) if readings else None
        fmt, _custom = self._display_format()
        sub = tk.Menu(menu, tearoff=0)
        var = tk.StringVar(master=sub, value=fmt)
        sub.var = var

        def pick(key):
            dates.set_display_format(key)
            self._redisplay()
        for key, label, _b, _a in tl.TIME_FORMATS:
            if key == "custom":
                continue
            example = tl.format_dt(sample, key) if sample is not None else ""
            sub.add_radiobutton(label="%s   %s" % (label, example) if example else label,
                                value=key, variable=var, command=lambda k=key: pick(k))
        sub.add_separator()
        sub.add_radiobutton(label="More styles or your own…", value="custom", variable=var,
                            command=lambda: dates._custom_display_format(
                                on_done=self._redisplay, sample=sample))
        menu.add_cascade(label="Date format", menu=sub)

    def _redisplay(self):
        """Show this row again (a new date format): the same row, table and position."""
        try:
            geo = self.geometry()
            parent, db, tbl, rid = self.master, self._db, self._tbl, self._rid
            self.destroy()
            w = RowWin.show(parent, db, tbl, rid)
            if w is not None:
                w.geometry(geo)
        except tk.TclError:
            pass

    def _copy_text(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)

    def _copy_as_menu(self, menu, col, val):
        """'Copy as' for a value read as a date: the UTC text, epoch s / ms / µs and
        WebKit µs (one click, no selecting by hand)."""
        from engine import timeline as tl
        readings = self._date_readings(col, val)
        if not readings:
            return
        kind = readings[0][0]
        dt = tl.to_utc(val, kind)
        if dt is None:
            return
        import calendar
        secs = calendar.timegm(dt.timetuple()) + dt.microsecond / 1e6
        us = int(round(secs * 1e6))
        sub = tk.Menu(menu, tearoff=0)
        utc = readings[0][2]
        for label, text in (("As shown (UTC)  %s" % utc, utc),
                            ("Unix seconds  %d" % (us // 1000000), str(us // 1000000)),
                            ("Unix milliseconds  %d" % (us // 1000), str(us // 1000)),
                            ("Unix microseconds  %d" % us, str(us)),
                            ("WebKit / Chrome µs  %d" % (us + 11644473600 * 1000000),
                             str(us + 11644473600 * 1000000))):
            sub.add_command(label=label, command=lambda t=text: self._copy_text(t))
        menu.add_cascade(label="Copy as (read as %s)" % tl.LABELS[kind], menu=sub)

    def _value_menu(self, event, col, val):
        """Right-click a field: Copy, Copy as (a date), then Related rows / Find this value
        everywhere."""
        rel = getattr(self.master, "relations", None)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Copy value", command=lambda: self._copy_val(val))
        menu.add_command(label="Copy column name", command=lambda: self._copy_text(col))
        self._copy_as_menu(menu, col, val)
        self._guess_menu(menu, col, val)
        self._date_format_menu(menu, col, val)
        if rel is not None:
            table = self._tbl if rel.supported(self._tbl) else None
            rel.value_menu(menu, table, col, val, self._rid)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _blob_context(self, col):
        """Where a BLOB comes from, for the inspector title and its JSON export (in a case,
        with its database)."""
        return "%s.%s row %s" % (self._where, col, self._rid)

    def _guess_menu(self, menu, col, val):
        """'If this is a date' for a number whose column name does not say date: every
        plausible reading, to look at or copy (never shown on the line by itself)."""
        if self._date_readings(col, val) or isinstance(val, bool) or \
                not isinstance(val, (int, float)):
            return
        guesses = self._date_readings(col, val, guess=True)
        if not guesses:
            return
        sub = tk.Menu(menu, tearoff=0)
        for _k, label, when in guesses:
            sub.add_command(label="%s: %s" % (label, when),
                            command=lambda t=when: self._copy_text(t))
        menu.add_cascade(label="If this is a date (copy a reading)", menu=sub)

    def _copy_text(self, text):
        self.clipboard_clear()
        self.clipboard_append(str(text))

    def _copy_val(self, v):
        self.clipboard_clear()
        if isinstance(v, bytes):
            self.clipboard_append(binascii.hexlify(v).decode())
        elif v is None:
            self.clipboard_append("NULL")
        else:
            self.clipboard_append(str(v))

    def _copy_create_sql(self):
        sql = self._db.create_sql(self._tbl)
        if sql:
            self.clipboard_clear()
            self.clipboard_append(sql)
            self._flash("Copied CREATE SQL")
        else:
            messagebox.showinfo("Info", "No CREATE SQL found")

    def _copy_schema_text(self):
        self.clipboard_clear()
        self.clipboard_append(_build_schema_text(self._db, self._tbl))
        self._flash("Copied Schema")

    def _export_single(self, data, col):
        bt = blob_type(data)
        ext = _EXT_MAP.get(bt, ".bin")
        path = filedialog.asksaveasfilename(defaultextension=ext,
                                             initialfile=blob_file_name(self._tbl, self._rid, col, ext))
        if write_allowed(path):
            try:
                with open(path, "wb") as f:
                    f.write(data)
            except Exception as e:
                messagebox.showerror("Error", str(e))

    def _flash(self, msg):
        """Show brief confirmation in title bar, then restore."""
        orig = self.title()
        self.title(msg)
        self.after(1500, lambda: self.title(orig))

    def _copy_json(self):
        """The row as JSON, every value whole (BLOBs as hex, as every export writes them)."""
        from engine.export import json_cell
        self.clipboard_clear()
        d = dict((c, json_cell(self._row_data.get(c))) for c in self._row_cols)
        self.clipboard_append(json.dumps(d, indent=2, ensure_ascii=False, default=str))
        self._flash("Copied JSON")

    def _copy_csv(self):
        """The row as CSV, every value whole (NULL as NULL, BLOBs as x'hex'); text is
        spreadsheet-safe like every CSV of the tool (engine.csvcells)."""
        from engine.csvcells import csv_text, csv_writer
        from engine.export import csv_cell
        self.clipboard_clear()
        out = io.StringIO()
        w = csv_writer(out, formulas=False)   # cells made by csv_cell
        w.writerow([csv_text(c) for c in self._row_cols])
        w.writerow([csv_cell(self._row_data.get(c)) for c in self._row_cols])
        self.clipboard_append(out.getvalue())
        self._flash("Copied CSV")

    def _copy_text(self):
        from engine.export import csv_cell
        self.clipboard_clear()
        self.clipboard_append("\n".join(
            "%s: %s" % (c, csv_cell(self._row_data.get(c), formulas=False))
            for c in self._row_cols))
        self._flash("Copied Text")

    def _export_blobs(self):
        folder = filedialog.askdirectory(title="Select folder for BLOBs")
        if not write_allowed(folder):
            return
        count, failed = 0, []
        for c in self._row_cols:
            v = self._row_data.get(c)
            if isinstance(v, bytes) and len(v) > 0:
                bt = blob_type(v)
                ext = _EXT_MAP.get(bt, ".bin")
                try:
                    # unique per row and never replaces an existing file
                    f, _path = create_new_file(folder, blob_file_name(self._tbl, self._rid, c, ext))
                    with f:
                        f.write(v)
                    count += 1
                except Exception as e:
                    failed.append("%s: %s" % (c, e))
        if failed:
            messagebox.showwarning("Export", "Exported %d BLOB(s); %d could not be written:\n%s"
                                   % (count, len(failed), "\n".join(failed[:5])), parent=self)
        elif count > 0:
            self._flash(f"Exported {count} BLOB(s)")
        else:
            messagebox.showinfo("Export", "No BLOBs found in this row")

    def _on_close(self):
        key = RowWin.pool_key(self._tbl, self._rid, self._db)
        if key in RowWin._pool:
            del RowWin._pool[key]
        # A row of 250 columns has over a thousand widgets, most never shown (the canvas maps
        # only the lines in view), and Tk takes about a millisecond for each of those: the
        # window goes away at once and its lines are destroyed a few at a time, so the other
        # windows never freeze.
        try:
            self.withdraw()
        except tk.TclError:
            pass
        cv = getattr(self, "_rw_canvas", None)
        lines = [line[0] for line in getattr(self, "_lines", ())] +             [line[0] for line in getattr(self, "_pending", ())]
        self._lines, self._pending = [], []
        self.complete = True            # nothing more is built into a closing window
        self._closing = True
        try:
            if cv is not None:
                cv.delete("all")
        except tk.TclError:
            pass

        def chunk(start):
            for w in lines[start:start + ROWWIN_CLOSE_BATCH]:
                try:
                    self.tk.call("destroy", w._w)
                except tk.TclError:
                    pass
            if start + ROWWIN_CLOSE_BATCH < len(lines):
                try:
                    self.after(1, lambda: chunk(start + ROWWIN_CLOSE_BATCH))
                    return
                except (tk.TclError, RuntimeError):
                    pass
            try:
                self.tk.call("destroy", self._w)
            except tk.TclError:
                pass
            try:
                self.destroy()
            except tk.TclError:
                pass
        chunk(0)
