"""The illustrated guide: one HTML page that says how the tool works - what it reads, where a
SQLite database keeps traces of what it no longer shows, what can be recovered and when it
cannot, how to read the results, saving and reports, the limits of all of it, the tabs and
the shortcuts. Written to the tool's own data folder (never near the evidence) and opened in
the browser: Help > Open the illustrated guide, or the command palette.

The page is self-contained (its own stylesheet, its diagrams drawn as inline SVG, nothing
loaded from anywhere; a Content-Security-Policy forbids loading anything), follows the
reports' light and dark palettes, and prints.
"""

import html
import os

from constants import VERSION
from engine.html_report_assets import DARK_VARS, FONT_VARS, LIGHT_VARS

FILE_NAME = "guide-%s.html" % VERSION


def esc(s):
    return html.escape(str(s), quote=True)


# -- content -----------------------------------------------------------------------------------
SECTIONS = (
    ("how", "How it works"),
    ("traces", "Where databases keep traces"),
    ("recovered", "What is recovered"),
    ("reading", "Reading the results"),
    ("work", "Working with the data"),
    ("tabs", "The tabs"),
    ("saving", "Saving and reports"),
    ("limits", "Limitations"),
    ("shortcuts", "Shortcuts and about"),
)

STEPS = (
    ("1", "Evidence",
     "A database file, with its -wal and -journal files when they exist, or a whole folder of "
     "an app's databases as one case. Every file is opened read-only through an immutable "
     "connection and hashed (SHA-256); nothing is ever written to it or next to it."),
    ("2", "Reading",
     "The engine reads the schema and the rows SQL shows, and also the bytes SQL never shows: "
     "every page of the file, the frames of the write-ahead log, the pages the rollback "
     "journal saved, free space inside pages and freed pages."),
    ("3", "Analysis",
     "Search every table at once, browse and filter, join tables without SQL, read fields out "
     "of JSON and plist values, put every date on one timeline, map the links between tables "
     "and databases, and recover the rows that were deleted or replaced."),
    ("4", "Results",
     "Row detail with the byte offset of every recovered record, tags, exports as CSV, JSON "
     "and HTML with a manifest, and reports that name the evidence files with their hashes "
     "and the limits that applied."),
)

TRACES = (
    ("Main file pages", "pages",
     "Live rows in the B-tree pages of each table and index.",
     "The rows SQL shows, as stored, with the page and cell each came from.",
     "A row updated in place leaves no trace of its old values in the main file; look in "
     "the WAL and the journal for those."),
    ("Freeblocks and unallocated space", "free",
     "Inside a page: the bytes of deleted cells (freeblocks) and the gap between the cell "
     "pointers and the cells.",
     "Deleted rows whose bytes were not reused yet, often whole; the first bytes of a "
     "freeblock are overwritten by its header, so the rowid can be lost and is then "
     "inferred when the page leaves one possibility.",
     "A row deleted and replaced by a longer one is gone; a shorter one leaves the tail of "
     "the old row (a 'partial' record)."),
    ("Freed pages", "fp",
     "Pages the database put on its free list after a whole table, index or subtree was "
     "emptied or dropped.",
     "Entire pages of old rows, with the table they belonged to worked out from the bytes "
     "(the page header no longer says).",
     "VACUUM removes them; secure_delete overwrites them with zeros."),
    ("Unreferenced pages", "orphan",
     "Pages no B-tree and no free list points to any more (an earlier version of the file, "
     "a dropped table's pages before they were reused).",
     "Rows of tables that may no longer exist; records rebuilt from damaged bytes there "
     "read as deleted.",
     "Pages reused by a new table hold a mix of old and new bytes."),
    ("Write-ahead log", "wal",
     "The -wal file: every page written since the last checkpoint, frame by frame, in order.",
     "Older versions of rows (an update writes the whole page again), rows deleted before the "
     "checkpoint, and the order in which pages changed; 'WAL merged' means the frames were "
     "read and overlaid in memory, never written back.",
     "A checkpoint folds the frames into the main file and the WAL is reused from the start; "
     "what a frame held before that is gone."),
    ("Rollback journal", "journal",
     "The -journal file: the pages as they were before the last transaction, for rolling "
     "it back.",
     "A table as it was before that transaction, row by row; a hot journal means the "
     "transaction never finished.",
     "Journals are deleted or truncated when a transaction commits; only the last "
     "transaction's pages survive."),
    ("Indexes", "index",
     "The B-tree of each index: a copy of the indexed columns of every row, with its rowid.",
     "A deleted index entry proves a row with those values existed under that rowid, even "
     "when the row's own bytes are gone.",
     "An index holds only its columns; the rest of the row is not in it."),
)

