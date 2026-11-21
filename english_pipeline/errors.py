class PipelineError(Exception):
    """Base class for deterministic pipeline failures."""


class ValidationError(PipelineError):
    pass


class FormalModelReviewPending(ValidationError):
    """The exact batch can resume pre-transaction review; formal apply is unstarted."""


class IdempotencyConflict(PipelineError):
    pass


class SourceHashMismatch(PipelineError):
    pass


class CompareAndSwapConflict(PipelineError):
    pass


class AuthorizationError(PipelineError):
    pass
