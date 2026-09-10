import unittest

from services.emoji_filter import EmojiStreamFilter


class EmojiStreamFilterTest(unittest.TestCase):
    def test_emoji_removal_is_independent_of_chunk_boundaries(self):
        text = "正文😀👍🏽👨‍👩‍👧‍👦🇨🇳🏴\U000E0067\U000E0062\U000E007F❤️☀️✅1️⃣#️⃣*⃣结束"
        for chunks in ([text], list(text), *([text[:i], text[i:]] for i in range(len(text)))):
            with self.subTest(chunks=chunks):
                stream = EmojiStreamFilter()
                result = "".join(stream.feed(chunk) for chunk in chunks)
                self.assertEqual(result + stream.feed("", final=True), "正文结束")

    def test_preserves_text_markdown_numbers_and_citations(self):
        text = "# 水温\n**测量** 23.5℃，范围±2；[[cite:1]]\n编号123 # *"
        stream = EmojiStreamFilter()
        result = "".join(stream.feed(char) for char in text)
        self.assertEqual(result + stream.feed("", final=True), text)

    def test_empty_chunks_and_trailing_digit(self):
        stream = EmojiStreamFilter()
        self.assertEqual(stream.feed("共3"), "共")
        self.assertEqual(stream.feed(""), "")
        self.assertEqual(stream.feed("", final=True), "3")
        self.assertEqual(stream.feed("", final=True), "")
