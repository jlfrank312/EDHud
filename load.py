"""
ED HUD - EDMarketConnector plugin.

On-screen cargo and mission tracking drawn by a built-in, click-through
overlay (no separate overlay program required).

Install: copy this whole folder to  %LOCALAPPDATA%\\EDMarketConnector\\plugins\\EDHud
(the folder name must stay a valid Python name - no hyphens or spaces).
"""
from __future__ import annotations

import logging
import os
import sys

from config import appname, config

plugin_name = os.path.basename(os.path.dirname(__file__))
logger = logging.getLogger(f"{appname}.{plugin_name}")
if not logger.hasHandlers():
    logger.setLevel(logging.INFO)
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(module)s:%(lineno)d: %(message)s"))
    logger.addHandler(_h)

# make sibling modules importable regardless of how EDMC imported load.py
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import myNotebook as nb  # noqa: E402
from monitor import monitor  # noqa: E402

from edhud_app import Controller  # noqa: E402

PLUGIN_TITLE = "ED HUD"
_ctl = None


def _journal_dir():
    """Where the game's journals live. monitor.currentdir is set once EDMC is watching them."""
    d = getattr(monitor, "currentdir", None)
    if d:
        return str(d)
    return config.get_str("journaldir") or config.default_journal_dir


def plugin_start3(plugin_dir):
    global _ctl
    _ctl = Controller(plugin_dir, config, _journal_dir)
    logger.info("ED HUD loaded from %s", plugin_dir)
    return PLUGIN_TITLE


def plugin_app(parent):
    return _ctl.build_main_ui(parent) if _ctl else None


def journal_entry(cmdr, is_beta, system, station, entry, state):
    if _ctl:
        _ctl.on_journal(cmdr, system, station, entry, state)


def dashboard_entry(cmdr, is_beta, entry):
    if _ctl:
        _ctl.on_status(entry)


def plugin_prefs(parent, cmdr, is_beta):
    return _ctl.build_prefs(parent, nb) if _ctl else None


def prefs_changed(cmdr, is_beta):
    if _ctl:
        _ctl.apply_prefs()


def plugin_stop():
    if _ctl:
        _ctl.stop()
