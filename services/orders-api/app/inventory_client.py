"""
HTTP client that orders-api uses to talk to inventory-svc.

It turns inventory-svc's HTTP responses into Python exceptions, so the order logic
in main.py reads naturally: `try: reserve(...) except OutOfStock: ...`
"""
import httpx


class OutOfStock(Exception):
    """inventory-svc answered 409: the product exists but there isn't enough stock."""


class UnknownSku(Exception):
    """inventory-svc answered 404: no product with this SKU."""


class InventoryUnavailable(Exception):
    """inventory-svc couldn't be reached, timed out, or returned an unexpected error."""


class InventoryClient:
    def __init__(self, base_url: str, timeout: float = 3.0):
        self.base_url = base_url.rstrip("/")
        # Always set a timeout on network calls. Without one, a hung inventory-svc
        # would make every order request hang too.
        self.timeout = timeout

    def reserve(self, sku: str, quantity: int) -> None:
        """Reserve stock. Returns normally on success; raises one of the exceptions above otherwise."""
        try:
            resp = httpx.post(
                f"{self.base_url}/reserve",
                json={"sku": sku, "quantity": quantity},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:  # connection refused, DNS failure, timeout, ...
            raise InventoryUnavailable(str(exc)) from exc

        if resp.status_code == 404:
            raise UnknownSku(sku)
        if resp.status_code == 409:
            raise OutOfStock(sku)
        if resp.status_code != 200:
            raise InventoryUnavailable(f"inventory-svc returned {resp.status_code}")
