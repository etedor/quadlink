"""Companion to QuadStream for Apple tvOS."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("quadlink")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
