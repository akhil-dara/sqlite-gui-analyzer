"""Named limits: every cap, sample size and parallelism of the tool in one place.

DEFAULTS holds each limit's default, RANGES what a setting may be and DESCRIPTIONS what it
limits. The user can override any of them in the app-data settings.json ({"limits": {"name":
value}}); load() validates what it reads: a value of the wrong type or out of range keeps the
default and is reported (problems, shown as Issues by the UI), never a crash. Code reads a
limit with get(name), or all of them with current(); checked(overrides) gives a full, checked
set for one job (a window's own choice of 'rows per link', say).

Whenever a limit cuts something off, the part of the UI or the output that used it says so,
with the limit's name.
"""

import threading

DEFAULTS = {
    # a case of several databases
    "case_max_databases": 64,           # databases open together at most
    "case_search_parallel": 4,          # databases searched side by side
    "case_timeline_parallel": 4,        # databases detected / read side by side (timeline)
    "folder_scan_files": 50000,         # files looked at by one Open Folder scan
    "hash_parallel": 2,                 # databases whose evidence is hashed at the same time
    # links between databases, matched by value (engine.crossdb)
    "crossdb_profile_values": 1000,     # distinct values sampled per text column
    "crossdb_profile_rows": 50000,      # rows read per column to sample them
    "crossdb_check_values": 200,        # values looked up in the other column per check
    "crossdb_min_shared": 3,            # sampled values two columns must share to be checked
    "crossdb_min_found": 3,             # values a trusted link must have found
    "crossdb_max_pairs": 5000,          # candidate column pairs checked at most
    "crossdb_common_value_columns": 50,  # a value in more columns than this links nothing
    # links inside a database (engine.relations)
    "relations_min_name_values": 3,     # distinct values a trusted link found by name must match
    "relations_min_same_name_values": 10,  # the same for a link of two same-named columns
    "relations_sample_scan_rows": 200000,   # rows of a natively read column read for a sample
    "relations_target_scan_rows": 500000,   # rows of an unindexed column read to check a link
    "relations_native_index_rows": 1000000,  # natively read tables this small are indexed
    "relations_cheap_scan_rows": 50000,     # unindexed tables this small are counted for menus
    "relations_key_sets": 16,               # value sets of unindexed columns kept in memory
    "relations_key_set_values": 300000,     # values one of those sets keeps at most
    # 'Show value from linked table' (the lookup display of a Browse column)
    "lookup_rows": 2000000,             # rows of the linked table read at most
    # Copy with related (engine.related_copy)
    "related_rows_per_link": 20,
    "related_hops": 2,
    "related_count_cap": 10000,
    "related_seen_rows": 500000,
    "related_small_table_rows": 50000,
    "related_scan_rows": 1000000,
    "related_preview_rows": 100,
    "clipboard_chars": 5 << 20,
    "markdown_cell_chars": 500,
    # the Database Map (engine.datamap)
    "map_exact_count_rows": 1000000,
    "map_type_rows": 2000,
    "map_blob_samples": 20,
    "map_blob_scan_rows": 5000,
    "map_blob_max_bytes": 4 << 20,
    "map_query_depth": 3,
    "map_query_joins": 12,
    "map_query_columns": 80,
    "map_queries": 100,
    "map_sample_rows": 5,
    "map_sample_chars": 80,
    # the Browse grid and position indexes (grid, engine.positions)
    "grid_window_rows": 200,            # rows read per grid window
    "grid_cache_windows": 24,           # grid windows kept in memory
    "grid_prefetch_windows": 2,         # windows read ahead in the scroll direction
    "grid_drag_debounce_ms": 40,        # wait before reading rows while the scrollbar is dragged
    "grid_copy_rows": 100000,           # rows copied at most in one go
    "grid_first_rows_wait_ms": 400,     # another table stays in view this long for new rows
    "checkpoint_every": 512,            # least spacing of position checkpoints (rows)
    "checkpoints_max": 100000,          # position checkpoints kept per table view
    "position_map_rows": 8000000,       # rows of a sorted/filtered view mapped to rowids (8 B each)
    "position_indexes_kept": 6,         # table views whose positions are kept per database
    "native_sort_rows": 200000,         # rows sorted/filtered in memory when SQLite cannot read
    # long text values (grid cells, tooltips, View value)
    "cell_draw_chars": 400,
    "cell_tip_chars": 3000,
    "cell_tip_lines": 40,
    "value_view_chars": 50000000,
    "value_view_line_chars": 2000,
    # opening, closing and the evidence (engine.session, engine.evidence)
    "ram_overlay_bytes": 1 << 30,       # memory the in-RAM view of the WAL may take
    "verify_rehash_bytes": 256 << 20,   # evidence up to this size is re-hashed on close
    "issues_kept": 10000,               # Issues kept per database (the rest are counted)
    # search, SQL, Find this value everywhere, related rows
    "sql_history": 100,                 # SQL queries kept in the history
    "value_search_rows": 1000,          # matching rows kept per table by Find this value everywhere
    "related_rows_shown": 200,          # rows of one related column the Related window lists
    # forensics (engine.forensics, the Forensics tab)
    "carve_max_records": 500000,        # records one recovery keeps at most
    "history_scan_rows": 2000000,       # rows a Row History of a WITHOUT ROWID table scans
    "forensics_history_keys": 20000,    # changed rows 'Find changed rows' lists
    "forensics_dropped_rows": 20000,    # rows of a dropped table shown
    "forensics_journal_rows": 50000,    # rows of the journal's pre-transaction table shown
    "live_hash_rows": 2000000,          # rows of a table hashed to spot live copies
    "live_index_entries": 2000000,      # entries of an index hashed to spot live copies
    "journal_records": 1 << 22,         # page records read from a rollback journal
    "audit_ptrmap_pages": 1000,         # pointer-map pages the audit checks
    # the timeline (engine.timeline, the Timeline tab)
    "timeline_sample_rows": 300,        # rows sampled from each end of a table to find dates
    "timeline_column_events": 50000,    # events read per column (the newest), by default
    "timeline_total_events": 1000000,   # events kept in one timeline
    "timeline_tag_rows": 500,           # events tagged at most from one selection
    # tags
    "tag_blob_bytes": 1 << 20,          # BLOBs up to this size are kept whole with a tag
    # the relationship diagram (engine.linkgraph)
    "diagram_group_min": 6,             # tables linked alike to one table drawn as one group
    "diagram_edge_labels": 20,          # lines labelled with their columns when this few
    "diagram_card_columns": 60,         # columns a table card of the diagram lists
    # the workspace UI: searchable dropdowns, column filters, the command palette, overview
    "dropdown_recent": 8,               # recent choices a dropdown lists first
    "filter_distinct_values": 1000,     # distinct values a column filter's checklist lists
    "filter_distinct_scan_rows": 5000000,  # rows read to count a column's distinct values
    "palette_results": 60,              # results the command palette lists
    "palette_recent": 12,               # recent palette choices kept
    "overview_date_tables": 400,        # tables of one database looked at for its date range
    "ui_stall_ms": 2000,                # the smoke run fails when the event loop stalls longer
    # HTML reports and exports (engine.html_report)
    "html_rows_per_part": 250000,       # rows of one table per HTML file (more: part files)
    "html_chunk_rows": 2000,            # rows per embedded JSON block of an HTML report
    "html_print_rows": 500,             # rows per table shown without JavaScript and printed
    "html_cell_chars": 2000,            # characters of a value in those plain tables
    "html_thumb_bytes": 256 << 10,      # image BLOBs up to this size are shown as pictures
    "html_decoded_bytes": 256 << 10,    # BLOBs up to this size get their decoded value
    "html_decoded_chars": 20000,        # characters of that decoded value kept
    "html_top_distinct": 10000,         # distinct values counted per column for 'top values'
    # untrusted input: SQL from the evidence, native parsing, decoders, previews
    "schema_replay_steps": 2000000,     # SQLite steps one replayed CREATE statement may take
    "schema_value_bytes": 16 << 20,     # largest value in a schema replay (Python 3.11+)
    "schema_sql_bytes": 16 << 20,       # longest CREATE statement replayed (Python 3.11+)
    "sql_value_bytes": 64 << 20,        # largest value SQLite may build or read (3.11+)
    "sql_view_steps": 250000000,        # SQLite steps of one read of a view / computed column
    "sql_window_bytes": 256 << 20,      # bytes of computed values one grid window keeps
    "native_payload_bytes": 1 << 30,    # bytes of one record assembled by the native reader
    "btree_max_depth": 64,              # b-tree levels the native reader follows
    "wal_frames": 10000000,             # WAL frames read when a database is opened
    "decode_max_depth": 16,             # nested BLOBs a decode may go down (highest choice)
    "decode_max_nodes": 50000,          # parts one BLOB decode produces
    "decode_max_output": 64 << 20,      # bytes one decompressed stream may produce
    "decode_total_output": 128 << 20,   # decompressed bytes of one BLOB decode in all
    "decode_max_nest": 100,             # structure nesting one decode follows
    "decode_max_steps": 400000,         # parse steps of one BLOB decode
    "summary_max_depth": 3,             # the same for the one-line summaries of grid cells
    "summary_max_nodes": 4000,
    "summary_max_output": 8 << 20,
    "summary_total_output": 16 << 20,
    "summary_max_steps": 40000,
    "decode_lzma_memory": 64 << 20,     # memory the xz / lzma decoder may take
    "decode_members": 64,               # concatenated compressed members followed
    "decode_lz_frames": 4096,           # LZ4 / zstd frames followed in one buffer
    "decode_checksum_bytes": 16 << 20,  # pure-Python checksums over more are skipped
    "protobuf_max_nest": 32,            # nested protobuf messages followed
    "protobuf_max_packed": 4096,        # values of one packed protobuf field
    "typedstream_max_array": 1 << 20,   # elements of one typedstream array
    "preview_pixels": 40000000,         # pixels of an image previewed (width x height)
}