STATES = (
    ("deleted", "b-bad", "Not live any more: no row of the table holds these values."),
    ("older version", "b-warn", "The row is live with other values (same rowid, or its UNIQUE "
     "key is live under another rowid): this is what it looked like before."),
    ("inserted again", "b-info", "Deleted, and the same values are live under another rowid "
     "(an app that deletes and re-inserts rows, or a duplicate row); the record names it."),
    ("index entry", "b-mut", "A deleted entry of an index: the indexed columns and the rowid "
     "they belonged to."),
    ("unreferenced page", "b-mut", "On a page nothing references; rebuilt from damaged bytes."),
)

CONFIDENCE = (
    ("high", "b-ok", "Every value fits the table's column types and the record was found "
     "intact, in a place that belongs to that table."),
    ("medium", "b-warn", "The values fit, but something was inferred (the rowid, the table "
     "from several that fit) or the record is an older version."),
    ("low", "b-bad", "A partial record, or values that fit the table only loosely: read it "
     "with the bytes beside it."),
)

TABS = (
    ("Overview", "Every database of the case on one list: path, size, tables, rows, WAL and "
     "journal state, SHA-256 (whole, copyable), the biggest tables and the links between "
     "databases."),
    ("Search", "One word, number or name in every table of every database, with the recovered "
     "records too; each hit says whether it is a live row, a WAL version or a recovered "
     "record, and where."),
    ("Browse", "Any table, however large, read as you scroll; Excel-like filters per column, "
     "Show as date / Show as number, fields inside JSON and plist values as ƒ columns, "
     "values from linked tables beside the keys."),
    ("SQL", "Your own SQL on the read-only database, with history, dates shown as dates and "
     "the same grid tools."),
    ("Query", "Tables joined on their links without writing SQL: pick columns, filter them, "
     "calculate, group; see the SQL it runs."),
    ("Forensics", "Recovered records with their state, confidence and byte offset, freed pages, "
     "row history across the main file, WAL and journal, dropped tables, the journal, an "
     "audit."),
    ("Timeline", "Every date column of every table on one list and chart, in any time zone, "
     "with the rows behind each bar."),
    ("Tagged", "The rows you marked, from every tab, exported with their provenance."),
    ("Relationships", "Declared foreign keys, links verified by values, links between "
     "databases, as a list and as a diagram."),
    ("WAL", "The frames of the write-ahead log as pages and as rows: what each frame changed "
     "and which version is current."),
)

