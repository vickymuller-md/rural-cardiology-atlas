"""Deterministic, offline-first analytical contracts for Atlas V19.

Network acquisition and analytical construction intentionally live in separate
modules.  Importing this package never performs I/O or opens an analytical
source file.
"""

from .errors import AcquisitionError, ContractError, IntegrityError

__all__ = ["AcquisitionError", "ContractError", "IntegrityError"]
