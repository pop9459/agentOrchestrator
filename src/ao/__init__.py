"""agentOrchestrator (`ao`)."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ao")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0"
