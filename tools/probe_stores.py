#!/usr/bin/env python3
"""One-shot store reachability probe - meant to run ON a GitHub Actions runner
so we test from the same datacenter IP the dealbot uses. One request per URL,
prints status + body fingerprint so we can tell WAF blocks apart from real pages."""
import sys
import time

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HDRS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "close",
}

PROBES = [
    ("amazon-home", "https://www.amazon.in/"),
    ("amazon-search", "https://www.amazon.in/s?k=bluetooth+headphones"),
    ("meesho-page", "https://www.meesho.com/mens-tshirts/pl/3ryy1"),
    ("myntra-page", "https://www.myntra.com/men-tshirts"),
    ("nykaa-page", "https://www.nykaa.com/"),
    ("ajio-page", "https://www.ajio.com/men/c/8303"),
    ("tatacliq-page", "https://www.tatacliq.com/mens-t-shirts/c-msh1207002"),
    ("tatacliq-api", "https://www.tatacliq.com/marketplacewebservices/v2/mpl/"
                     "products/productsSearch?searchText=shoes&isFiltered=false&sort=rec"),
    ("croma-page", "https://www.croma.com/televisions/c/1003"),
    ("croma-api", "https://api.croma.com/searchservices/v1/s/search?q=tv&page=1"),
    ("jiomart-page", "https://www.jiomart.com/catalogsearch/result/?q=deals"),
    ("firstcry-page", "https://www.firstcry.com/toys-and-gaming"),
]

BLOCK_MARKERS = ("captcha", "challenge", "cloudflare", "access denied",
                 "blocked", "akamai", "unusual traffic", "robot")


def sniff(body: str) -> str:
    low = body[:4000].lower()
    for m in BLOCK_MARKERS:
        if m in low:
            return f"BLOCK-MARKER:{m}"
    if "<script" in low or "<html" in low:
        return "html"
    if body[:1] in "{[":
        return "json"
    return "other"


def main() -> int:
    s = requests.Session()
    s.headers.update(HDRS)
    print(f"{'probe':16} {'status':6} {'len':>8}  fingerprint")
    for name, url in PROBES:
        try:
            r = s.get(url, timeout=15, allow_redirects=True)
            text = r.text[:4000] if r.text else ""
            print(f"{name:16} {r.status_code:<6} {len(r.content):>8}  {sniff(text)}"
                  f"  | {r.url[:80]}")
        except Exception as exc:  # noqa: BLE001 - diagnostic must not die
            print(f"{name:16} ERR    {'-':>8}  {type(exc).__name__}: {exc}")
        time.sleep(2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
