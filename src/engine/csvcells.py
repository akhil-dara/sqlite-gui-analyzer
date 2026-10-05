"""The two rules every CSV the tool writes follows (exports, tagged rows, timeline, forensic
report, activity log, links, copied rows), in one place:

- Spreadsheet-safe text (on by default): a text value starting with = + - @, a tab or a
  carriage return gets a ' in front, so a spreadsheet shows it as text instead of running it
  as a formula. Numbers are never changed. The HTML report's in-browser CSV download uses
  the same rule (engine.html_report_assets: csvField).
- NUL: the character U+0000 is written as the four characters \\x00 (Python 3.8-3.10's csv
  module cannot write it at all; a text holding the four characters \\x00 looks the same).

csv_writer() wraps csv.writer with the NUL rule for every text cell (and the formula rule
when asked), and turns csv.Error into OSError so callers that handle a failed write handle
this too.
"""

import csv

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
NUL_TEXT = "\\x00"
FORMULA_NOTE = ("spreadsheet-safe: a text value starting with = + - @, a tab or a carriage "
                "return has a ' added in front (so it is not run as a formula)")
RAW_NOTE = "as it is, no spreadsheet formula escaping"
NUL_NOTE = "the character NUL (U+0000) is written as the four characters \\x00"


def nul_safe(text):
    """text with every NUL written as the four characters \\x00."""
    return text.replace("\x00", NUL_TEXT) if "\x00" in text else text


def formula_safe(text):
    """text with a ' in front when it starts with = + - @, a tab or a carriage return."""
    return "'" + text if text.startswith(FORMULA_PREFIXES) else text


def csv_text(text, formulas=True):
    """A text value as written in a CSV cell: NUL escaped, and formula_safe() when formulas."""
    text = nul_safe(text)
    return formula_safe(text) if formulas else text


class _Writer(object):
    """csv.writer whose rows go through csv_text() (str cells only; numbers stay numbers)."""

    def __init__(self, f, formulas, kw):
        self._w = csv.writer(f, **kw)
        self.formulas = formulas

    def _cells(self, row):
        return [csv_text(c, self.formulas) if isinstance(c, str) else c for c in row]

    def writerow(self, row):
        try:
            return self._w.writerow(self._cells(row))
        except csv.Error as e:
            raise OSError("cannot write a CSV row: %s" % e)

    def writerows(self, rows):
        for row in rows:
            self.writerow(row)


def csv_writer(f, formulas=True, **kw):
    """A csv.writer for f applying the NUL rule to every text cell and, with formulas, the
    spreadsheet-safe rule; csv.Error is raised as OSError."""
    return _Writer(f, formulas, kw)
