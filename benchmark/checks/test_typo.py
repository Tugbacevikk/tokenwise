import pytest
from cart import Cart


def test_message_is_spelled_right(catalog):
    with pytest.raises(ValueError, match="^Quantity must be positive$"):
        Cart(catalog).add("tea", 0)
