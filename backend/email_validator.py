class EmailNotValidError(ValueError):
    pass


def validate_email(email: str, allow_smtputf8: bool = True, *_, **__) -> object:
    if "@" not in email:
        raise EmailNotValidError("Invalid email address")
    return type("ValidatedEmail", (), {"email": email})()
