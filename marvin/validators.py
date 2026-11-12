"""
What each kind of free text may contain, shared by every form.

Output is escaped and queries are parameterised, so these are not the only
defence against injection. They keep stored data to what each field is for,
so it stays safe wherever it ends up later: an email, a CSV opened in a
spreadsheet, a payee name sent to Wise.
"""

import re

from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator

# Control characters other than tab and line breaks, which long text may use.
_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
_LINE_BREAK = re.compile(r'[\t\n\r]')
# A spreadsheet runs a cell that starts with one of these as a formula.
_FORMULA_START = ('=', '+', '-', '@')
# Letters from any alphabet, joined by single spaces, apostrophes, hyphens or
# dots, as in "José", "Mary-Jane O'Neil", "J. R. R. Tolkien" or "李 小龍".
_NAME = re.compile(r"[^\W\d_]+(?:[ '’.\-]\s?[^\W\d_]+)*\.?")
_PHONE = re.compile(r'\+?\(?[0-9][0-9 ().\-]{5,29}')
_POSTCODE = re.compile(r'[A-Za-z0-9][A-Za-z0-9 \-]{1,11}')
_BANK_VALUE = re.compile(r'[A-Za-z0-9][A-Za-z0-9 .\-/]{0,39}')


def multi_line(value):
    """Long text: any characters people type, but no hidden control codes."""
    if _CONTROL.search(value):
        raise ValidationError('Remove the hidden control characters from this text.')


def single_line(value):
    """A short value that belongs on one line, with no markup."""
    multi_line(value)
    if _LINE_BREAK.search(value):
        raise ValidationError('Keep this on a single line.')
    if '<' in value or '>' in value:
        raise ValidationError('Remove the < and > characters.')
    if value.startswith(_FORMULA_START):
        raise ValidationError('This cannot start with =, +, - or @.')


def person_name(value):
    """A name in any alphabet, without digits or symbols."""
    single_line(value)
    if not _NAME.fullmatch(value.strip()):
        raise ValidationError('Use letters only, with spaces, hyphens or apostrophes between them.')


def phone_number(value):
    if not _PHONE.fullmatch(value.strip()):
        raise ValidationError('Enter a phone number using digits, with an optional + at the start.')


def postcode(value):
    if not _POSTCODE.fullmatch(value.strip()):
        raise ValidationError('Enter a postcode using letters, digits, spaces or hyphens.')


def bank_value(value):
    if not _BANK_VALUE.fullmatch(value.strip()):
        raise ValidationError('Use letters, digits, spaces, dots or hyphens only.')


def restrict(form_class, **rules):
    """
    Attach validators, and optionally a length limit, to fields of a form class.

    Each rule is (validators, max_length). Changing base_fields once here
    applies to every instance of the form, and to formsets built from it.
    """
    for name, (validators, max_length) in rules.items():
        field = form_class.base_fields[name]
        field.validators = [*field.validators, *validators]
        if max_length:
            field.max_length = max_length
            field.validators.append(MaxLengthValidator(max_length))
            field.widget.attrs['maxlength'] = str(max_length)


# The rules, named for the kind of value rather than the field holding it.
NAME = ([person_name], 150)
LINE = ([single_line], None)
# The pattern alone limits it to digits and separators, and a leading + is
# how international numbers are written, so the line rules do not apply.
PHONE = ([phone_number], 30)
POSTCODE = ([single_line, postcode], 12)
BANK = ([single_line, bank_value], 40)


def line(max_length):
    return ([single_line], max_length)


def text(max_length):
    return ([multi_line], max_length)