RANGES = {
    "case_max_databases": (1, 1000),
    "case_search_parallel": (1, 32),
    "case_timeline_parallel": (1, 32),
    "folder_scan_files": (100, 10000000),
    "hash_parallel": (1, 64),
    "crossdb_profile_values": (10, 100000),
    "crossdb_profile_rows": (100, 100000000),
    "crossdb_check_values": (10, 100000),
    "crossdb_min_shared": (1, 1000),
    "crossdb_min_found": (3, 1000),
    "crossdb_max_pairs": (1, 10000000),
    "crossdb_common_value_columns": (2, 100000),
    "relations_min_name_values": (3, 1000),
    "relations_min_same_name_values": (3, 1000),
    "relations_sample_scan_rows": (1000, 1 << 40),
    "relations_target_scan_rows": (1000, 1 << 40),
    "relations_native_index_rows": (1, 1 << 40),
    "relations_cheap_scan_rows": (0, 1 << 40),
    "relations_key_sets": (0, 10000),
    "relations_key_set_values": (0, 1 << 32),
    "lookup_rows": (1000, 1000000000),
    "related_rows_per_link": (1, 100000),
    "related_hops": (1, 3),
    "related_count_cap": (100, 10000000),
    "related_seen_rows": (1000, 50000000),
    "related_small_table_rows": (0, 10000000),
    "related_scan_rows": (0, 100000000),
    "related_preview_rows": (1, 100000),
    "clipboard_chars": (1024, 1 << 30),
    "markdown_cell_chars": (20, 1000000),
    "map_exact_count_rows": (0, 1 << 62),
    "map_type_rows": (10, 10000000),
    "map_blob_samples": (1, 10000),
    "map_blob_scan_rows": (10, 10000000),
    "map_blob_max_bytes": (1024, 1 << 31),
    "map_query_depth": (1, 6),
    "map_query_joins": (1, 64),
    "map_query_columns": (5, 2000),
    "map_queries": (1, 100000),
    "map_sample_rows": (1, 10000),
    "map_sample_chars": (10, 100000),
    "grid_window_rows": (20, 5000),
    "grid_cache_windows": (4, 2000),
    "grid_prefetch_windows": (0, 50),
    "grid_drag_debounce_ms": (0, 2000),
    "grid_copy_rows": (1, 50000000),
    "grid_first_rows_wait_ms": (0, 5000),
    "checkpoint_every": (16, 1000000),
    "checkpoints_max": (100, 10000000),
    "position_map_rows": (0, 500000000),
    "position_indexes_kept": (1, 256),
    "native_sort_rows": (1000, 100000000),
    "cell_draw_chars": (20, 20000),
    "cell_tip_chars": (100, 200000),
    "cell_tip_lines": (3, 1000),
    "value_view_chars": (1000, 1000000000),
    "value_view_line_chars": (100, 1000000),
    "ram_overlay_bytes": (0, 1 << 40),
    "verify_rehash_bytes": (0, 1 << 44),
    "issues_kept": (100, 10000000),
    "sql_history": (1, 100000),
    "value_search_rows": (10, 100000000),
    "related_rows_shown": (10, 10000000),
    "carve_max_records": (1000, 100000000),
    "history_scan_rows": (1000, 1 << 40),
    "forensics_history_keys": (100, 100000000),
    "forensics_dropped_rows": (100, 100000000),
    "forensics_journal_rows": (100, 100000000),
    "live_hash_rows": (0, 1 << 40),
    "live_index_entries": (0, 1 << 40),
    "journal_records": (1, 1 << 40),
    "audit_ptrmap_pages": (1, 1 << 32),
    "timeline_sample_rows": (10, 1000000),
    "timeline_column_events": (100, 100000000),
    "timeline_total_events": (1000, 500000000),
    "timeline_tag_rows": (1, 10000000),
    "tag_blob_bytes": (1024, 1 << 31),
    "diagram_group_min": (2, 1000000),
    "diagram_edge_labels": (0, 1000000),
    "diagram_card_columns": (1, 100000),
    "dropdown_recent": (0, 1000),
    "filter_distinct_values": (10, 10000000),
    "filter_distinct_scan_rows": (1000, 1 << 40),
    "palette_results": (5, 100000),
    "palette_recent": (0, 1000),
    "overview_date_tables": (1, 1000000),
    "ui_stall_ms": (50, 600000),
    "html_rows_per_part": (10, 500000),
    "html_chunk_rows": (10, 100000),
    "html_print_rows": (0, 100000),
    "html_cell_chars": (50, 10000000),
    "html_thumb_bytes": (0, 64 << 20),
    "html_decoded_bytes": (0, 64 << 20),
    "html_decoded_chars": (100, 10000000),
    "html_top_distinct": (10, 10000000),
    "schema_replay_steps": (10000, 1 << 40),
    "schema_value_bytes": (1 << 16, (1 << 31) - 1),
    "schema_sql_bytes": (1 << 16, (1 << 30) - 1),
    "sql_value_bytes": (1 << 20, (1 << 31) - 1),
    "sql_view_steps": (100000, 1 << 50),
    "sql_window_bytes": (1 << 20, 1 << 40),
    "native_payload_bytes": (1 << 20, 1 << 40),
    "btree_max_depth": (8, 1000),
    "wal_frames": (1000, 1 << 40),
    "decode_max_depth": (1, 64),
    "decode_max_nodes": (100, 100000000),
    "decode_max_output": (1 << 16, 1 << 40),
    "decode_total_output": (1 << 16, 1 << 40),
    "decode_max_nest": (4, 900),
    "decode_max_steps": (1000, 1 << 40),
    "summary_max_depth": (0, 64),
    "summary_max_nodes": (10, 100000000),
    "summary_max_output": (1 << 12, 1 << 40),
    "summary_total_output": (1 << 12, 1 << 40),
    "summary_max_steps": (100, 1 << 40),
    "decode_lzma_memory": (1 << 20, 1 << 40),
    "decode_members": (1, 1000000),
    "decode_lz_frames": (1, 10000000),
    "decode_checksum_bytes": (0, 1 << 40),
    "protobuf_max_nest": (1, 500),
    "protobuf_max_packed": (1, 100000000),
    "typedstream_max_array": (1, 1 << 32),
    "preview_pixels": (10000, 1 << 34),
}

