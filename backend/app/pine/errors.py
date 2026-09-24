class PineError(Exception):
    """Base for anything wrong with a user-supplied Pine Script — always
    surfaced to the UI as a readable message, never a raw traceback."""


class PineParseError(PineError):
    def __init__(self, message: str, line_no: int | None = None):
        self.line_no = line_no
        prefix = f"Line {line_no}: " if line_no else ""
        super().__init__(f"{prefix}{message}")


class PineUnsupportedError(PineError):
    """Raised for syntax that is valid Pine Script but outside the supported
    subset (arrays, matrices, security(), libraries, drawing objects, ...)."""

    def __init__(self, feature: str, line_no: int | None = None):
        self.line_no = line_no
        self.feature = feature
        prefix = f"Line {line_no}: " if line_no else ""
        super().__init__(f"{prefix}Unsupported construct: {feature}")
