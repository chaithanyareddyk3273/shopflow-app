"""HTTP client for inventory-svc (synchronous service-to-service call)."""
import httpx


class OutOfStock(Exception):
    pass


class UnknownSku(Exception):
    pass


class InventoryUnavailable(Exception):
    pass


class InventoryClient:
    def __init__(self, base_url: str, timeout: float = 3.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def reserve(self, sku: str, quantity: int) -> None:
        try:
            resp = httpx.post(
                f"{self.base_url}/reserve",
                json={"sku": sku, "quantity": quantity},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise InventoryUnavailable(str(exc)) from exc

        if resp.status_code == 404:
            raise UnknownSku(sku)
        if resp.status_code == 409:
            raise OutOfStock(sku)
        if resp.status_code != 200:
            raise InventoryUnavailable(f"inventory-svc returned {resp.status_code}")
