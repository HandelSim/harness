"""Text helpers."""


def slugify(text):
    """Turn text into a URL slug.

    Rules:
    - lowercase everything
    - any run of characters that are not ASCII letters or digits becomes a single "-"
    - no leading or trailing "-"
    - an input with no letters or digits returns "n-a"

    >>> slugify("Hello, World!")
    'hello-world'
    >>> slugify("  Release v2.0 -- notes ")
    'release-v2-0-notes'
    """
    raise NotImplementedError
