"""GTAVI - Grab The Action, Viral Instantly. Clip live streams the moment it happens."""
__version__ = "0.1.0"

from .clipper import FORMATS
from .session import ClipInfo, ClipSession
from .sources import Source, parse_source

__all__ = ["FORMATS", "ClipInfo", "ClipSession", "Source", "parse_source", "__version__"]