WORK = (
    ("Open the databases you need", (
        ("1 Folder", "Open ▾ › Open folder…, choose the folder (an extraction, a users "
         "folder); Include subfolders. Nothing is read yet."),
        ("2 Which files", "Optional conditions of your own on the file name, the path in the "
         "folder, the folder name, the extension or the size - is, is not, contains, starts "
         "or ends with, matches * and ?, at least, at most; all or any of them. Example: "
         "File name is History, or File name is places.sqlite."),
        ("3 Scan", "Scan for databases. The line beside it says first what will be opened; "
         "files that do not match are never opened. The list fills as databases are found, "
         "with Stop and Scan again."),
        ("Then", "Filter the list by the same conditions or a word, Tick shown only, sort by "
         "any column, Open: the databases form one case - one search, one timeline, every "
         "result naming its database."))),
    ("Fields inside values, as columns", (
        ("Where", "Right-click the header of a BLOB or JSON column › Fields inside… (Browse), "
         "or the column's ▸ in the Query tab."),
        ("What", "The fields its values hold - JSON, plist, NSKeyedArchiver, protobuf and the "
         "other decoded forms - listed with how many rows have them and an example."),
        ("Result", "Each field ticked becomes a column of its own: ƒ sender.name, "
         "ƒ amount (the full path, so the same key under two parents gives two "
         "columns), in its own colour; sort, filter, copy, export it, or join another table "
         "on it in Query."),
        ("Stored", "Nothing: the stored value stays as it is, the field is read from it as "
         "you scroll."))),
    ("Formulas of your own", (
        ("Where", "Right-click a column header › Calculate from…; x is the value of that "
         "column, any other column of the row is used by its name."),
        ("Checked", "As you type, against real rows of the table, with the result beside "
         "each; an error says where and why, nothing runs that is not a formula."),
        ("Result", "A ƒ= column, sortable, filterable, exported as its own column and "
         "remembered for tables of the same name; or steps (divide, round, a date kind, a "
         "field) instead of a formula."))),
    ("Query without SQL", (
        ("Tables", "Add tables: they join on their links (declared keys and links verified by "
         "values), the strongest per table, the others a right-click away."),
        ("Columns", "Tick the columns to show; calculated columns and fields inside values "
         "join them."),
        ("Conditions", "Month bars and calendars for dates, value lists with counts, number "
         "ranges, any SQLite comparison; summaries per column (count, distinct, min, max, "
         "sum, average); group by any column."),
        ("Result", "What you will get, said in words; the SQL it runs, in the SQL tab; across "
         "the databases of a case and over recovered rows."))),
    ("Millions of rows", (
        ("Scroll", "Drag the scrollbar anywhere: the rows follow it; the wheel, Page Up and "
         "Down, Home and End the same."),
        ("Filter", "Every column header has a filter like a spreadsheet: values with counts, "
         "dates by month, ranges, conditions; a box filters all columns."),
        ("Status", "Which rows are shown of how many, which filters apply, and when a read "
         "is waited for."))),
)

FORMULAS = (
    ("round(x / 100, 2)", "an amount kept in the smallest unit, in units"),
    ("x * qty + fee", "a total from three columns of the row"),
    ("date(x, \"ms\")", "a Unix millisecond number as a date"),
    ("json(x, \"to.name\")", "a field of a JSON or plist value"),
    ("size(x)", "a byte count (or a BLOB's length) as '1.5 MB'"),
    ("upper(left(name, 3))", "the first three letters of another column, in capitals"),
    ("x if x > 0 else 0", "a value, or 0 for a negative one"),
)

SHORTCUTS = (
    ("Ctrl+O", "open a database"), ("Ctrl+K", "the command palette (any table, column or action)"),
    ("Ctrl+F", "the search field of the tab or window shown"),
    ("Ctrl+B", "show or hide the databases panel"),
    ("Enter", "open the row detail of the current row"),
    ("Shift+Enter", "view the whole value of the current cell"),
    ("Ctrl+C / Ctrl+Shift+C", "copy the cell / the selected rows"),
    ("Ctrl+T, Ctrl+1…9", "tag the selected rows"),
    ("Alt+Down", "open a dropdown, a column filter or a calendar"),
    ("Ctrl+E / Ctrl+Enter", "go to the SQL editor / run the query"),
    ("F3 / Shift+F3", "next / previous match in a search field"),
    ("Escape", "clear a filter, close a list, stop a search"),
)

LIMITS = (
    ("What was overwritten is gone",
     "A page written again, a freeblock reused, a checkpointed WAL, a journal of a committed "
     "transaction: the bytes are replaced and no tool reads them back. The report says which "
     "places held nothing."),
    ("secure_delete and VACUUM",
     "A database with secure_delete on overwrites deleted bytes with zeros as it deletes; "
     "VACUUM rewrites the file without its free space. Little or nothing is recoverable then, "
     "and the tool says so rather than guessing."),
    ("Encrypted files",
     "A file that is not a plain SQLite database (an encrypted one, or another format with "
     "the same extension) is named as such on the Overview, with what its header reads; it "
     "cannot be read without the key."),
    ("Matching is by shape",
     "A recovered record is matched to a table by the number and types of its values. Two "
     "tables of the same shape can both fit; the confidence and the reasons say when the "
     "match was inferred, and a record that is a copy of a live row is left out."),
    ("Every cap is a named limit",
     "Rows kept in memory, samples read, time spent: each is a limit with a name, listed "
     "under Database › Limits… and said wherever it cut something off. Nothing is "
     "trimmed silently."),
    ("Row counts are estimates until counted",
     "A very large table shows '~' and 'estimate' until its rows are counted in the "
     "background; what you see is marked as such."),
)


