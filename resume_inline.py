"""The resume's deliberately small inline format: paired bold spans only."""

import re


def fragments(value):
    value = str(value or "")
    if value.count("**") % 2:
        raise ValueError("Resume emphasis must use paired bold delimiters")
    bold = False
    for part in re.split(r"(\*\*)", value):
        if part == "**":
            bold = not bold
        elif part:
            yield part, bold


def plain_text(value):
    return "".join(text for text, _ in fragments(value))


def normalized_text(value):
    value = plain_text(value).replace("\u00ad", "")
    for char in "\u2010\u2011\u2012\u2013\u2014\u2212":
        value = value.replace(char, "-")
    return " ".join(value.split())
