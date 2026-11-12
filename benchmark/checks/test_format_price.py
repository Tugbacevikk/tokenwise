from money import format_price


def test_format_price():
    assert format_price(1250) == "12.50 TL"
    assert format_price(5) == "0.05 TL"
    assert format_price(123456) == "1,234.56 TL"