DESCRIPTIONS = {
    "case_max_databases": "databases open together in a case",
    "case_search_parallel": "databases searched side by side",
    "case_timeline_parallel": "databases read side by side by the timeline",
    "folder_scan_files": "files looked at by one Open Folder scan",
    "hash_parallel": "databases whose evidence is hashed at the same time",
    "crossdb_profile_values": "distinct values sampled per text column (links between "
                              "databases)",
    "crossdb_profile_rows": "rows read per column to sample them",
    "crossdb_check_values": "values looked up in the other column per check",
    "crossdb_min_shared": "sampled values two columns must share to be checked",
    "crossdb_min_found": "values a trusted link between databases must have found",
    "crossdb_max_pairs": "candidate column pairs checked at most",
    "crossdb_common_value_columns": "a value in more columns than this links nothing",
    "relations_min_name_values": "distinct values a link found by a column's name must match "
                                 "to be trusted (fewer: weaker)",
    "relations_min_same_name_values": "distinct values a link of two same-named columns must "
                                      "match to be trusted (fewer: weaker)",
    "relations_sample_scan_rows": "rows of a natively read column read to sample its values "
                                  "for a link check (the check says when it stopped there)",
    "relations_target_scan_rows": "rows of an unindexed column read to check a link's values "
                                  "(the check says when it stopped there)",
    "relations_native_index_rows": "natively read tables up to this many rows are indexed in "
                                   "memory to follow links; larger ones are read through their "
                                   "own index or scanned (slower, nothing left out)",
    "relations_cheap_scan_rows": "unindexed tables up to this many rows are counted at once for "
                                 "a menu; larger ones are counted when asked",
    "relations_key_sets": "value sets of unindexed columns kept in memory for link checks",
    "relations_key_set_values": "values one of those sets keeps (a larger column is read again "
                                "for each check)",
    "lookup_rows": "rows of a linked table read by 'Show value from linked table'",
    "related_rows_per_link": "related rows kept per link and row (the rest are counted)",
    "related_hops": "links followed from a row at most",
    "related_count_cap": "related rows counted per link and row beyond the kept ones",
    "related_seen_rows": "rows remembered so a row met again is named, not repeated",
    "related_small_table_rows": "an unindexed table this small is read once to follow links",
    "related_scan_rows": "an unindexed table this small is read once for a small selection",
    "related_preview_rows": "rows the preview of Copy with related shows",
    "clipboard_chars": "characters Copy puts on the clipboard",
    "markdown_cell_chars": "characters of a value in a Markdown cell",
    "map_exact_count_rows": "tables estimated larger than this get an estimated row count",
    "map_type_rows": "rows whose storage classes are counted per table",
    "map_blob_samples": "BLOB values decoded per column",
    "map_blob_scan_rows": "rows looked at for BLOB values per column",
    "map_blob_max_bytes": "larger BLOBs are counted, not decoded",
    "map_query_depth": "links followed by a generated query",
    "map_query_joins": "joins in one generated query",
    "map_query_columns": "columns in one generated query",
    "map_queries": "generated queries in a map",
    "map_sample_rows": "sample rows per table, when asked for",
    "map_sample_chars": "characters of a sample value",
    "grid_window_rows": "rows a grid reads at a time",
    "grid_cache_windows": "grid windows (of grid_window_rows rows) kept in memory",
    "grid_prefetch_windows": "windows read ahead in the scroll direction",
    "grid_drag_debounce_ms": "milliseconds the scrollbar thumb rests before rows are read",
    "grid_copy_rows": "rows a grid copies at most in one go",
    "grid_first_rows_wait_ms": "milliseconds the last table stays in view for a new table's rows",
    "checkpoint_every": "rows between two position checkpoints of a Browse view, at least",
    "checkpoints_max": "position checkpoints kept per Browse view (their spacing grows)",
    "position_map_rows": "rows of a sorted or filtered Browse view mapped to their rowids "
                         "(8 bytes a row); larger views scroll more slowly",
    "position_indexes_kept": "Browse views (table, sort, filter) indexed per database",
    "native_sort_rows": "rows sorted or filtered in memory for a table SQLite cannot read",
    "cell_draw_chars": "characters of a value drawn in a grid cell",
    "cell_tip_chars": "characters of a value shown in a cell's tooltip",
    "cell_tip_lines": "lines of a value shown in a cell's tooltip",
    "value_view_chars": "characters of a value shown by View value",
    "value_view_line_chars": "longer lines are shown in pieces by View value",
    "ram_overlay_bytes": "memory the in-RAM view of the WAL may take (about twice the database "
                         "size is needed; a larger database is read with SQL seeing the main "
                         "file only). Takes effect when a database is opened",
    "verify_rehash_bytes": "evidence files up to this total size are re-hashed (SHA-256) when "
                           "closed; larger ones are checked by size and time",
    "issues_kept": "Issues kept per database (the rest are counted)",
    "sql_history": "SQL queries kept in the SQL tab's history",
    "value_search_rows": "matching rows kept per table by Find this value everywhere",
    "related_rows_shown": "rows of one related column the Related window lists",
    "carve_max_records": "records one Recover deleted records keeps at most",
    "history_scan_rows": "rows a Row History of a table without rowid scans for the row",
    "forensics_history_keys": "changed rows Find changed rows lists",
    "forensics_dropped_rows": "rows of a dropped table the Forensics tab shows",
    "forensics_journal_rows": "rows of a table's pre-transaction state (rollback journal) shown",
    "live_hash_rows": "rows of a table hashed to tell a recovered record from a live copy (and "
                      "an older version) when it has no rowid; a larger table's records are not "
                      "compared, and Recovered Records says so",
    "live_index_entries": "entries of an index hashed to tell a recovered index entry from a "
                          "live one; a larger index's entries are not compared, and Recovered "
                          "Records says so",
    "journal_records": "page records read from a rollback journal (the Journal sub-tab says "
                       "when it stopped there)",
    "audit_ptrmap_pages": "pointer-map pages the audit checks for invalid entries (the finding "
                          "says when it checked only some)",
    "timeline_sample_rows": "rows sampled from each end of a table to find its date columns",
    "timeline_column_events": "events read per date column, the newest first (the Timeline's "
                              "default for Max events per column)",
    "timeline_total_events": "events kept in one timeline (the newest)",
    "timeline_tag_rows": "timeline events tagged at most from one selection",
    "tag_blob_bytes": "BLOBs up to this size are kept whole with a tag; of a larger one the "
                      "size, SHA-256 and first 64 KiB are kept",
    "diagram_group_min": "tables linked to the same table in exactly the same way that the "
                         "Relationships diagram draws as one group box (the box names the "
                         "count; click it to list them); fewer are drawn one by one",
    "diagram_edge_labels": "lines of the Relationships diagram labelled with their columns "
                           "when there are at most this many; with more, hover a line or click "
                           "a table to read them",
    "diagram_card_columns": "columns a table card of the Relationships diagram lists (the "
                            "card says how many more there are)",
    "dropdown_recent": "recent choices a dropdown lists first",
    "filter_distinct_values": "distinct values a column filter's checklist lists (the most "
                              "frequent first; the checklist says when there are more)",
    "filter_distinct_scan_rows": "rows read to count a column's distinct values for its filter "
                                 "checklist (the checklist says when it stopped there)",
    "palette_results": "results the command palette (Ctrl+K) lists (it says how many more "
                       "match)",
    "palette_recent": "recent command palette choices kept",
    "overview_date_tables": "tables of one database the Overview looks at for dates (it says "
                            "when a database has more)",
    "ui_stall_ms": "the longest the window may stop answering (one event of the Tk loop) in "
                   "the scripted UI check before it is reported as an error",
    "html_rows_per_part": "rows of one table an HTML report or export holds per file; a larger "
                          "table continues in part files next to it, which the report lists "
                          "(at most 500,000: browsers cannot scroll a longer table)",
    "html_chunk_rows": "rows per block of data embedded in an HTML report (the page reads them "
                       "block by block while it opens)",
    "html_print_rows": "rows of each table an HTML report shows as a plain table without "
                       "JavaScript and prints (the report says when a table has more)",
    "html_cell_chars": "characters of a value those plain tables show (the full value stays "
                       "in the report's data and its row details)",
    "html_thumb_bytes": "image BLOBs up to this size are shown as pictures in HTML reports; a "
                        "larger one is described and the report says why it is not shown",
    "html_decoded_bytes": "BLOBs up to this size get their decoded value (XML plist, protobuf "
                          "fields or JSON) in an HTML report's row details; 0: none",
    "html_decoded_chars": "characters of a BLOB's decoded value an HTML report keeps; the "
                          "report says when one is cut",
    "html_top_distinct": "distinct values an HTML report counts per column for its 'top "
                         "values' (it says when a column has more)",
    "schema_replay_steps": "SQLite steps one CREATE statement of the evidence may take when it "
                           "is replayed in an empty scratch database to read its columns (a "
                           "statement stopped there gives no columns, and an Issue says so)",
    "schema_value_bytes": "largest value a schema replay may build (Python 3.11 or later)",
    "schema_sql_bytes": "longest CREATE statement replayed to read a table's columns (Python "
                        "3.11 or later)",
    "sql_value_bytes": "largest value SQLite may build or read on the evidence (Python 3.11 or "
                       "later); a table holding a larger value is read natively, a view "
                       "computing one stops and says so",
    "sql_view_steps": "SQLite steps one read of a view (or of a table's computed columns) may "
                      "take; the view stops and says so",
    "sql_window_bytes": "bytes of computed values (views, generated columns) one grid window "
                        "keeps; the window says when it stopped there",
    "native_payload_bytes": "bytes of one record the native reader assembles (a record "
                            "declaring more is cut there, and an Issue says so)",
    "btree_max_depth": "b-tree levels the native reader follows down (deeper pages are "
                       "reported as a damaged tree)",
    "wal_frames": "WAL frames read when a database is opened (the banners say when frames "
                  "after them were not read)",
    "decode_max_depth": "nested BLOBs the BLOB inspector may decode down (the highest depth "
                        "it offers)",
    "decode_max_nodes": "parts one BLOB decode produces (the decode says when it stopped)",
    "decode_max_output": "bytes one decompressed stream may produce in a BLOB decode",
    "decode_total_output": "decompressed bytes one BLOB decode may produce in all",
    "decode_max_nest": "structure nesting (plists, protobuf, JSON) one BLOB decode follows",
    "decode_max_steps": "parse steps one BLOB decode may take",
    "summary_max_depth": "nested BLOBs decoded for a grid cell's one-line summary",
    "summary_max_nodes": "parts decoded for a grid cell's one-line summary",
    "summary_max_output": "bytes one decompressed stream may produce for a summary",
    "summary_total_output": "decompressed bytes one summary may produce in all",
    "summary_max_steps": "parse steps one summary may take",
    "decode_lzma_memory": "memory the xz / lzma decoder may take for one stream (a stream "
                          "declaring a larger dictionary is not decoded, and says so)",
    "decode_members": "concatenated gzip / bzip2 / xz / zstd members followed in one BLOB",
    "decode_lz_frames": "LZ4 / zstd frames followed in one BLOB",
    "decode_checksum_bytes": "streams up to this size have their checksums verified (pure "
                             "Python checksums are slow; a larger one says it was not checked)",
    "protobuf_max_nest": "nested protobuf messages decoded",
    "protobuf_max_packed": "values of one packed protobuf field decoded",
    "typedstream_max_array": "elements of one typedstream array decoded",
    "preview_pixels": "pixels (width x height) of an image the inspector and the record view "
                      "show; a larger image is described, not drawn, and says why",
}

