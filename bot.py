"""DealBot - 24/7 price tracker for Flipkart, Amazon & Meesho.

Continuously scrapes products, records every price into SQLite history,
matches the same product across platforms, scores deals (all-time low,
60%+ off, cross-platform winner, big 7-day drops) and pushes genuine
best-deal alerts + a daily 9AM IST buy-list to Telegram chats.

Usage:
  python3 bot.py                 # run 24/7 loop
  python3 bot.py --once          # single check cycle (for testing)
  python3 bot.py --test-alert    # send a test message to registered chats
"""
import argparse
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from database import Database, now_ist
from matcher import build_groups, group_summary
from scorer import score_product
from formatter import (fmt_deal_alert, fmt_digest, fmt_status, fmt_welcome,
                       fmt_help, fmt_test, deal_buttons)
from telegram_client import TelegramBot

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
DB_PATH = BASE / "data" / "deals.db"
LOG_DIR = BASE / "logs"

log = logging.getLogger("bot")


# ---------------------------------------------------------------- setup
def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(LOG_DIR / "bot.log",
                                              maxBytes=5 * 1024 * 1024, backupCount=3)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def start_health_server():
    """Tiny HTTP health endpoint. PaaS hosts (Render/Railway/Runny/Fly...)
    often require an open port - set PORT env var and they can health-check us."""
    port = int(os.environ.get("PORT", "0") or 0)
    if port <= 0:
        return
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"DealBot is running 24/7")

        def log_message(self, *args):
            pass

    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log.info("Health server listening on port %s", port)
    except OSError as e:
        log.warning("Health server not started: %s", str(e)[:80])