# -- the diagram: how a deleted row comes back ----------------------------------------------
def _flow_svg():
    """Sources on the left, the match in the middle, the record and its states on the right:
    inline SVG, the colours from the page's variables."""
    w, h = 1120, 330
    srcs = [("Freeblock", "bytes of a deleted cell inside a page"),
            ("Freed page", "a whole page on the free list"),
            ("WAL frame", "a page as it was before an update"),
            ("Journal page", "a page before the last transaction")]
    out = ['<svg viewBox="0 0 %d %d" role="img" aria-labelledby="flowt" '
           'xmlns="http://www.w3.org/2000/svg">' % (w, h),
           '<title id="flowt">How a deleted row comes back: the bytes of a freeblock, a '
           'freed page, a WAL frame or a journal page are matched to a table by shape, '
           'checked against the live rows, and listed with a state, a confidence and '
           'where they were found.</title>',
           '<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
           'markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" '
           'fill="var(--muted)"/></marker></defs>']
    y = 22
    for title, sub in srcs:
        out.append('<g class="node src"><rect x="20" y="%d" width="250" height="56" rx="10"/>'
                   '<text x="36" y="%d" class="t">%s</text><text x="36" y="%d" class="s">%s'
                   '</text></g>' % (y, y + 24, esc(title), y + 44, esc(sub)))
        out.append('<path d="M270 %d C 330 %d, 340 165, 400 165" class="edge"/>'
                   % (y + 28, y + 28))
        y += 72
    out.append('<g class="node mid"><rect x="400" y="100" width="300" height="130" rx="12"/>'
               '<text x="418" y="128" class="t">Matched to a table by shape</text>'
               '<text x="418" y="150" class="s">the number and types of the values fit one</text>'
               '<text x="418" y="166" class="s">table (or several: then the confidence says)</text>'
               '<text x="418" y="196" class="t2">Checked against the live rows</text>'
               '<text x="418" y="216" class="s">a copy of a live row is left out</text></g>')
    out.append('<path d="M700 165 L 760 165" class="edge arrow"/>')
    out.append('<g class="node res"><rect x="760" y="60" width="340" height="210" rx="12"/>'
               '<text x="778" y="88" class="t">One recovered record</text>'
               '<text x="778" y="110" class="s">state, confidence and the reasons;</text>'
               '<text x="778" y="126" class="s">file, page, cell offset and byte offset;</text>'
               '<text x="778" y="142" class="s">the bytes themselves, highlighted</text>')
    y = 166
    for state, tone, _why in STATES[:4]:
        out.append('<rect x="778" y="%d" width="150" height="22" rx="11" class="pill %s"/>'
                   '<text x="853" y="%d" class="p" text-anchor="middle">%s</text>'
                   % (y, tone, y + 15, esc(state)))
        y += 26
    out.append('<text x="950" y="182" class="s">high</text><text x="950" y="208" class="s">'
               'medium</text><text x="950" y="234" class="s">low</text>'
               '<text x="1000" y="182" class="s">intact, fits, own place</text>'
               '<text x="1000" y="208" class="s">something inferred</text>'
               '<text x="1000" y="234" class="s">partial or loose</text></g>')
    out.append("</svg>")
    return "".join(out)


def _reading_svg():
    """A row as the grid shows it: the stored value, a derived ƒ column, a column shown
    as a date and one as a number, the badges on their headers."""
    w, h = 1120, 150
    cols = [("amount · ÷100", "340.05", "num"),
            ("ts · UTC ms", "2025-02-28 14:41:45.766", "date"),
            ("tstore_data", '{"amount":34005,"to":{"name":…', "raw"),
            ("ƒ to.name", "Grocer", "der")]
    out = ['<svg viewBox="0 0 %d %d" role="img" aria-labelledby="rdt" '
           'xmlns="http://www.w3.org/2000/svg">' % (w, h),
           '<title id="rdt">A row in the grid: a column shown as a number carries a ÷100 '
           'badge, a date column a UTC badge, a derived column is named ƒ and drawn in '
           'its own colour; the stored values stay underneath.</title>']
    x = 20
    widths = [200, 280, 360, 220]
    for (head, val, kind), cw in zip(cols, widths):
        out.append('<rect x="%d" y="20" width="%d" height="34" class="hd"/>' % (x, cw))
        out.append('<text x="%d" y="42" class="h">%s</text>' % (x + 12, esc(head)))
        out.append('<rect x="%d" y="54" width="%d" height="40" class="cell"/>' % (x, cw))
        out.append('<text x="%d" y="79" class="v %s">%s</text>' % (x + 12, kind, esc(val)))
        x += cw
    out.append('<text x="20" y="120" class="s">stored: 34005</text>'
               '<text x="220" y="120" class="s">stored: 1740753705766</text>'
               '<text x="500" y="120" class="s">the value itself, whole in the row detail</text>'
               '<text x="860" y="120" class="s">read out of tstore_data</text>'
               '<text x="20" y="142" class="s">Copies keep the stored values; exports keep '
               'them and add the shown text as its own column.</text></svg>')
    return "".join(out)


