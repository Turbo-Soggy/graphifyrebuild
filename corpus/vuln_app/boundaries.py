"""Boundary cases for file:line -> node resolution.

graph.json stores no function extents, only a start line, so resolution uses
"nearest preceding callable". These cases probe where that heuristic can
mis-attribute a finding to a function that does NOT contain the line.
"""

import os


def first_function():
    """A function whose body ends well before the next definition."""
    return 1


# --- module level, AFTER first_function ends -------------------------------
# A SAST finding here (hardcoded secret, taint source in config) belongs to the
# MODULE, not to first_function. Nearest-preceding-callable attributes it to
# first_function anyway.
MODULE_LEVEL_SECRET = os.environ.get("APP_SECRET", "")


def second_function():
    """The next definition after the module-level gap."""
    return MODULE_LEVEL_SECRET


LAST_MODULE_LEVEL = 42
