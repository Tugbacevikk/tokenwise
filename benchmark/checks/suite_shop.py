"""The shop's own behaviour, which every task must leave working (the regression suite).

Named suite_*.py, not test_*.py, so a task's check never collects it by accident."""

import pytest
from cart import Cart
from catalog import Product
from orders import OrderBook
from report import best_sellers, revenue
from users import UserStore


def test_cart_adds_counts_and_totals(catalog):
    cart = Cart(catalog)
    cart.add("tea", 2)
    cart.add("cup")
    cart.add("tea")
    assert cart.count() == 4 and cart.total() == 3 * 1250 + 400
    with pytest.raises(KeyError):
        cart.add("nothing")


def test_catalog_search_and_restock(catalog):
    assert [p.sku for p in catalog.search("TEA")] == ["tea", "cup"]
    assert catalog.restock("cup", 3).stock == 8
    catalog.add(Product("pot", "Tea pot", 900))
    assert catalog.get("pot").price == 900


def test_orders_and_report(catalog):
    book = OrderBook()
    cart = Cart(catalog)
    cart.add("tea", 2)
    first = book.place(cart)
    assert first.id == 1 and first.total == 2500 and cart.count() == 0
    cart.add("cup")
    book.place(cart)
    book.cancel(2)
    assert revenue(book) == 2500 and best_sellers(book) == [("tea", 2)]
    with pytest.raises(ValueError):
        book.place(cart)


def test_users_register_and_log_in():
    store = UserStore()
    store.register(" Ayse@Example.com ", "s3cret")
    assert store.login("ayse@example.com", "s3cret") and not store.login("ayse@example.com", "x")
    with pytest.raises(ValueError):
        store.register("AYSE@example.com", "other")
