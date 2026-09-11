import regex


_EMOJI_PATTERN = regex.compile(
    r"[0-9#*][\uFE0E\uFE0F]?\u20E3"
    r"|[\p{Emoji}--[0-9#*]]"
    r"|[\u200D\uFE0E\uFE0F\u20E3\U000E0020-\U000E007F]",
    regex.VERSION1,
)
_PENDING_KEYCAP = regex.compile(r"[0-9#*][\uFE0E\uFE0F]?$")


class EmojiStreamFilter:
    """Strip emoji before streaming, retaining possible split keycap sequences."""

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, text: str, *, final: bool = False) -> str:
        text = self._pending + text
        self._pending = ""
        match = None if final else _PENDING_KEYCAP.search(text)
        if match is not None:
            self._pending = match.group()
            text = text[:match.start()]
        return _EMOJI_PATTERN.sub("", text)
