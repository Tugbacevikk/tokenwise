import pytest
from cart import Cart


def test_discount_reduces_total(catalog):
    cart = Cart(catalog)
    cart.add("tea", 2)
    cart.apply_discount(10)
    assert cart.total() == 2250


def test_discount_must_be_a_percentage(catalog):
    cart = Cart(catalog)
    with pytest.raises(ValueError):
        cart.apply_discount(150)
