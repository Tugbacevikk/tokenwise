from catalog import Catalog


class Cart:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self.items: dict[str, int] = {}

    def add(self, sku: str, quantity: int = 1) -> None:
        if quantity <= 0:
            raise ValueError("Quantiy must be positive")
        self.catalog.get(sku)
        self.items[sku] = self.items.get(sku, 0) + quantity

    def remove(self, sku: str) -> None:
        del self.items[sku]

    def total(self) -> int:
        """Total price in cents."""
        return sum(self.catalog.get(sku).price * qty for sku, qty in self.items.items())

    def count(self) -> int:
        return sum(self.items.values())
