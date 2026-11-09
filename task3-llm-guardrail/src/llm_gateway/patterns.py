"""PII patterns and full-text redaction.

All patterns use bounded repetition ({m,n}), so no single match can exceed a
few hundred characters. That bound is what lets the streaming redactor hold
back only a small, fixed-size tail instead of buffering the whole response.
"""

import regex

REDACTED = "[REDACTED]"

PII_PATTERN = regex.compile(
    r"""
      (?P<email> [A-Za-z0-9._%+-]{1,64} @ [A-Za-z0-9.-]{1,253} \. [A-Za-z]{2,24} )
    | (?P<ssn>   \b \d{3}-\d{2}-\d{4} \b )
    | (?P<card>  \b (?:\d[\ -]?){12,18} \d \b )
    """,
    regex.VERBOSE,
)


def passes_luhn(number: str) -> bool:
    """Luhn checksum used by all major card networks; filters out order IDs, timestamps, etc."""
    digits = [int(char) for char in number if char.isdigit()]
    checksum = 0
    for position, digit in enumerate(reversed(digits)):
        if position % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def is_sensitive(match: regex.Match) -> bool:
    if match.lastgroup == "card":
        return passes_luhn(match.group())
    return True


def redact_text(text: str) -> str:
    """Redact a complete string (used for non-streaming responses)."""
    return PII_PATTERN.sub(lambda m: REDACTED if is_sensitive(m) else m.group(), text)
