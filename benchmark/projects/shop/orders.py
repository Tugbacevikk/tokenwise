from dataclasses import dataclass, field
from datetime import datetime

from cart import Cart


@dataclass
class Order:
    id: int
    lines: dict[str, int]
    total: int
    created_at: datetime = field(default_factory=datetime.now)
    status: str = "new"


class OrderBook:
    def __init__(self):
        self._orders: list[Order] = []

    def place(self, cart: Cart) -> Order:
        if not cart.items:
            raise ValueError("cart is empty")
        order = Order(len(self._orders) + 1, dict(cart.items), cart.total())
        self._orders.append(order)
        cart.items.clear()
        return order

    def cancel(self, order_id: int) -> None:
        for order in self._orders:
            if order.id == order_id:
                order.status = "cancelled"
                return
        raise KeyError(order_id)

    def by_status(self, status: str) -> list[Order]:
        return [o for o in self._orders if o.status == status]
