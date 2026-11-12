from collections import Counter

from orders import OrderBook


def best_sellers(book: OrderBook, limit: int = 3) -> list[tuple[str, int]]:
    counts: Counter[str] = Counter()
    for order in book.by_status("new"):
        counts.update(order.lines)
    return counts.most_common(limit)


def revenue(book: OrderBook) -> int:
    return sum(o.total for o in book.by_status("new"))