# ---------------------------------------------------------------- core
class DealBot:
    def __init__(self):
        self.config = load_config()
        self.config_mtime = CONFIG_PATH.stat().st_mtime
        self.db = Database(str(DB_PATH))
        self.tg = None
        self.tg_token = ""
        self.scrapers = {}
        self.started_at = now_ist()
        self._init_scrapers()
        self._seed_categories()
        self._ensure_telegram()

    # -------- scrapers (retailer adapters) --------
    def _init_scrapers(self):
        from scrapers.flipkart import FlipkartScraper
        from scrapers.amazon import AmazonScraper
        from scrapers.meesho import MeeshoScraper
        self.scrapers = {
            "flipkart": FlipkartScraper(),
            "amazon": AmazonScraper(),
            "meesho": MeeshoScraper(),
        }
        # optional adapters: auto-activate on clean IPs, silent no-op when blocked
        for module, cls in (("croma", "CromaAdapter"),
                            ("jiomart", "JioMartAdapter"),
                            ("nykaa", "NykaaAdapter"),
                            ("ajio", "AjioAdapter"),
                            ("tatacliq", "TataCliqAdapter"),
                            ("myntra", "MyntraAdapter"),
                            ("firstcry", "FirstCryAdapter")):
            try:
                mod = __import__(f"scrapers.{module}", fromlist=[cls])
                self.scrapers[module] = getattr(mod, cls)()
            except ImportError:
                pass

    def _seed_categories(self):
        """Seed the catalogue universe with each adapter's root categories.
        Discovery grows the tree from here automatically every cycle."""
        for name, scraper in self.scrapers.items():
            if not self.config.get("platforms", {}).get(name):
                continue
            seeds = getattr(scraper, "SEED_CATEGORIES", None)
            if not seeds:
                continue
            existing = self.db.category_count(name)
            if existing >= len(seeds):
                continue  # universe already seeded
            added = 0
            for url in seeds:
                if self.db.add_category(name, url, name="seed", depth=0):
                    added += 1
            if added:
                log.info("[catalogue] seeded %d root categories for %s "
                         "(universe: %d)", added, name,
                         self.db.category_count(name))

    # -------- telegram --------
    def _ensure_telegram(self):
        # env var wins (PaaS-friendly), config.json is the default source
        token = (os.environ.get("TELEGRAM_BOT_TOKEN")
                 or self.config.get("bot_token") or "").strip()
        if not token:
            return
        if token == self.tg_token and self.tg is not None:
            return
        tg = TelegramBot(token, on_command=self.handle_command)
        me = tg.get_me()
        if me.get("ok"):
            self.tg = tg
            self.tg_token = token
            tg.start_polling()
            botname = me["result"].get("username", "DealBot")
            log.info("Telegram connected as @%s", botname)
            # startup ping only once per 6h so restarts (e.g. GH Actions runs)
            # never spam the chats
            try:
                last = float(self.db.get_meta("last_start_broadcast", "0") or 0)
            except (TypeError, ValueError):
                last = 0.0
            if time.time() - last > 6 * 3600:
                self.tg.broadcast(self.db.get_chats(), fmt_test())
                self.db.set_meta("last_start_broadcast", str(time.time()))
        else:
            log.error("Telegram token invalid - bot will keep retrying each cycle")

    # -------- config hot reload --------
    def maybe_reload_config(self):
        try:
            mtime = CONFIG_PATH.stat().st_mtime
            if mtime != self.config_mtime:
                self.config_mtime = mtime
                self.config = load_config()
                log.info("Config reloaded (hot)")
                self._ensure_telegram()
        except OSError:
            pass

    def muted(self) -> bool:
        until = self.db.get_meta("mute_until", "0")
        try:
            if until and until != "0" and now_ist() < datetime.fromisoformat(until):
                return True
        except ValueError:
            pass
        return False

    # ------------------------------------------------ check cycle
    def run_cycle(self, platform_override: str = ""):
        self.maybe_reload_config()
        self.db.bump_stat("checks")
        # FULL SWEEP: every enabled platform is checked EVERY cycle - not one
        # or two stores, all of them, every time. Two safety valves keep each
        # cycle inside its wall-clock budget:
        #   1. per-cycle time budget (cycle_time_budget_seconds, default 300s)
        #      - platforms beyond it are deferred to the next cycle
        #   2. per-platform circuit breaker - a hard-blocked site (2x 403)
        #      is skipped for the rest of the cycle instead of wasting time
        # Within each platform the sweep still rotates through the catalogue
        # universe (rr_off_<name> cursor), so full coverage compounds.
        enabled = [k for k, v in self.config.get("platforms", {}).items() if v]
        if platform_override:
            enabled = [platform_override] if platform_override in self.scrapers else enabled
        sweep_t0 = time.time()
        budget_s = int(self.config.get("cycle_time_budget_seconds", 300))
        all_products = []
        done, deferred = [], []
        for name in enabled:
            if time.time() - sweep_t0 > budget_s and not platform_override:
                deferred.append(name)
                continue
            try:
                offset = int(self.db.get_meta(f"rr_off_{name}", "0") or 0)
            except (TypeError, ValueError):
                offset = 0
            self.db.set_meta(f"rr_off_{name}", str(offset + 1))
            budget = int(self.config.get("cycle_budget", {}).get(name, 5))
            scraper = self.scrapers.get(name)
            if not scraper:
                continue
            try:
                # catalogue universe: rotating slice of DISCOVERED categories
                cats = []
                max_cats = int(self.config.get("max_categories", 400))
                if self.db.category_count(name) > 0 and offset > 2:
                    cats = self.db.pick_categories(name, offset, budget)
                prods = scraper.fetch_products(offset=offset, budget=budget,
                                               cats=cats)
                for c in cats:
                    self.db.mark_category_crawled(c["id"], 0)
                all_products.extend(prods)
                # catalogue discovery: adapters collect deeper subcategories
                # from the HTML they already fetched (zero extra requests)
                discovered = scraper.drain_discovered()
                new = 0
                for d in discovered:
                    if self.db.category_count(name) >= max_cats:
                        break
                    if self.db.add_category(name, d["url"], d.get("name", ""),
                                            d.get("depth", 1)):
                        new += 1
                if new:
                    log.info("[catalogue] %s: +%d discovered subcategories "
                             "(universe: %d)", name, new,
                             self.db.category_count(name))
                log.info("[sweep] %s (offset %d, budget %d) -> %d products",
                         name, offset, budget, len(prods))
                done.append(name)
            except Exception as e:
                log.warning("[%s] cycle error: %s", name, str(e)[:150])
        if deferred:
            log.info("[sweep] time budget (%ds) reached - deferred to next cycle: %s",
                     budget_s, ", ".join(deferred))
        log.info("Cycle gathered %d raw products from %d/%d platforms in %ds",
                 len(all_products), len(done), len(enabled),
                 int(time.time() - sweep_t0))
        if not all_products:
            return
        if not all_products:
            return

        # store products + price history (product master enrichment)
        stored = []
        seen_pairs = set()
        from matcher import brand_of, model_tokens
        for p in all_products:
            if p["current_price"] < self.config.get("min_price", 149):
                continue
            if p["current_price"] > self.config.get("max_price", 200000):
                continue
            fp = self.fingerprint(p)
            p["fingerprint"] = fp
            # MRP sanity clamp: scrapers sometimes grab a wrong strike-price
            # node (a Rs.225 peeler showing MRP Rs.1,12,500). A broken MRP
            # poisons discount math and flags good products as fake. When the
            # strike price is >= 8x the selling price we drop the MRP and keep
            # the site-shown discount text instead.
            if p.get("mrp") and float(p["mrp"]) >= float(p["current_price"]) * 8:
                p["mrp"] = 0.0
            if p.get("discount_pct") and float(p["discount_pct"]) > 90:
                p["discount_pct"] = 0.0
            key = (p["platform"], fp)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            # product master fields (brand/model/subcategory auto-derived)
            p.setdefault("brand", brand_of(p["title"]))
            models = sorted(model_tokens(p["title"]))
            if models:
                p.setdefault("model", models[0])
            p.setdefault("subcategory", p.get("category", "general"))
            pid, _ = self.db.upsert_product(p)
            self.db.record_price(pid, p["current_price"])
            stored.append((pid, p))
        log.info("Stored %d unique products", len(stored))

        # cross-platform groups: built over ALL tracked products (accumulated
        # in DB), so Flipkart vs Amazon vs Meesho comparisons work across cycles
        tracked = self.db.all_products()
        tracked_by_fp = {p["fingerprint"]: p for p in tracked}
        groups = build_groups(list(tracked_by_fp.values()))
        group_of = {}
        for g in groups:
            info = group_summary(g)
            for p in g:
                fp = p.get("fingerprint")
                if fp:
                    group_of[fp] = info
        log.info("Built %d cross-platform comparison groups "
                 "(%d with 2+ platforms)", len(groups),
                 sum(1 for g in groups if len(g) >= 2))

        # score + alert (v2 gate: tiered by Deal Score + Data Confidence)
        rules = self.config.get("rules", {})
        gate = self.config.get("gate", {})
        cooldown = int(self.config.get("alert_cooldown_hours", 24))
        improve_pct = float(self.config.get("realert_improve_pct", 2))
        max_strong = int(self.config.get("max_alerts_per_cycle", 8))
        max_selective = int(self.config.get("max_selective_alerts", 2))
        strong_min = int(gate.get("strong_min", 70))
        selective_min = int(gate.get("selective_min", 60))
        candidates = []
        for pid, p in stored:
            stats = self.db.price_stats(pid)
            verdict = score_product(p, stats, group_of.get(p.get("fingerprint")),
                                    rules, gate)
            self.db.set_score(pid, verdict["score"], " | ".join(verdict["reasons"])[:400])
            if not (verdict["triggered"] and verdict["genuine"]):
                continue
            primary = verdict["triggered"][0]
            s = verdict["score"]
            if s < selective_min:
                continue  # below the gate entirely - keep tracking silently
            # cooldown dedup with material-improvement bypass
            if not self.db.alert_allowed(pid, primary, cooldown,
                                         current_price=p["current_price"],
                                         improve_pct=improve_pct):
                continue
            candidates.append({"pid": pid, "product": p, "verdict": verdict,
                               "stats": stats,
                               "group_info": group_of.get(p.get("fingerprint")),
                               "primary": primary,
                               "tier": "strong" if s >= strong_min else "selective"})

        strong = [c for c in candidates if c["tier"] == "strong"]
        selective = [c for c in candidates if c["tier"] == "selective"]
        strong.sort(key=lambda c: (-c["verdict"]["score"], -c["verdict"].get("confidence", 0)))
        selective.sort(key=lambda c: (-c["verdict"]["score"], -c["verdict"].get("confidence", 0)))
        # premium labels (EXCEPTIONAL/BEST) first; WATCH-tier extras only fill leftover space
        to_send = strong[:max_strong] + selective[:max_selective]

        # ---- BEST-OF-CYCLE guarantee (v4) -------------------------------
        # The strict gate needs history + cross-store data that a young
        # database does not have yet. That meant ZERO posts ever - not
        # acceptable. If the strict gate produced nothing this cycle, we send
        # the 2 strongest products we actually scraped right now: biggest
        # verified listing discount, sane MRP, decent rating. Each product
        # re-alerts only after its own long cooldown, so no spam.
        best_cycle_used = False
        if not to_send:
            boc_cfg = self.config.get("best_of_cycle", {})
            if boc_cfg.get("enabled", True):
                boc_count = int(boc_cfg.get("count", 2))
                boc_cooldown = int(boc_cfg.get("cooldown_hours", 168))
                boc_pool = []
                for pid, p in stored:
                    price = float(p["current_price"] or 0)
                    if price < float(self.config.get("min_price", 149)):
                        continue
                    disc = float(p.get("discount_pct") or 0)
                    mrp = float(p.get("mrp") or 0)
                    eff_disc = disc
                    if mrp > price * 1.2:
                        eff_disc = max(eff_disc, round((mrp - price) / mrp * 100, 1))
                    if eff_disc < 35:
                        continue
                    rating = p.get("rating")
                    if rating is not None and float(rating) < 3.2:
                        continue
                    if not self.db.alert_allowed(pid, "best_cycle", boc_cooldown):
                        continue
                    rank = eff_disc + (float(rating) * 3 if rating else 8.0) \
                         + min(10.0, (p.get("reviews_count") or 0) / 1000.0)
                    boc_pool.append((rank, pid, p, eff_disc))
                boc_pool.sort(key=lambda x: -x[0])
                for rank, pid, p, eff_disc in boc_pool[:boc_count]:
                    stats = self.db.price_stats(pid)
                    verdict = score_product(p, stats, group_of.get(p.get("fingerprint")),
                                            rules, gate)
                    verdict["deal_type"] = "GOOD" if eff_disc >= 50 else "WATCH"
                    verdict["verdict"] = "BUY" if eff_disc >= 50 else "WATCH"
                    if not any("top pick" in r.lower() for r in verdict["reasons"]):
                        verdict["reasons"].insert(
                            0, f"⭐ Top pick this cycle — {int(eff_disc)}% below listed MRP, "
                               f"engine verified price just now")
                    candidates.append({"pid": pid, "product": p, "verdict": verdict,
                                       "stats": stats,
                                       "group_info": group_of.get(p.get("fingerprint")),
                                       "primary": "best_cycle", "tier": "selective"})
                    to_send = candidates[:]
                    best_cycle_used = len(to_send) > 0

        if not to_send or self.muted() or not self.tg:
            if not self.tg and to_send:
                log.info("%d deals passed the gate but Telegram not configured yet", len(to_send))
            if self.muted() and to_send:
                log.info("%d deals found but alerts are muted", len(to_send))
            top3 = sorted(((c["verdict"]["score"], c["product"]["title"])
                           for c in candidates), reverse=True)[:3]
            if top3:
                log.info("No sends. Closest to gate this cycle: %s",
                         [(s, t[:34]) for s, t in top3])
            return

        chats = self.db.get_chats()
        if not chats:
            log.info("%d deals found but no chats registered yet (send /start)", len(to_send))
            return
        sent = 0
        photos = 0
        for c in to_send:
            msg = fmt_deal_alert(c["product"], c["verdict"], c["group_info"], c["stats"])
            buttons = deal_buttons(c["product"], c["group_info"])
            # IMAGE RESOLUTION CHAIN (v4.1): fresh scrape -> stored DB row
            # (earlier sweeps may have caught the image) -> normalize legacy
            # template URLs (Flipkart {@width}/{@height}) into fetchable ones.
            image = (c["product"].get("image_url") or "").strip()
            if not image.startswith("https://"):
                row = self.db.get_product(c["pid"])
                if row and row.get("image_url"):
                    image = (row["image_url"] or "").strip()
            from scraper_base import normalize_image_url
            image = normalize_image_url(image)
            if image:
                ok = self.tg.broadcast_photo(chats, image, msg, reply_markup=buttons)
                if ok:
                    photos += 1
                else:
                    ok = self.tg.broadcast(chats, msg, disable_preview=True,
                                           reply_markup=buttons)
            else:
                ok = self.tg.broadcast(chats, msg, disable_preview=True,
                                       reply_markup=buttons)
            if ok:
                self.db.log_alert(c["pid"], c["primary"], c["verdict"]["score"],
                                  c["product"]["current_price"])
                self.db.bump_stat("alerts_sent")
                sent += 1
                log.info("SENT [%s] score=%.1f %s -> %d chats",
                         c["primary"], c["verdict"]["score"],
                         c["product"]["title"][:48], len(chats))
            else:
                log.warning("SEND FAILED [%s] %s", c["primary"],
                            c["product"]["title"][:48])
            time.sleep(1.1)
        log.info("FUNNEL fetched=%d stored=%d triggered=%d gated=%d "
                 "best_cycle=%s SENT=%d (photos=%d)",
                 len(all_products), len(stored),
                 sum(1 for c in candidates if c["primary"] != "best_cycle"),
                 len(candidates) - (len(to_send) if best_cycle_used else 0),
                 "yes" if best_cycle_used else "no", sent, photos)

    # ------------------------------------------------ adaptive rechecks
    TIER_DELAYS = {  # seconds between rechecks per product tier
        "flash": 5 * 60,        # flash-sale candidate
        "dropped": 15 * 60,     # recently dropped
        "popular": 30 * 60,     # popular product
        "normal": 3 * 3600,     # normal product
        "stable": 12 * 3600,    # stable / low-interest
    }

    def _tier_delay(self, product: dict) -> int:
        """Adaptive frequency: spend checking power where deals happen."""
        stats = self.db.price_stats(product["id"])
        cnt = int(stats.get("cnt") or 0)
        price = product["current_price"] or 0
        if cnt < 3:
            return 45 * 60  # new products: build history fast
        ch = self.db.last_change_info(product["id"], price)
        if ch.get("recorded_at"):
            try:
                from datetime import datetime
                age_h = (now_ist() - datetime.fromisoformat(ch["recorded_at"])).total_seconds() / 3600
                prev = float(ch.get("price") or 0)
                if age_h <= 48 and prev > 0:
                    drop = (prev - price) / prev * 100 if prev > price else 0
                    if drop >= 8:
                        return self.TIER_DELAYS["flash"]
                    return self.TIER_DELAYS["dropped"]
            except (ValueError, TypeError):
                pass
        reviews = product.get("reviews_count") or 0
        if reviews >= 5000:
            return self.TIER_DELAYS["popular"]
        if ch.get("recorded_at"):
            try:
                from datetime import datetime
                age_d = (now_ist() - datetime.fromisoformat(ch["recorded_at"])).days
                if age_d >= 14:
                    return self.TIER_DELAYS["stable"]
            except (ValueError, TypeError):
                pass
        return self.TIER_DELAYS["normal"]

    def recheck_due_products(self, budget: int = 4):
        """Re-fetch individual product pages whose adaptive timer expired.
        This is what lets flash deals get checked every few minutes while
        boring products wait hours - coverage without wasted requests."""
        if budget <= 0:
            return
        due = self.db.due_products(limit=budget)
        for prod in due:
            scraper = self.scrapers.get(prod["platform"])
            if not scraper or not hasattr(scraper, "get_product_update"):
                continue
            upd = None
            try:
                upd = scraper.get_product_update(prod["url"])
            except Exception as e:
                log.debug("[%s] recheck error %s", prod["platform"], str(e)[:80])
            if upd and upd.get("price"):
                refreshed = dict(prod)
                refreshed["current_price"] = float(upd["price"])
                for k in ("mrp", "rating", "reviews_count", "image_url", "title"):
                    if upd.get(k):
                        refreshed[k] = upd[k]
                pid, _ = self.db.upsert_product(refreshed)
                self.db.record_price(pid, refreshed["current_price"])
                log.info("[recheck] %s -> ₹%s", prod["title"][:40],
                         f"{refreshed['current_price']:,.0f}")
            delay = self._tier_delay(prod)
            nxt = (now_ist() + timedelta(seconds=delay)).isoformat(timespec="seconds")
            self.db.set_next_check(prod["id"], nxt)
            time.sleep(0.8)

    # -------- fingerprint for dedup --------
    @staticmethod
    def fingerprint(p: dict) -> str:
        import re as _re
        from matcher import normalize, brand_of
        url = p.get("url") or ""
        m = _re.search(r"itm([a-z0-9]+)", url)
        if m:
            return f"itm{m.group(1)[:12]}"
        m = _re.search(r"/dp/([A-Z0-9]{10})", url)
        if m:
            return f"asin{m.group(1)}"
        t = normalize(p["title"])
        brand = brand_of(p["title"])
        toks = [w for w in t.split() if w != brand][:6]
        return brand + "-" + "-".join(sorted(toks))[:90]

    # ------------------------------------------------ daily digest
    def maybe_digest(self):
        cfg = self.config
        hour = int(cfg.get("digest_hour", 9))
        minute = int(cfg.get("digest_minute", 0))
        n = now_ist()
        if not (n.hour == hour and n.minute >= minute):
            return
        today = n.date().isoformat()
        if self.db.get_meta("last_digest_date") == today:
            return
        chats = self.db.get_chats()
        if not chats or not self.tg:
            return
        top = self.db.top_scored(10)
        if not top:
            return
        # rebuild verdict context lightly for display
        deals = []
        groups = build_groups([dict(t) for t in self.db.all_products()[:400]])
        group_of = {}
        for g in groups:
            info = group_summary(g)
            for p in g:
                group_of[p.get("fingerprint", p.get("id"))] = info
        for t in top:
            product = dict(t)
            verdict = {"score": t.get("last_score", 0),
                       "reasons": [r for r in (t.get("last_reasons") or "").split(" | ") if r],
                       "triggered": ["digest"], "genuine": True}
            deals.append({"product": product, "verdict": verdict,
                          "group_info": group_of.get(t.get("fingerprint", t.get("id")))})
        deals.sort(key=lambda d: d["verdict"]["score"], reverse=True)
        msg = fmt_digest(deals[:10], self.db.stats_summary())
        self.tg.broadcast(chats, msg, disable_preview=True)
        self.db.set_meta("last_digest_date", today)
        log.info("Daily digest sent")

    def maybe_weekly_report(self):
        """Monday morning: WEEKLY REPORT - week stats + top verified deals."""
        n = now_ist()
        if n.weekday() != 0 or n.hour != int(self.config.get("digest_hour", 9)):
            return
        if not self.config.get("weekly_report", True):
            return
        week = n.date().isocalendar()[1]
        key = f"last_weekly_report_{n.date().year}_w{week}"
        if self.db.get_meta(key):
            return
        chats = self.db.get_chats()
        if not chats or not self.tg:
            return
        stats = self.db.stats_summary()
        top = self.db.alerts_since(days=7, limit=5)
        lines = [
            "📊 <b>WEEKLY REPORT</b>",
            f"<i>Week {week} · {n.strftime('%d %b %Y')}</i>",
            "",
            f"🔍 Products tracked: <b>{stats.get('products', 0)}</b>",
            f"📦 Price points recorded: <b>{stats.get('price_points', 0)}</b>",
            f"🔥 Alerts delivered this week: <b>{self.db.alerts_count_since(7)}</b>",
            f"🏪 Retailers monitored: <b>{len(self.scrapers)}</b>",
        ]
        if top:
            lines += ["", "🏆 <b>TOP DEALS OF THE WEEK</b>"]
            for i, t in enumerate(top, 1):
                title = (t.get("title") or "")[:55]
                plat = (t.get("platform") or "").title()
                price = t.get("price") or 0
                score = t.get("score") or 0
                lines.append(f"{i}. {title}\n    {plat} · ₹{price:,.0f} · score {score:.0f}")
        lines += ["", "Full 24/7 monitoring continues. Next report: next Monday 📈"]
        self.tg.broadcast(chats, "\n".join(lines), disable_preview=True)
        self.db.set_meta(key, "1")
        log.info("Weekly report sent (%s)", key)

    # ------------------------------------------------ commands
    def handle_command(self, update: dict):
        msg = update.get("message") or update.get("edited_message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        chat_type = chat.get("type", "private")
        text = (msg.get("text") or "").strip()
        if not chat_id or not text.startswith("/"):
            return
        cmd = text.split()[0].split("@")[0].lower()
        log.info("Command /%s from chat %s (%s)", cmd.lstrip('/'), chat_id, chat_type)

        if cmd == "/start":
            self.db.add_chat(chat_id, chat_type, chat.get("title") or chat.get("first_name") or "")
            if self.tg:
                self.tg.send_message(chat_id, fmt_welcome(chat_type))
        elif cmd == "/id":
            if self.tg:
                self.tg.send_message(chat_id, f"🆔 Chat ID: <code>{chat_id}</code>\nType: {chat_type}")
        elif cmd == "/status":
            if self.tg:
                muted_until = self.db.get_meta("mute_until", "0")
                self.tg.send_message(chat_id, fmt_status(self.db.stats_summary(), muted_until))
        elif cmd == "/deals":
            if self.tg:
                top = self.db.top_scored(5)
                if not top:
                    self.tg.send_message(chat_id, "⏳ Still collecting deals... check again in a few minutes!")
                    return
                lines = ["🏆 <b>TOP DEALS RIGHT NOW</b>", ""]
                for i, t in enumerate(top, 1):
                    lines.append(f"{i}. <b>{t['title'][:55]}</b>")
                    lines.append(f"   💵 ₹{int(t['current_price']):,} · Score {t['last_score']:.0f} · "
                                 f"{t['platform'].title()}")
                    reason = (t.get("last_reasons") or "").split(" | ")
                    if reason and reason[0]:
                        lines.append(f"   ↳ {reason[0]}")
                    lines.append(f"   🛒 {t['url']}")
                    lines.append("")
                self.tg.send_message(chat_id, "\n".join(lines), disable_preview=True)
        elif cmd == "/mute":
            parts = text.split()
            hours = 8
            if len(parts) > 1:
                try:
                    hours = max(1, min(72, int(_digits(parts[1]))))
                except ValueError:
                    pass
            until = (now_ist() + timedelta(hours=hours)).isoformat(timespec="seconds")
            self.db.set_meta("mute_until", until)
            if self.tg:
                self.tg.send_message(chat_id, f"😴 Alerts muted for {hours}h (until {until[:16]} IST).\n"
                                              f"Use /unmute to resume anytime.")
        elif cmd == "/unmute":
            self.db.set_meta("mute_until", "0")
            if self.tg:
                self.tg.send_message(chat_id, "🔔 Alerts resumed!")
        elif cmd == "/broadcast":
            # Owner (private chat only) can send a custom message to ALL
            # registered chats (private + groups) with one command.
            if chat_type != "private":
                if self.tg:
                    self.tg.send_message(chat_id, "🔒 /broadcast works only in your private chat with the bot.")
                return
            parts = text.split(maxsplit=1)
            if len(parts) < 2 or not self.tg:
                if self.tg:
                    self.tg.send_message(chat_id, "Usage: /broadcast Your message here")
                return
            from formatter import esc
            chats = self.db.get_chats()
            n = self.tg.broadcast(chats, f"📢 <b>Announcement</b>\n\n{esc(parts[1])}",
                                  disable_preview=True)
            self.tg.send_message(chat_id, f"✅ Broadcast sent to {n} chat(s)")
        elif cmd == "/help":
            if self.tg:
                self.tg.send_message(chat_id, fmt_help())

    # ------------------------------------------------ main loop
    def run_forever(self, platform_override: str = ""):
        interval = int(self.config.get("check_interval_seconds", 60))
        log.info("=" * 60)
        log.info("DealBot starting · interval %ss · platforms: %s",
                 interval, ", ".join(k for k, v in self.config.get("platforms", {}).items() if v))
        log.info("Telegram: %s", "connected" if self.tg else "waiting for token in config.json")
        log.info("=" * 60)
        while True:
            try:
                self.run_cycle(platform_override=platform_override)
                self.maybe_digest()
                self.maybe_weekly_report()
                try:
                    rb = int(self.config.get("product_rechecks_per_cycle", 4))
                except (TypeError, ValueError):
                    rb = 4
                self.recheck_due_products(budget=rb)
            except KeyboardInterrupt:
                log.info("Stopped by user")
                break
            except Exception as e:
                log.exception("cycle crashed: %s", str(e)[:200])
            time.sleep(interval)


def _digits(s: str) -> int:
    import re as _re
    m = _re.search(r"\d+", s)
    return int(m.group(0)) if m else 8


def main():
    parser = argparse.ArgumentParser(description="DealBot 24/7")
    parser.add_argument("--once", action="store_true", help="run one check cycle and exit")
    parser.add_argument("--test-alert", action="store_true", help="send test message")
    parser.add_argument("--platform", default="", help="force one platform for this run (flipkart/amazon/meesho)")
    args = parser.parse_args()

    setup_logging()
    start_health_server()
    bot = DealBot()

    if args.test_alert:
        chats = bot.db.get_chats()
        if not bot.tg:
            print("❌ Telegram token not configured yet (config.json -> bot_token)")
            return
        if not chats:
            print("⚠️ No chats registered yet. Open your bot in Telegram and send /start")
            return
        n = bot.tg.broadcast(chats, fmt_test())
        print(f"✅ Test message sent to {n} chat(s)")
        return

    if args.once:
        # CATCH-UP MODE (GitHub Actions): GitHub's cron scheduler is
        # best-effort and often fires late or skips slots entirely. Instead of
        # one 4-minute cycle per trigger, we keep running full sweep cycles
        # back-to-back until the catch-up budget is exhausted. One Actions run
        # now covers up to ~8 minutes of continuous checking, so even a badly
        # delayed cron still gives the user continuous deal coverage.
        catchup = int(bot.config.get("catchup_seconds", 470))
        started = time.time()
        cycles = 0
        while True:
            cycles += 1
            bot.run_cycle(platform_override=args.platform)
            bot.maybe_digest()
            bot.maybe_weekly_report()
            try:
                rb = int(bot.config.get("product_rechecks_per_cycle", 4))
            except (TypeError, ValueError):
                rb = 4
            bot.recheck_due_products(budget=rb)
            if time.time() - started >= catchup:
                break
        log.info("Catch-up session done: %d full cycle(s) in %ds",
                 cycles, int(time.time() - started))
        return

    bot.run_forever(platform_override=args.platform)


if __name__ == "__main__":
    main()
