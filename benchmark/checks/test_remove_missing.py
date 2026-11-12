import pytest
from cart import Cart


def test_remove_missing_item_says_what_is_missing(catalog):
    with pytest.raises(KeyError, match="not in cart: cup"):
        Cart(catalog).remove("cup")


def test_remove_still_works(catalog):
    cart = Cart(catalog)
    cart.add("tea")
    cart.remove("tea")
    assert cart.count() == 0
