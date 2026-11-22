"""Deterministic style diagnostics and conservative patches for Chinese prose."""

from .rewrite import polish_style
from .scanner import MAX_TEXT, PROFILES, scan_style

__all__ = ["MAX_TEXT", "PROFILES", "polish_style", "scan_style"]
