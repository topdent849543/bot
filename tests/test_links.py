import pytest

from bot.handlers import normalize_telegram_channel_url


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://t.me/my_channel", "https://t.me/my_channel"),
        ("t.me/my_channel", "https://t.me/my_channel"),
        ("www.t.me/my_channel", "https://t.me/my_channel"),
        ("telegram.me/my_channel", "https://t.me/my_channel"),
        ("@my_channel", "https://t.me/my_channel"),
        ("https://t.me/+AbCdEf123", "https://t.me/+AbCdEf123"),
        ("https://t.me/joinchat/AbCdEf123", "https://t.me/joinchat/AbCdEf123"),
        ("https://t.me/my_channel?start=abc", "https://t.me/my_channel?start=abc"),
    ],
)
def test_telegram_channel_links_are_normalized(given, expected):
    assert normalize_telegram_channel_url(given) == expected


@pytest.mark.parametrize("given", ["", "@ab", "https://example.com/channel", "t.me", "www.google.com"])
def test_non_telegram_or_invalid_links_are_rejected(given):
    assert normalize_telegram_channel_url(given) is None
