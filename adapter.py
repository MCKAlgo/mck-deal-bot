"""MCK Engine - formal retailer adapter interface.

Every retailer plugs into the engine through this one contract. Adding a new
retailer means implementing this class - nothing else in the engine changes.

Interface (per design spec):
    discover_categories()   -> catalogue discovery (subcategories of this retailer)
    fetch_products(...)     -> batch sweep of listings (deal discovery layer)
    get_product(url)        -> full single-product refresh
    get_price(url)          -> current price only
    get_offers(url)         -> bank offers / coupons (best-effort, may be [])
    get_availability(url)   -> in-stock status (best-effort, may be None)

Adapters that cannot parse a field return None/[] gracefully - the engine
treats missing data with neutral scoring rather than failure.
"""
import logging

from scraper_base import BaseScraper

log = logging.getLogger("adapter")


class RetailerAdapter(BaseScraper):
    """Base class all retailer adapters inherit from."""
    platform = "base"

    # -------------------------------------------------- catalogue discovery
    def discover_categories(self, html: str = "", seed_url: str = "") -> list:
        """Extract deeper subcategory candidates from a listing page HTML.
        Returns list of dicts: {url, name, depth}. Engine dedupes and stores
        them in the categories table. Default: no discovery (JS-only sites)."""
        return []

    def drain_discovered(self) -> list:
        """Pop and return categories this adapter collected during fetches."""
        found = getattr(self, "_discovered", [])
        self._discovered = []
        return found

    def _record_discovered(self, url: str, name: str = "", depth: int = 0):
        if not getattr(self, "_discovered", None):
            self._discovered = []
        self._discovered.append({"url": url, "name": name, "depth": depth})

    # -------------------------------------------------- deal discovery
    def fetch_products(self, offset: int = 0, budget: int = 5,
                       cats: list = None) -> list:
        """Sweep a rotating slice of category/search listings.
        cats: optional list of {url, name} picked by the engine from the
        discovered category universe. Adapters fall back to their seed list."""
        raise NotImplementedError

    # -------------------------------------------------- single product
    def get_product(self, url: str):
        """Full product refresh: {price, mrp, rating, reviews_count,
        image_url, title, availability?} or None if unavailable."""
        return self.get_product_update(url)

    def get_price(self, url: str):
        upd = self.get_product(url)
        return upd.get("price") if upd else None

    def get_offers(self, url: str):
        """Bank offers/coupons if the page exposes them (best-effort)."""
        return []

    def get_availability(self, url: str):
        upd = self.get_product(url)
        return upd.get("availability") if upd else None

    # adapters that support single-product rechecks implement this
    def get_product_update(self, url: str):
        return None
