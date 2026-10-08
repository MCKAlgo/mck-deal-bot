"""SQLite database layer for DealBot - price history, dedup, chat registry."""
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def now_ist() -> datetime:
    return datetime.now(IST)


class Database:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    def _migrate(self):
        cur = self.conn.cursor()
        cur.executescript("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            title TEXT NOT NULL,
            category TEXT DEFAULT 'general',
            url TEXT,
            image_url TEXT,
            mrp REAL,
            current_price REAL,
            discount_pct REAL,
            rating REAL,
            reviews_count INTEGER,
            last_score REAL DEFAULT 0,
            last_reasons TEXT DEFAULT '',
            first_seen TEXT,
            last_seen TEXT,
            UNIQUE(platform, fingerprint)
        );
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            price REAL NOT NULL,
            recorded_at TEXT NOT NULL,
            FOREIGN KEY(product_id) REFERENCES products(id)
        );
        CREATE INDEX IF NOT EXISTS idx_history_product ON price_history(product_id, recorded_at);
        CREATE TABLE IF NOT EXISTS alerts_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            alert_type TEXT NOT NULL,
            score REAL,
            price REAL,
            sent_at TEXT NOT NULL,
            UNIQUE(product_id, alert_type, sent_at)
        );
        CREATE INDEX IF NOT EXISTS idx_alerts_product ON alerts_log(product_id, alert_type);
        CREATE TABLE IF NOT EXISTS chats (
            chat_id TEXT PRIMARY KEY,
            chat_type TEXT,
            title TEXT,
            added_at TEXT
        );
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)
        # ---- catalogue universe (discovered subcategories) ----
        cur.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            url TEXT NOT NULL,
            name TEXT,
            depth INTEGER DEFAULT 0,
            discovered_at TEXT,
            last_crawled TEXT,
            products_found INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            UNIQUE(platform, url)
        )
        """)
        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_categories_platform
            ON categories(platform, active)
        """)
        # ---- product master enrichment + adaptive scheduling ----
        for col, ddl in (("next_check_at", "TEXT"), ("popularity", "INTEGER"),
                         ("brand", "TEXT"), ("model", "TEXT"),
                         ("subcategory", "TEXT"), ("availability", "TEXT"),
                         ("seller", "TEXT")):
            try:
                cur.execute(f"ALTER TABLE products ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError:
                pass  # column already exists
        self.conn.commit()

    # ---------------- categories (catalogue universe) ----------------
    def add_category(self, platform: str, url: str, name: str = "",
                     depth: int = 0) -> bool:
        """Insert a discovered category; False if it already existed."""
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("""INSERT OR IGNORE INTO categories
                        (platform, url, name, depth, discovered_at)
                        VALUES (?,?,?,?,?)""",
                        (platform, url, name[:80], depth,
                         now_ist().isoformat(timespec="seconds")))
            self.conn.commit()
            return cur.rowcount > 0

    def category_count(self, platform: str) -> int:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT COUNT(*) c FROM categories WHERE platform=? AND active=1",
                        (platform,))
            return cur.fetchone()["c"]

    def pick_categories(self, platform: str, offset: int, budget: int) -> list:
        """Rotating slice of the discovered category universe for this cycle.
        Falls back to seeds: if universe is smaller than the seed list the
        scraper keeps its own seeds, so this only ADDS coverage."""
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("""SELECT id, url, name FROM categories
                        WHERE platform=? AND active=1
                        ORDER BY id""", (platform,))
            rows = [dict(r) for r in cur.fetchall()]
        if not rows:
            return []
        budget = max(1, min(budget, len(rows)))
        start = (max(0, offset) * budget) % len(rows)
        return [rows[(start + i) % len(rows)] for i in range(budget)]

    def mark_category_crawled(self, cat_id: int, products_found: int):
        with self.lock:
            self.conn.execute("""UPDATE categories SET last_crawled=?,
                              products_found=? WHERE id=?""",
                              (now_ist().isoformat(timespec="seconds"),
                               products_found, cat_id))
            self.conn.commit()

    def prune_dead_category(self, cat_id: int):
        """Category that never yields products gets parked (site changed)."""
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT products_found, last_crawled FROM categories WHERE id=?",
                        (cat_id,))
            row = cur.fetchone()
            if row and (row["products_found"] or 0) == 0 and row["last_crawled"]:
                cur.execute("UPDATE categories SET active=0 WHERE id=?", (cat_id,))
                self.conn.commit()

    # ---------------- products ----------------
    def upsert_product(self, p: dict):
        """Insert or refresh a product. Returns (product_id, is_new)."""
        ts = now_ist().isoformat(timespec="seconds")
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT id FROM products WHERE platform=? AND fingerprint=?",
                        (p["platform"], p["fingerprint"]))
            row = cur.fetchone()
            if row:
                pid = row["id"]
                cur.execute("""UPDATE products SET title=?, url=?, image_url=?, mrp=?,
                            current_price=?, discount_pct=?, rating=?, reviews_count=?,
                            category=?, last_seen=?,
                            brand=COALESCE(NULLIF(?, ''), brand),
                            model=COALESCE(?, model),
                            subcategory=COALESCE(?, subcategory),
                            availability=COALESCE(?, availability),
                            seller=COALESCE(?, seller)
                            WHERE id=?""",
                            (p["title"], p.get("url"), p.get("image_url"), p.get("mrp"),
                             p["current_price"], p.get("discount_pct"), p.get("rating"),
                             p.get("reviews_count"), p.get("category", "general"), ts,
                             p.get("brand") or "", p.get("model"), p.get("subcategory"),
                             p.get("availability"), p.get("seller"), pid))
                is_new = False
            else:
                cur.execute("""INSERT INTO products (platform, fingerprint, title, category, url,
                            image_url, mrp, current_price, discount_pct, rating, reviews_count,
                            brand, model, subcategory, availability, seller,
                            first_seen, last_seen)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (p["platform"], p["fingerprint"], p["title"], p.get("category", "general"),
                             p.get("url"), p.get("image_url"), p.get("mrp"), p["current_price"],
                             p.get("discount_pct"), p.get("rating"), p.get("reviews_count"),
                             p.get("brand"), p.get("model"), p.get("subcategory"),
                             p.get("availability"), p.get("seller"), ts, ts))
                pid = cur.lastrowid
                is_new = True
            self.conn.commit()
            return pid, is_new

    def record_price(self, product_id: int, price: float):
        with self.lock:
            self.conn.execute("INSERT INTO price_history (product_id, price, recorded_at) VALUES (?,?,?)",
                              (product_id, price, now_ist().isoformat(timespec="seconds")))
            # keep history lean: keep last 400 rows per product
            self.conn.execute("""DELETE FROM price_history WHERE product_id=? AND id NOT IN
                              (SELECT id FROM price_history WHERE product_id=? ORDER BY id DESC LIMIT 400)""",
                              (product_id, product_id))
            self.conn.commit()

    def price_stats(self, product_id: int) -> dict:
        """Rich price intelligence: all-time/7d/30d/90d lows & averages,
        7d/30d reference prices for drop detection, history span, data points."""
        with self.lock:
            cur = self.conn.cursor()
            n = now_ist()
            d7 = (n - timedelta(days=7)).isoformat(timespec="seconds")
            d30 = (n - timedelta(days=30)).isoformat(timespec="seconds")
            d90 = (n - timedelta(days=90)).isoformat(timespec="seconds")
            d6 = (n - timedelta(days=6)).isoformat(timespec="seconds")
            d14 = (n - timedelta(days=14)).isoformat(timespec="seconds")
            d25 = (n - timedelta(days=25)).isoformat(timespec="seconds")
            d40 = (n - timedelta(days=40)).isoformat(timespec="seconds")
            cur.execute("""SELECT MIN(price) amin, MAX(price) amax, COUNT(*) cnt,
                        COUNT(DISTINCT price) uniq,
                        MIN(CASE WHEN recorded_at >= ? THEN price END) wmin,
                        AVG(CASE WHEN recorded_at >= ? THEN price END) wavg,
                        MIN(CASE WHEN recorded_at >= ? THEN price END) m30low,
                        AVG(CASE WHEN recorded_at >= ? THEN price END) m30avg,
                        MIN(CASE WHEN recorded_at >= ? THEN price END) m90low,
                        AVG(CASE WHEN recorded_at >= ? THEN price END) m90avg,
                        AVG(CASE WHEN recorded_at BETWEEN ? AND ? THEN price END) d7_ref,
                        AVG(CASE WHEN recorded_at BETWEEN ? AND ? THEN price END) d30_ref,
                        MIN(recorded_at) first_ts
                        FROM price_history WHERE product_id=?""",
                        (d7, d7, d30, d30, d90, d90, d14, d6, d40, d25, product_id))
            row = dict(cur.fetchone())
            cur.execute("""SELECT price FROM price_history WHERE product_id=?
                        ORDER BY id ASC LIMIT 1""", (product_id,))
            first = cur.fetchone()
            row["first_price"] = first["price"] if first else None
            return row

    def last_change_info(self, product_id: int, current_price: float) -> dict:
        """When did the price last move (used for adaptive check scheduling)."""
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("""SELECT recorded_at, price FROM price_history
                        WHERE product_id=? AND price != ?
                        ORDER BY id DESC LIMIT 1""", (product_id, current_price))
            row = cur.fetchone()
            return dict(row) if row else {}

    def last_alert_info(self, product_id: int) -> dict:
        """Most recent alert sent for this product (price/score/time)."""
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("""SELECT sent_at, price, score, alert_type FROM alerts_log
                        WHERE product_id=? ORDER BY id DESC LIMIT 1""", (product_id,))
            row = cur.fetchone()
            return dict(row) if row else {}

    # ---------------- adaptive scheduling ----------------
    def set_next_check(self, product_id: int, when_iso: str):
        with self.lock:
            self.conn.execute("UPDATE products SET next_check_at=? WHERE id=?",
                              (when_iso, product_id))
            self.conn.commit()

    def set_popularity(self, product_id: int, popularity: int):
        with self.lock:
            self.conn.execute("UPDATE products SET popularity=? WHERE id=?",
                              (popularity, product_id))
            self.conn.commit()

    def due_products(self, limit: int = 5) -> list:
        """Products whose adaptive next-check time has arrived (NULL = never)."""
        with self.lock:
            cur = self.conn.cursor()
            now = now_ist().isoformat(timespec="seconds")
            cur.execute("""SELECT * FROM products
                        WHERE (next_check_at IS NULL OR next_check_at <= ?)
                        AND url IS NOT NULL AND url != ''
                        ORDER BY (next_check_at IS NULL) DESC, last_score DESC
                        LIMIT ?""", (now, limit))
            return [dict(r) for r in cur.fetchall()]

    def get_product(self, product_id: int):
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT * FROM products WHERE id=?", (product_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def all_products(self) -> list:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT * FROM products ORDER BY last_score DESC LIMIT 800")
            return [dict(r) for r in cur.fetchall()]

    def set_score(self, product_id: int, score: float, reasons: str):
        with self.lock:
            self.conn.execute("UPDATE products SET last_score=?, last_reasons=? WHERE id=?",
                              (score, reasons, product_id))
            self.conn.commit()

    def top_scored(self, limit=10) -> list:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("""SELECT * FROM products WHERE last_score > 0
                        ORDER BY last_score DESC LIMIT ?""", (limit,))
            return [dict(r) for r in cur.fetchall()]

    # ---------------- alerts / dedup ----------------
    def alert_allowed(self, product_id: int, alert_type: str, cooldown_hours: int,
                      current_price: float = None, improve_pct: float = 0.0) -> bool:
        """Dedup gate: one alert per product+rule per cooldown window.
        EXCEPTION: inside the window we still allow a re-alert when the deal
        MATERIALLY improved (price dropped improve_pct% below the last alerted
        price) - so better offers never get silently swallowed."""
        cutoff = (now_ist() - timedelta(hours=cooldown_hours)).isoformat(timespec="seconds")
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("""SELECT price FROM alerts_log WHERE product_id=? AND alert_type=?
                        ORDER BY id DESC LIMIT 1""", (product_id, alert_type))
            row = cur.fetchone()
            if not row:
                return True
            last_price = row["price"]
            if current_price and last_price and improve_pct > 0:
                if current_price <= last_price * (1 - improve_pct / 100.0):
                    return True  # materially better deal -> worth re-alerting
            cur.execute("""SELECT 1 FROM alerts_log WHERE product_id=? AND alert_type=?
                        AND sent_at >= ? LIMIT 1""", (product_id, alert_type, cutoff))
            return cur.fetchone() is None

    def log_alert(self, product_id: int, alert_type: str, score: float, price: float):
        with self.lock:
            self.conn.execute("""INSERT OR IGNORE INTO alerts_log (product_id, alert_type, score, price, sent_at)
                              VALUES (?,?,?,?,?)""",
                              (product_id, alert_type, score, price, now_ist().isoformat(timespec="seconds")))
            self.conn.commit()

    # ---------------- chats ----------------
    def add_chat(self, chat_id: str, chat_type: str, title: str = ""):
        with self.lock:
            self.conn.execute("""INSERT INTO chats (chat_id, chat_type, title, added_at)
                              VALUES (?,?,?,?)
                              ON CONFLICT(chat_id) DO UPDATE SET chat_type=excluded.chat_type,
                              title=excluded.title""",
                              (str(chat_id), chat_type, title, now_ist().isoformat(timespec="seconds")))
            self.conn.commit()

    def get_chats(self, chat_type: str = None) -> list:
        with self.lock:
            cur = self.conn.cursor()
            if chat_type:
                cur.execute("SELECT * FROM chats WHERE chat_type=?", (chat_type,))
            else:
                cur.execute("SELECT * FROM chats")
            return [dict(r) for r in cur.fetchall()]

    # ---------------- meta (mute, digest date, stats) ----------------
    def get_meta(self, key: str, default=None):
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("SELECT value FROM meta WHERE key=?", (key,))
            row = cur.fetchone()
            return row["value"] if row else default

    def set_meta(self, key: str, value: str):
        with self.lock:
            self.conn.execute("""INSERT INTO meta (key, value) VALUES (?,?)
                              ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, str(value)))
            self.conn.commit()

    def bump_stat(self, key: str, amount: int = 1):
        cur_val = self.get_meta(key, "0")
        try:
            self.set_meta(key, str(int(cur_val) + amount))
        except ValueError:
            self.set_meta(key, str(amount))

    def stats_summary(self) -> dict:
        with self.lock:
            cur = self.conn.cursor()
            out = {}
            cur.execute("SELECT COUNT(*) c FROM products")
            out["products"] = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) c FROM price_history")
            out["price_points"] = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) c FROM alerts_log")
            out["alerts_sent"] = cur.fetchone()["c"]
            cur.execute("SELECT platform, COUNT(*) c FROM products GROUP BY platform")
            out["by_platform"] = {r["platform"]: r["c"] for r in cur.fetchall()}
            cur.execute("SELECT MAX(recorded_at) t FROM price_history")
            row = cur.fetchone()
            out["last_check"] = row["t"] if row else None
            return out
