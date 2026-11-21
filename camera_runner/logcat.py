"""Patterns shared by logcat collection, comparison and feature checks."""

from datetime import datetime
import re


LOG_TAG_PATTERN = re.compile(r"[A-Za-z0-9_.-]+")
LOG_CLOCK_PATTERN = re.compile(r"\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}")
LOG_LINE = re.compile(r"^\S+\s+\S+\s+(\d+)\s+\d+\s+[VDIWEFAS]\s+([^:]+?)\s*:\s*(.*)$")
LOG_STAMP = re.compile(r"^(\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s")


def line_stamp(line):
    """The leading MM-DD HH:MM:SS.mmm timestamp of a threadtime line, or None."""
    found = LOG_STAMP.match(line)
    return found[1] if found else None


def elapsed_ms(earlier, later):
    """Whole milliseconds between two year-less logcat stamps, or None when they cannot be compared.

    Both stamps are read as the same (leap) year, so a span across New Year sorts backwards and is dropped.
    """
    try:
        start, end = (datetime.strptime(f"2000-{stamp}", "%Y-%m-%d %H:%M:%S.%f") for stamp in (earlier, later))
    except (TypeError, ValueError):
        return None
    return round((end - start).total_seconds() * 1000) if end >= start else None
