class PipelineError(Exception):
    """Base class for deterministic pipeline failures."""


class ValidationError(PipelineError):
    pass


class IdempotencyConflict(PipelineError):
    pass


class SourceHashMismatch(PipelineError):
    pass


class CompareAndSwapConflict(PipelineError):
    pass


class AuthorizationError(PipelineError):
    pass
