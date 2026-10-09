#!/usr/bin/env python3
"""Capture the exact HTML each store serves from the runner IP and upload it
as a workflow artifact - ground truth for parser development."""
import os
import sys
import time

import requests

OUT = "/tmp/storepages"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HDRS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Connection": "close",
}

PAGES = [
    ("croma", "https://www.croma.com/televisions/c/1001"),
    ("jiomart", "https://www.jiomart.com/catalogsearch/result/?q=deals"),
    ("firstcry", "https://www.firstcry.com/kids-footwear"),
    ("myntra", "https://www.myntra.com/men-tshirts"),
    ("tatacliq", "https://www.tatacliq.com/mens-t-shirts/c-msh1207002"),
]


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    s = requests.Session()
    s.headers.update(HDRS)
    for name, url in PAGES:
        try:
            r = s.get(url, timeout=20)
            path = os.path.join(OUT, f"{name}.html")
            with open(path, "w", encoding="utf-8", errors="replace") as f:
                f.write(r.text or "")
            with open(os.path.join(OUT, f"{name}.meta"), "w") as f:
                f.write(f"status={r.status_code} bytes={len(r.content)} final={r.url}\n")
            print(f"[capture] {name:10} {r.status_code} {len(r.content):>8} bytes")
        except Exception as exc:  # noqa: BLE001
            print(f"[capture] {name:10} ERR {type(exc).__name__}: {str(exc)[:80]}")
        time.sleep(2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
