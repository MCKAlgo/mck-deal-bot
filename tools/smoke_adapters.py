#!/usr/bin/env python3
"""Smoke-test the REAL retailer adapters from the runner's IP.

Runs each adapter's actual fetch_products() with a tiny budget and prints
yield + samples - this is exactly what the dealbot sees each cycle.
Diagnostic only: no Telegram, no DB writes.
"""
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrapers.amazon import AmazonAdapter          # noqa: E402
from scrapers.ajio import AjioAdapter              # noqa: E402
from scrapers.croma import CromaAdapter            # noqa: E402
from scrapers.firstcry import FirstCryAdapter      # noqa: E402
from scrapers.jiomart import JioMartAdapter        # noqa: E402
from scrapers.meesho import MeeshoAdapter          # noqa: E402
from scrapers.myntra import MyntraAdapter          # noqa: E402
from scrapers.nykaa import NykaaAdapter            # noqa: E402
from scrapers.tatacliq import TataCliqAdapter      # noqa: E402

CASES = [
    ("croma", CromaAdapter, 2),
    ("jiomart", JioMartAdapter, 2),
    ("firstcry", FirstCryAdapter, 2),
    ("myntra", MyntraAdapter, 1),
    ("tatacliq", TataCliqAdapter, 1),
    ("amazon", AmazonAdapter, 1),
    ("meesho", MeeshoAdapter, 1),
    ("nykaa", NykaaAdapter, 1),
    ("ajio", AjioAdapter, 1),
]


def run(name, cls, budget):
    t0 = time.time()
    try:
        ad = cls()
        products = ad.fetch_products(offset=0, budget=budget) or []
        imgs = sum(1 for p in products if p.get("image_url"))
        sample = ""
        if products:
            s = products[0]
            sample = (f"{s['title'][:44]!r} Rs{s['current_price']:.0f} "
                      f"({s.get('discount_pct', 0):.0f}% off)")
        print(f"[smoke] {name:10} {len(products):>3} products, {imgs} with image "
              f"({time.time()-t0:.0f}s)  {sample}")
    except Exception as exc:  # noqa: BLE001
        print(f"[smoke] {name:10} CRASH: {type(exc).__name__}: {str(exc)[:100]}")
        traceback.print_exc(limit=2)


if __name__ == "__main__":
    for name, cls, budget in CASES:
        run(name, cls, budget)
        time.sleep(1)
