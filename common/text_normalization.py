import unicodedata


def normalize_text(value: str) -> str:
    """Shared dictionary and query normalization (not semantic classification)."""
    return unicodedata.normalize("NFKC", value).casefold().strip()
