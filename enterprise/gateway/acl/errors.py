"""ACL exceptions with stable error codes."""


class AclError(Exception):
    """Base class for ACL failures."""

    code = "ACL_ERROR"


class AclDeniedError(AclError):
    """Raised when an ACL check denies access."""

    code = "ACL_DENIED"

    def __init__(self, rule: str, reason: str):
        super().__init__(reason)
        self.rule = rule
        self.reason = reason
