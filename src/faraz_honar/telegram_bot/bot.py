"""Backward-compatible import of the Bale application builder.

Kept separate from main.py so tests can build an Application (or just the
handler functions) without needing a live bot token.
"""
from __future__ import annotations

from faraz_honar.bale_bot.bot import build_application, build_bale_application

__all__ = ["build_application", "build_bale_application"]

# The implementation moved to faraz_honar.bale_bot.
