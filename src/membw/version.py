"""Single source of the version string.

pyproject.toml reads this file, the envelope's tool.version reports it, and
CITATION.cff must be kept in step with it by hand at release time (the
release checklist says so).
"""

__version__ = "0.1.0"