# name -> (default, minimum, maximum, what it limits): the table a settings window lists
LIMITS = dict((k, (DEFAULTS[k], RANGES[k][0], RANGES[k][1], DESCRIPTIONS.get(k, k)))
              for k in DEFAULTS)

_lock = threading.Lock()
_values = dict(DEFAULTS)
problems = []                       # what the last load() refused, in plain words


def describe(name):
    return DESCRIPTIONS.get(name, name)


def hint(name):
    """How a message names a limit the user can change."""
    return "limit %s (Limits…, settings.json)" % name


def validate(overrides):
    """(values, problems): DEFAULTS with the valid overrides applied. Unknown names, values
    that are not whole numbers, and values out of range are refused (the default stays)."""
    values = dict(DEFAULTS)
    out = []
    if overrides is None:
        return values, out
    if not isinstance(overrides, dict):
        return values, ["settings 'limits' is not a set of name: value pairs; defaults used"]
    for name, v in sorted(overrides.items(), key=lambda kv: str(kv[0])):
        if name not in DEFAULTS:
            out.append("unknown limit %r ignored" % (name,))
            continue
        if isinstance(v, bool) or not isinstance(v, int):
            out.append("limit %s = %r is not a whole number; the default %s is used"
                       % (name, v, DEFAULTS[name]))
            continue
        lo, hi = RANGES.get(name, (None, None))
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            out.append("limit %s = %s is outside %s..%s; the default %s is used"
                       % (name, v, lo, hi, DEFAULTS[name]))
            continue
        values[name] = v
    return values, out