# -- the page ------------------------------------------------------------------------------
CSS = (":root{" + LIGHT_VARS + FONT_VARS +
       "--shadow:0 1px 2px rgba(15,23,42,.06),0 4px 14px "
       "rgba(15,23,42,.05);--radius:14px;color-scheme:light}\n"
       "@media (prefers-color-scheme:dark){:root:not([data-theme=light]){" + DARK_VARS +
       "--shadow:0 1px 2px rgba(0,0,0,.4);color-scheme:dark}}\n"
       ":root[data-theme=dark]{" + DARK_VARS + "--shadow:0 1px 2px rgba(0,0,0,.4);"
       "color-scheme:dark}\n" + r"""
*{box-sizing:border-box}
html{scroll-padding-top:16px;-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 var(--font)}
a{color:var(--link);text-underline-offset:2px}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 80px}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);padding:22px 26px;margin-bottom:22px}
.hero{position:relative;overflow:hidden;padding:30px 32px 26px}
.hero:before{content:"";position:absolute;inset:0;background-image:radial-gradient(var(--line) 1.2px,transparent 1.2px);background-size:18px 18px;opacity:.9;mask-image:linear-gradient(90deg,transparent 40%,#000);-webkit-mask-image:linear-gradient(90deg,transparent 40%,#000)}
.hero>*{position:relative}
.kicker{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);font-weight:600;margin:0 0 6px}
h1{font-size:30px;line-height:1.2;margin:0 0 8px;letter-spacing:-.01em}
.lead{margin:0 0 16px;color:var(--muted);font-size:15.5px;max-width:860px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:0;padding:0;list-style:none}
.chips a{display:inline-block;padding:6px 14px;border:1px solid var(--border);border-radius:999px;background:var(--card);color:var(--text);text-decoration:none;font-size:13.5px}
.chips a:hover{border-color:var(--secondary);background:var(--hover)}
h2{font-size:19px;margin:0 0 2px}
.sub{color:var(--muted);margin:0 0 16px;font-size:14px}
h3{font-size:15px;margin:0 0 6px}
.steps{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;align-items:stretch}
.step{border:1px solid var(--border);border-radius:12px;padding:16px 16px 14px;background:var(--card);position:relative}
.step .n{display:inline-flex;align-items:center;justify-content:center;width:34px;height:34px;border-radius:9px;background:var(--b-info-bg);color:var(--b-info-fg);font-weight:700;margin-bottom:10px}
.step p{margin:0;color:var(--muted);font-size:13.5px;line-height:1.5}
.minor{font-size:11.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:600;margin:22px 0 10px}
figure{margin:0}
figure svg{width:100%;height:auto;display:block}
figcaption{color:var(--muted);font-size:13px;margin-top:8px}
svg text{font-family:var(--font);fill:var(--text)}
svg .t{font-size:14px;font-weight:650}svg .t2{font-size:13.5px;font-weight:650}
svg .s{font-size:12px;fill:var(--muted)}svg .h{font-size:12.5px;font-weight:650}
svg .node rect{fill:var(--card);stroke:var(--border);stroke-width:1.2}
svg .node.mid rect{fill:var(--hover);stroke:var(--secondary)}
svg .node.res rect{fill:var(--b-ok-bg);stroke:var(--success)}
svg .edge{fill:none;stroke:var(--secondary);stroke-width:1.6}
svg .edge.arrow{marker-end:url(#arr);stroke:var(--muted)}
svg .pill{stroke:none}svg .p{font-size:12px;font-weight:600}
svg .pill.b-bad{fill:var(--b-bad-bg)}svg .pill.b-bad+text{fill:var(--b-bad-fg)}
svg .pill.b-warn{fill:var(--b-warn-bg)}svg .pill.b-warn+text{fill:var(--b-warn-fg)}
svg .pill.b-info{fill:var(--b-info-bg)}svg .pill.b-info+text{fill:var(--b-info-fg)}
svg .pill.b-mut{fill:var(--b-mut-bg)}svg .pill.b-mut+text{fill:var(--b-mut-fg)}
svg .hd{fill:var(--head);stroke:var(--border)}svg .cell{fill:var(--card);stroke:var(--border)}
svg .v{font-size:13.5px}svg .v.num,svg .v.date{fill:var(--success);font-variant-numeric:tabular-nums}
svg .v.der{fill:#7c3aed}svg .v.raw{font-family:var(--mono);font-size:12.5px}
.grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
.trace{border:1px solid var(--border);border-radius:12px;padding:16px 18px;background:var(--card)}
.trace h3{display:flex;align-items:center;gap:10px}
.trace .ic{width:30px;height:30px;border-radius:8px;background:var(--b-info-bg);color:var(--b-info-fg);display:inline-flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex:none}
.trace dl{margin:10px 0 0;display:grid;grid-template-columns:auto 1fr;gap:6px 12px;font-size:13.5px}
.trace dt{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:600;padding-top:3px}
.trace dd{margin:0;color:var(--text)}
.pills{display:grid;grid-template-columns:auto 1fr;gap:10px 14px;align-items:start;margin:8px 0 0}
.pill{display:inline-block;padding:3px 12px;border-radius:999px;font-size:12.5px;font-weight:600;white-space:nowrap}
.b-ok{background:var(--b-ok-bg);color:var(--b-ok-fg)}.b-warn{background:var(--b-warn-bg);color:var(--b-warn-fg)}
.b-bad{background:var(--b-bad-bg);color:var(--b-bad-fg)}.b-info{background:var(--b-info-bg);color:var(--b-info-fg)}
.b-mut{background:var(--b-mut-bg);color:var(--b-mut-fg)}
.pills p{margin:0;font-size:13.5px;color:var(--text)}
.tab{border:1px solid var(--border);border-radius:12px;padding:12px 14px;background:var(--card)}
.tab h3{margin-bottom:4px}.tab p{margin:0;font-size:13.5px;color:var(--muted)}
.lim{border-left:3px solid var(--amber);padding:2px 0 2px 14px;margin:0 0 12px}
.lim h3{margin:0 0 2px}.lim p{margin:0;color:var(--muted);font-size:13.5px}
table.keys{border-collapse:collapse;width:100%;font-size:13.5px}
table.keys td{padding:6px 10px;border-top:1px solid var(--line);vertical-align:top}
table.keys td:first-child{font-family:var(--mono);white-space:nowrap;width:220px;color:var(--text)}
.about{color:var(--muted);font-size:13px;margin-top:14px}
kbd{font-family:var(--mono);font-size:12.5px;background:var(--head);border:1px solid var(--border);border-radius:5px;padding:1px 6px}
@media (max-width:900px){.steps,.grid3{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:620px){.steps,.grid2,.grid3{grid-template-columns:1fr}.wrap{padding:14px 12px 60px}.card{padding:16px}h1{font-size:24px}}
@media print{body{background:#fff}.card{box-shadow:none;break-inside:avoid}.chips{display:none}}
""")


