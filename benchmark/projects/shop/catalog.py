from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    price: int  # cents
    stock: int = 0


class Catalog:
    def __init__(self, products: list[Product] | None = None):
        self._products = {p.sku: p for p in products or []}

    def get(self, sku: str) -> Product:
        try:
            return self._products[sku]
        except KeyError:
            raise KeyError(f"unknown product: {sku}") from None

    def add(self, product: Product) -> None:
        self._products[product.sku] = product

    def search(self, text: str) -> list[Product]:
        text = text.lower()
        return [p for p in self._products.values() if text in p.name.lower()]

    def restock(self, sku: str, amount: int) -> Product:
        product = self.get(sku)
        updated = Product(product.sku, product.name, product.price, product.stock + amount)
        self._products[sku] = updated
        return updated
