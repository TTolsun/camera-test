"""Patterns shared by logcat collection, comparison and feature checks."""

import re


LOG_TAG_PATTERN = re.compile(r"[A-Za-z0-9_.-]+")
LOG_CLOCK_PATTERN = re.compile(r"\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}")
LOG_LINE = re.compile(r"^\S+\s+\S+\s+(\d+)\s+\d+\s+[VDIWEFAS]\s+([^:]+?)\s*:\s*(.*)$")
