import pytest
from catalog import Catalog, Product


@pytest.fixture
def catalog():
    return Catalog([Product("tea", "Rize tea", 1250, 10), Product("cup", "Tea cup", 400, 5)])
