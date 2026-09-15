"""SQLite storage: the category catalogue, customer picks and settings.

Category ids are IPTV Boss group ids. They are unique across layouts and
survive renames, reorders and rebuilds, so picks are stored by id.
"""
import json
import sqlite3
import threading
import time

TYPES = ("live", "vod", "series")
RULES = ("show", "hide")

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    type        TEXT    NOT NULL,
    id          TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0,
    layout      INTEGER,
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    removed_at  INTEGER,
    PRIMARY KEY (type, id)
);
CREATE TABLE IF NOT EXISTS picks (
    username      TEXT    NOT NULL,
    type          TEXT    NOT NULL,
    mode          TEXT    NOT NULL DEFAULT 'all',
    included      TEXT    NOT NULL DEFAULT '[]',
    excluded      TEXT    NOT NULL DEFAULT '[]',
    new_rule      TEXT    NOT NULL DEFAULT 'default',
    saved_at      INTEGER NOT NULL,
    PRIMARY KEY (username, type)
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path, default_rule="show"):
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()
        self._default_rule = default_rule
        self._cache = {}        # username -> {type: Picks}; cleared on every picks write
        self.version = 0        # bumps whenever picks or settings change

    def close(self):
        self._db.close()

    # ---- settings -------------------------------------------------------

    def new_categories_rule(self):
        row = self._db.execute("SELECT value FROM settings WHERE key = 'new_categories'").fetchone()
        return row[0] if row else self._default_rule

    def set_new_categories_rule(self, rule):
        if rule not in RULES:
            raise ValueError("new_categories must be show or hide")
        with self._lock:
            self._db.execute("INSERT INTO settings(key, value) VALUES('new_categories', ?) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (rule,))
            self._changed()

    # ---- catalogue ------------------------------------------------------

    def record_categories(self, ctype, rows):
        """Upsert the categories from a category list IPTV Boss returned."""
        now = int(time.time())
        with self._lock:
            for pos, row in enumerate(rows):
                if not isinstance(row, dict):
                    continue
                cid = str(row.get("category_id", "")).strip()
                if not cid:
                    continue
                self._db.execute(
                    "INSERT INTO categories(type, id, name, position, first_seen, last_seen) VALUES(?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(type, id) DO UPDATE SET name = excluded.name, position = excluded.position, "
                    "last_seen = excluded.last_seen, removed_at = NULL",
                    (ctype, cid, str(row.get("category_name", "")), pos, now, now))

    def record_layout_categories(self, ctype, layout, category_ids, mark_removed=False):
        """Tag categories with their layout.

        With mark_removed, `category_ids` is the layout's complete category list,
        so categories tagged to that layout but missing from it are marked removed.
        """
        if layout is None:
            return
        now = int(time.time())
        present = {str(c) for c in category_ids}
        with self._lock:
            for cid in present:
                self._db.execute("UPDATE categories SET layout = ? WHERE type = ? AND id = ?", (layout, ctype, cid))
            if not mark_removed:
                return
            known = [r[0] for r in self._db.execute(
                "SELECT id FROM categories WHERE type = ? AND layout = ? AND removed_at IS NULL", (ctype, layout))]
            for cid in known:
                if cid not in present:
                    self._db.execute("UPDATE categories SET removed_at = ? WHERE type = ? AND id = ?",
                                     (now, ctype, cid))

    def categories(self, ctype=None, include_removed=False, layout=None):
        sql = "SELECT type, id, name, position, layout, first_seen, last_seen, removed_at FROM categories"
        where, args = [], []
        if ctype:
            where.append("type = ?"); args.append(ctype)
        if layout is not None:
            where.append("(layout = ? OR layout IS NULL)"); args.append(layout)
        if not include_removed:
            where.append("removed_at IS NULL")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY type, layout, position"
        keys = ("type", "id", "name", "position", "layout", "first_seen", "last_seen", "removed_at")
        return [dict(zip(keys, r)) for r in self._db.execute(sql, args)]

    # ---- picks ----------------------------------------------------------

    def picks(self, username):
        """{type: Picks}. A type without saved picks shows everything."""
        cached = self._cache.get(username)
        if cached is not None:
            return cached
        result = {t: Picks(t) for t in TYPES}
        for ctype, mode, inc, exc, rule, saved in self._db.execute(
                "SELECT type, mode, included, excluded, new_rule, saved_at FROM picks WHERE username = ?",
                (username,)):
            if ctype in result:
                result[ctype] = Picks(ctype, mode, set(json.loads(inc)), set(json.loads(exc)), rule, saved)
        self._cache[username] = result
        return result

    def has_filtering(self, username):
        return any(p.filters for p in self.picks(username).values())

    def save_picks(self, username, ctype, mode, included, excluded, new_rule):
        if ctype not in TYPES:
            raise ValueError(f"type must be one of {', '.join(TYPES)}")
        if mode not in ("all", "selected"):
            raise ValueError("mode must be all or selected")
        if new_rule not in ("default",) + RULES:
            raise ValueError("new_categories must be default, show or hide")
        inc = sorted({str(i) for i in included})
        exc = sorted({str(i) for i in excluded} - set(inc))
        with self._lock:
            self._db.execute(
                "INSERT INTO picks(username, type, mode, included, excluded, new_rule, saved_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(username, type) DO UPDATE SET mode = excluded.mode, included = excluded.included, "
                "excluded = excluded.excluded, new_rule = excluded.new_rule, saved_at = excluded.saved_at",
                (username, ctype, mode, json.dumps(inc), json.dumps(exc), new_rule, int(time.time())))
            self._changed()

    def clear_picks(self, username):
        with self._lock:
            self._db.execute("DELETE FROM picks WHERE username = ?", (username,))
            self._changed()

    def rename_user(self, old, new):
        with self._lock:
            self._db.execute("DELETE FROM picks WHERE username = ?", (new,))
            self._db.execute("UPDATE picks SET username = ? WHERE username = ?", (new, old))
            self._changed()

    def _changed(self):
        self._cache.clear()
        self.version += 1


class Picks:
    """One customer's choice for one content type."""

    def __init__(self, ctype, mode="all", included=None, excluded=None, new_rule="default", saved_at=0):
        self.type = ctype
        self.mode = mode
        self.included = included or set()
        self.excluded = excluded or set()
        self.new_rule = new_rule
        self.saved_at = saved_at

    @property
    def filters(self):
        return self.mode == "selected"

    def rule(self, default_rule):
        return default_rule if self.new_rule == "default" else self.new_rule

    def allows(self, category_id, default_rule):
        """Whether a category is visible. Categories the customer never decided on follow the rule."""
        if not self.filters:
            return True
        cid = str(category_id)
        if cid in self.included:
            return True
        if cid in self.excluded:
            return False
        return self.rule(default_rule) == "show"

    def as_dict(self):
        return {
            "mode": self.mode,
            "included": sorted(self.included),
            "excluded": sorted(self.excluded),
            "new_categories": self.new_rule,
            "saved_at": self.saved_at,
        }
