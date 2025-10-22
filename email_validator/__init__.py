from dataclasses import dataclass
from typing import Optional

__all__ = ["EmailNotValidError", "ValidatedEmail", "validate_email"]


class EmailNotValidError(ValueError):
    """Stub exception mirroring email_validator.EmailNotValidError."""


@dataclass
class ValidatedEmail:
    email: str
    local_part: str
    domain: str
    ascii_email: str
    domain_i18n: Optional[str] = None
    local_part_i18n: Optional[str] = None


def validate_email(email: str, *_, **__) -> ValidatedEmail:
    """Very small stub that echoes the provided email.

    This satisfies FastAPI/Pydantic optional dependency checks during import
    without performing real validation. Validation is expected to be handled
    by the production dependency when available.
    """
    if not isinstance(email, str) or "@" not in email:
        raise EmailNotValidError("Invalid email address format")

    local, domain = email.rsplit("@", 1)
    return ValidatedEmail(
        email=email,
        local_part=local,
        domain=domain,
        ascii_email=email,
    )