def guide_html(version=VERSION):
    """The guide as one HTML document."""
    out = ['<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">',
           '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
           'style-src \'unsafe-inline\'; img-src data:">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           '<title>How SQLite GUI Analyzer works</title><style>', CSS, '</style></head><body>',
           '<div class="wrap">']
    # hero
    out.append('<header class="card hero"><p class="kicker">Guide</p>'
               '<h1>How SQLite GUI Analyzer works</h1>'
               '<p class="lead">What it reads, where a database keeps traces of what it no '
               'longer shows, what can be recovered and when it cannot, how to read the '
               'results, and the limits of all of it.</p><ul class="chips">')
    for sid, title in SECTIONS:
        out.append('<li><a href="#%s">%s</a></li>' % (sid, esc(title)))
    out.append('</ul></header>')
    # how it works
    out.append('<section class="card" id="how"><h2>How it works</h2>'
               '<p class="sub">From evidence to a report, in four steps.</p><div class="steps">')
    for n, title, text in STEPS:
        out.append('<div class="step"><span class="n">%s</span><h3>%s</h3><p>%s</p></div>'
                   % (n, esc(title), esc(text)))
    out.append('</div><p class="minor">How a deleted row comes back</p><figure>%s'
               '<figcaption>The bytes of a freeblock, a freed page, a WAL frame or a journal '
               'page are matched to a table by their shape, checked against the live rows, '
               'and listed as one record with its state, its confidence and where it was '
               'found, down to the byte.</figcaption></figure></section>' % _flow_svg())
    # traces
    out.append('<section class="card" id="traces"><h2>Where databases keep traces</h2>'
               '<p class="sub">Each place in a SQLite file, what it keeps, what it tells you, '
               'and what to watch out for.</p><div class="grid2">')
    for title, ic, keeps, tells, watch in TRACES:
        out.append('<div class="trace"><h3><span class="ic">%s</span>%s</h3><dl>'
                   '<dt>Keeps</dt><dd>%s</dd><dt>Tells you</dt><dd>%s</dd>'
                   '<dt>Watch out</dt><dd>%s</dd></dl></div>'
                   % (esc(ic[:2].upper()), esc(title), esc(keeps), esc(tells), esc(watch)))
    out.append('</div></section>')
    # recovered
    out.append('<section class="card" id="recovered"><h2>What is recovered</h2>'
               '<p class="sub">Every record says what it is and how sure the match is.</p>'
               '<div class="grid2"><div><h3>State</h3><div class="pills">')
    for state, tone, why in STATES:
        out.append('<span class="pill %s">%s</span><p>%s</p>' % (tone, esc(state), esc(why)))
    out.append('</div></div><div><h3>Confidence</h3><div class="pills">')
    for level, tone, why in CONFIDENCE:
        out.append('<span class="pill %s">%s</span><p>%s</p>' % (tone, esc(level), esc(why)))
    out.append('</div><p class="about">A record identical to a live row is left out, also '
               'when its bytes fit several tables and the row is live in one of them. The '
               'reasons of every record are listed with it and in the reports.</p></div></div>'
               '</section>')
    # reading
    out.append('<section class="card" id="reading"><h2>Reading the results</h2>'
               '<p class="sub">Derived values are new columns, marked; stored values are never '
               'replaced.</p><figure>%s<figcaption>Right-click a column header for Show as '
               'date, Show as number (÷100 for amounts kept in the smallest unit, '
               '÷1,000, ÷1,000,000, thousands separators) and Fields inside… '
               '(fields of JSON, plist and other decoded values as ƒ columns), and Calculate '
               'from… for a column of your own: a formula in Excel-like terms over any '
               'column of the row (round(x / 100, 2), x * qty + fee, upper(left(name, 3)), '
               'x if x &gt; 0 else 0, date(x, "ms"), json(x, "to.name")) or steps, checked '
               'as you type with a live preview, added as a ƒ= column. The header '
               'carries the badge, the tooltip shows the stored value, copies keep it, and an '
               'export adds the shown text as a column of its own.</figcaption></figure>'
               '<p class="minor">Provenance</p><p>Every recovered record opens in a row '
               'detail with three views: the values, where it was found (database, file, page, '
               'cell offset and the byte offset in the file, the WAL frame and its state when '
               'it came from the log) and the bytes themselves with the record highlighted. '
               'All of it is copyable and goes into the exports and the reports; names say '
               'which database a row is from when a case holds several.</p></section>'
               % _reading_svg())
    # working with the data
    out.append('<section class="card" id="work"><h2>Working with the data</h2>'
               '<p class="sub">Picking the databases, turning values into columns, '
               'formulas, queries without SQL, and moving through millions of rows.</p>'
               '<div class="grid2">')
    for title, rows in WORK:
        out.append('<div class="trace"><h3>%s</h3><dl>' % esc(title))
        for term, text in rows:
            out.append('<dt>%s</dt><dd>%s</dd>' % (esc(term), esc(text)))
        out.append('</dl></div>')
    out.append('</div><p class="minor">Formulas, for example</p><table class="keys">')
    for formula, what in FORMULAS:
        out.append('<tr><td>%s</td><td>%s</td></tr>' % (esc(formula), esc(what)))
    out.append('</table><p class="about">The formula editor lists every function with an example: text, numbers, '
               'dates and time zones, durations, sizes, hashes, base64 / hex, regular '
               'expressions, and fields of JSON, plist, NSKeyedArchiver and protobuf '
               'values.</p></section>')
    # tabs
    out.append('<section class="card" id="tabs"><h2>The tabs</h2><p class="sub">Where each '
               'kind of question is answered.</p><div class="grid3">')
    for title, text in TABS:
        out.append('<div class="tab"><h3>%s</h3><p>%s</p></div>' % (esc(title), esc(text)))
    out.append('</div></section>')
    # saving
    out.append('<section class="card" id="saving"><h2>Saving and reports</h2>'
               '<p class="sub">Everything you keep names the evidence it came from.</p>'
               '<div class="grid2"><div class="trace"><h3>Exports</h3><dl>'
               '<dt>Rows</dt><dd>CSV or JSON of the rows a filter keeps or the rows selected, '
               'from any grid; a column shown as a date or a number keeps the stored value '
               'and adds the shown text beside it.</dd>'
               '<dt>BLOBs</dt><dd>Each BLOB as a file, with a manifest.</dd>'
               '<dt>Manifest</dt><dd>Every export writes one: the evidence files with size '
               'and SHA-256, the tool and its version, the time (UTC), the scope and the '
               'filters, and the limits that applied.</dd></dl></div>'
               '<div class="trace"><h3>Reports</h3><dl>'
               '<dt>Forensic report</dt><dd>Everything found: recovered records with state, '
               'confidence, reasons and offsets; row history; freed pages; the audit. HTML '
               'that works offline, searchable, sortable, printable.</dd>'
               '<dt>Tagged rows</dt><dd>The rows you marked, from every tab, with their '
               'provenance.</dd>'
               '<dt>Schema report, Database Map</dt><dd>The tables, columns, indexes and '
               'triggers; the links between tables and databases.</dd></dl></div></div>'
               '<p class="about">Nothing is written near the evidence: exports refuse a path '
               'in an evidence folder. The tool’s own files (tags, settings, this guide) '
               'live in its data folder.</p></section>')
    # limits
    out.append('<section class="card" id="limits"><h2>Limitations</h2><p class="sub">What '
               'no reading of the bytes can give, said where it applies.</p>')
    for title, text in LIMITS:
        out.append('<div class="lim"><h3>%s</h3><p>%s</p></div>' % (esc(title), esc(text)))
    out.append('</section>')
    # shortcuts and about
    out.append('<section class="card" id="shortcuts"><h2>Shortcuts and about</h2>'
               '<p class="sub">The keys that reach everything; the rest is in Help.</p>'
               '<table class="keys">')
    for keys, what in SHORTCUTS:
        out.append('<tr><td>%s</td><td>%s</td></tr>' % (esc(keys), esc(what)))
    out.append('</table><p class="about">SQLite GUI Analyzer %s. Read-only by design: every '
               'database is opened immutable, write-ahead logs are overlaid in memory, and '
               'every export names the evidence with its SHA-256. Standard library only: '
               'runs from source on Python 3.8 to 3.14.</p></section>' % esc(version))
    out.append('</div></body></html>')
    return "".join(out)


def guide_path(directory=None):
    """Where the guide is written: the tool's data folder (never near the evidence)."""
    from engine.tags import data_dir
    return os.path.join(directory or data_dir(), FILE_NAME)


def write_guide(directory=None):
    """Write the guide (again when its content changed) and return its path."""
    path = guide_path(directory)
    text = guide_html()
    try:
        with open(path, "r", encoding="utf-8") as f:
            if f.read() == text:
                return path
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def open_guide(parent=None):
    """Write the guide and open it in the browser; the path is returned (and shown in a
    message when no browser could be started)."""
    import webbrowser
    path = write_guide()
    try:
        ok = webbrowser.open("file:///" + path.replace("\\", "/"))
    except Exception:               # noqa: BLE001 - no browser: the path is still useful
        ok = False
    if not ok and parent is not None:
        from tkinter import messagebox
        messagebox.showinfo("Guide", "The guide is written at:\n%s" % path, parent=parent)
    return path
