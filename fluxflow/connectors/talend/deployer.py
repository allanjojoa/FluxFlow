"""Talend deploy module — re-exports from the unified TalendConnector.

The :class:`TalendConnector` in ``builder.py`` handles both build (diff) and
deploy (create/update) operations. This module exists to keep the package
structure explicit and provide a convenient import path.
"""

from fluxflow.connectors.talend.builder import TalendConnector

__all__ = ["TalendConnector"]