def load(settings):
    """Apply the 'limits' of the app settings (a dict, as engine.tags.load_settings gives);
    returns the problems found (also kept in `problems`)."""
    values, found = validate((settings or {}).get("limits") if isinstance(settings, dict)
                             else None)
    with _lock:
        _values.clear()
        _values.update(values)
        del problems[:]
        problems.extend(found)
    return list(found)


def get(name):
    """The limit's value (the default unless settings.json overrides it)."""
    with _lock:
        return _values.get(name, DEFAULTS[name])


def current():
    """Every limit's value now (a copy)."""
    with _lock:
        return dict(_values)


def checked(overrides):
    """A full set of limits for one job: the values in force now, with the valid overrides
    applied (the invalid ones keep the value in force). overrides may be a full set."""
    base = current()
    if not isinstance(overrides, dict):
        return base
    for k, v in overrides.items():
        if k in DEFAULTS and valid(k, v):
            base[k] = v
    return base


def valid(name, v):
    """True for a whole number within the limit's range."""
    if name not in DEFAULTS or isinstance(v, bool) or not isinstance(v, int):
        return False
    lo, hi = RANGES[name]
    return lo <= v <= hi


def reset():
    """Back to the defaults (tests)."""
    with _lock:
        _values.clear()
        _values.update(DEFAULTS)
        del problems[:]
