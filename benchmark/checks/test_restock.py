import pytest


def test_restock_rejects_non_positive_amounts(catalog):
    with pytest.raises(ValueError):
        catalog.restock("tea", -3)
    with pytest.raises(ValueError):
        catalog.restock("tea", 0)
    assert catalog.restock("tea", 2).stock == 12
