"""User-facing texts are Russian for now and pass through `_()`, so English can be added later by extracting them
(Babel) instead of touching every template. Until then `_()` returns the text as is."""


def _(text: str) -> str:
    return text
