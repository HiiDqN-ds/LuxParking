"""Password policy shared by every place a password can be set (team, owner, self-service, seed).

Length is the main control (NIST SP 800-63B favours long passwords over composition rules), plus a block list of
the passwords attackers try first and a check against values derived from the account itself.
"""
from __future__ import annotations

MIN_LENGTH = 12
MAX_LENGTH = 128

# Small embedded block list: the most common passwords that already satisfy a 12-character minimum, plus
# product/brand words. Not a replacement for a breach-corpus check, but it stops the obvious ones offline.
_COMMON = frozenset({
    "password1234", "password12345", "123456789012", "1234567890123", "qwertyuiop12", "qwertyuiopas",
    "qwerty123456", "qwertz123456", "iloveyou1234", "administrator", "welcome12345", "letmein12345",
    "changeme1234", "change-me-too", "change-me-too-please", "changemetoo!", "passw0rd1234", "p@ssw0rd1234",
    "abcdefghijkl", "abcd12345678", "111111111111", "000000000000", "aaaaaaaaaaaa", "1q2w3e4r5t6y",
    "1qaz2wsx3edc", "zaq12wsxcde3", "parkpilot123", "parkpilot1234", "luxuryparking", "luxuryparking1",
    "flughafen1234", "duesseldorf12", "dusseldorf12", "willkommen123", "passwort1234", "hallo1234567",
    "correct-horse", "correcthorse1", "test-password", "testtesttest", "secret123456", "default12345",
})
_SEQUENCES = ("abcdefghijklmnopqrstuvwxyz", "qwertyuiopasdfghjklzxcvbnm", "0123456789", "9876543210")


def _is_sequential(value: str) -> bool:
    lowered = value.lower()
    return any(lowered in seq or lowered[::-1] in seq for seq in _SEQUENCES)


def validate_password(password: str, *, email: str | None = None, name: str | None = None) -> list[str]:
    """Return a list of human-readable problems (empty list = acceptable)."""
    problems: list[str] = []
    if len(password) < MIN_LENGTH:
        problems.append(f"Password must contain at least {MIN_LENGTH} characters.")
    if len(password) > MAX_LENGTH:
        problems.append(f"Password must not be longer than {MAX_LENGTH} characters.")
    lowered = password.lower().strip()
    if lowered in _COMMON or lowered.replace(" ", "") in _COMMON:
        problems.append("This password is too common. Choose something harder to guess.")
    if len(set(lowered)) <= 3 and len(lowered) >= 1:
        problems.append("Password uses too few different characters.")
    if len(password) >= 6 and _is_sequential(lowered):
        problems.append("Password must not be a simple keyboard or number sequence.")
    if email:
        local = email.split("@", 1)[0].lower()
        if len(local) >= 4 and local in lowered:
            problems.append("Password must not contain the email address.")
    if name:
        for part in name.lower().split():
            if len(part) >= 4 and part in lowered:
                problems.append("Password must not contain the person's name.")
                break
    return problems


def password_validator(email_field: str = "email", name_field: str = "name"):
    """WTForms validator that applies `validate_password`, reading email/name from sibling fields."""
    from wtforms.validators import ValidationError

    def _check(form, field):
        if not field.data:
            return
        email = getattr(form, email_field, None)
        name = getattr(form, name_field, None)
        problems = validate_password(
            field.data, email=email.data if email is not None else None, name=name.data if name is not None else None)
        if problems:
            raise ValidationError(problems[0])

    return _check
