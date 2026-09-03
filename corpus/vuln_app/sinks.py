"""Dangerous sinks for the taint-validation corpus."""

import os
import sqlite3


def run_sql(conn: sqlite3.Connection, statement: str):
    """SINK: executes a SQL string verbatim."""
    return conn.execute(statement)


def run_shell(command: str):
    """SINK: passes a string to the shell."""
    return os.system(command)


def render_html(fragment: str) -> str:
    """SINK: emits an HTML fragment without escaping."""
    return f"<div>{fragment}</div>"


def sanitize(value: str) -> str:
    """SANITIZER: neutralises a value before it reaches a sink."""
    return "".join(ch for ch in str(value) if ch.isalnum() or ch in " _-")
