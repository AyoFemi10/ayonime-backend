"""
AnimePahe Downloader

A Python application for downloading anime episodes from AnimePahe.
"""

__version__ = "0.4.0"

# Main entry points — GUI import is optional (not available on headless servers)
from .main import main
from .cli import cli_main, run_interactive_mode

try:
    from .gui import run_gui
except ImportError:
    run_gui = None  # type: ignore

__all__ = ['main', 'cli_main', 'run_interactive_mode', 'run_gui']
