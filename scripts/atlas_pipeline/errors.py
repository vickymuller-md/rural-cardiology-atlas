"""Fail-closed exception types used by the V19 pipeline."""


class AtlasPipelineError(RuntimeError):
    """Base error for a preregistered contract violation."""


class ContractError(AtlasPipelineError):
    """A source row or derived table violates its fixed schema or semantics."""


class IntegrityError(AtlasPipelineError):
    """Bytes, identifiers, accounting, or canonical output failed integrity."""


class AcquisitionError(AtlasPipelineError):
    """A controlled transfer or response snapshot is invalid."""
