"""SQLite storage: the category catalogue, customer picks and settings.

Category ids are IPTV Boss group ids. They survive renames, reorders and
rebuilds, so picks are stored by id. They are NOT unique across layouts: two
layouts on one server can both have a group 12, so the catalogue is keyed by
(type, layout, id). A category learned before its layout is known sits under
UNKNOWN until a layout claims it.
"""
import json
import sqlite3
import threading
import time

TYPES = ("live", "vod", "series")
RULES = ("show", "hide")
UNKNOWN = -1              # layout not known yet; never a real IPTV Boss layout id (those start at 0)

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    type        TEXT    NOT NULL,
    layout      INTEGER NOT NULL DEFAULT -1,
    id          TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0,
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    removed_at  INTEGER,
    PRIMARY KEY (type, layout, id)
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
    def __init__(self, path, default_rule="show", grace_days=0):
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._migrate()
        self._db.executescript(SCHEMA)
        self._default_rule = default_rule
        self._grace = int(grace_days) * 86400      # 0 = a missing category is never marked removed
        self._cache = {}        # username -> {type: Picks}; cleared on every picks write
        self.version = 0        # bumps whenever picks, settings or the catalogue change
        self.writes = 0         # catalogue writes, for tests and the health page

    def close(self):
        self._db.close()

    def _migrate(self):
        """1.0 keyed categories by (type, id) with a nullable layout. Re-key by (type, layout, id)."""
        row = self._db.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'categories'").fetchone()
        if not row or "PRIMARY KEY (type, layout, id)" in row[0]:
            return
        with self._lock:
            self._db.executescript("""
                ALTER TABLE categories RENAME TO categories_v1;
                CREATE TABLE categories (
                    type TEXT NOT NULL, layout INTEGER NOT NULL DEFAULT -1, id TEXT NOT NULL, name TEXT NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0, first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
                    removed_at INTEGER, PRIMARY KEY (type, layout, id));
                INSERT OR IGNORE INTO categories(type, layout, id, name, position, first_seen, last_seen, removed_at)
                    SELECT type, COALESCE(layout, -1), id, name, position, first_seen, last_seen, removed_at
                    FROM categories_v1;
                DROP TABLE categories_v1;
            """)

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

    def record_categories(self, ctype, rows, layout=None):
        """Upsert the categories from a category list IPTV Boss returned, under the layout they belong to.

        With no layout they are filed under UNKNOWN and claimed later by record_layout_categories.
        """
        now = int(time.time())
        layout = UNKNOWN if layout is None else int(layout)
        with self._lock:
            before = self._signature(ctype, layout)
            for pos, row in enumerate(rows):
                if not isinstance(row, dict):
                    continue
                cid = str(row.get("category_id", "")).strip()
                if not cid:
                    continue
                self._db.execute(
                    "INSERT INTO categories(type, layout, id, name, position, first_seen, last_seen) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(type, layout, id) DO UPDATE SET name = excluded.name, position = excluded.position, "
                    "last_seen = excluded.last_seen, removed_at = NULL",
                    (ctype, layout, cid, str(row.get("category_name", "")), pos, now, now))
            self.writes += 1
            if self._signature(ctype, layout) != before:
                self._changed()

    def record_layout_categories(self, ctype, layout, category_ids, mark_removed=False):
        """A layout's category ids, as seen in one of its lists.

        Categories still filed under UNKNOWN move to this layout. With mark_removed, `category_ids`
        is the layout's complete list: a category of that layout missing from it is marked removed
        once it has been missing for the grace period (never, when the grace period is 0).
        """
        if layout is None:
            return
        layout, now = int(layout), int(time.time())
        present = {str(c) for c in category_ids}
        with self._lock:
            before = self._signature(ctype, layout)
            for cid in present:
                # claim, never overwrite: a real row for this layout always wins over an UNKNOWN one
                self._db.execute(
                    "UPDATE categories SET layout = ? WHERE type = ? AND layout = ? AND id = ? "
                    "AND NOT EXISTS (SELECT 1 FROM categories c WHERE c.type = ? AND c.layout = ? AND c.id = ?)",
                    (layout, ctype, UNKNOWN, cid, ctype, layout, cid))
                self._db.execute("UPDATE categories SET last_seen = ?, removed_at = NULL "
                                 "WHERE type = ? AND layout = ? AND id = ?", (now, ctype, layout, cid))
                # the same id filed before its layout was known is this row's older self: drop it
                self._db.execute("DELETE FROM categories WHERE type = ? AND layout = ? AND id = ?", (ctype, UNKNOWN, cid))
            if mark_removed and self._grace > 0:
                self._db.execute(
                    "UPDATE categories SET removed_at = ? WHERE type = ? AND layout = ? AND removed_at IS NULL "
                    "AND last_seen < ? AND id NOT IN (%s)" % ",".join("?" * len(present)) if present else
                    "UPDATE categories SET removed_at = ? WHERE type = ? AND layout = ? AND removed_at IS NULL "
                    "AND last_seen < ?",
                    [now, ctype, layout, now - self._grace] + sorted(present))
            if self._signature(ctype, layout) != before:
                self._changed()

    def categories(self, ctype=None, include_removed=False, layout=None):
        """Catalogue rows. With a layout: that layout's rows, or the UNKNOWN rows while it has none yet."""
        sql = "SELECT type, id, name, position, layout, first_seen, last_seen, removed_at FROM categories"
        where, args = [], []
        if ctype:
            where.append("type = ?"); args.append(ctype)
        if layout is not None:
            where.append("(layout = ? OR (layout = ? AND NOT EXISTS (SELECT 1 FROM categories b "
                         "WHERE b.type = categories.type AND b.layout = ? AND b.removed_at IS NULL)))")
            args += [int(layout), UNKNOWN, int(layout)]
        if not include_removed:
            where.append("removed_at IS NULL")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY type, layout, position, id"
        keys = ("type", "id", "name", "position", "layout", "first_seen", "last_seen", "removed_at")
        out = []
        for r in self._db.execute(sql, args):
            row = dict(zip(keys, r))
            if row["layout"] == UNKNOWN:
                row["layout"] = None
            out.append(row)
        return out

    def _signature(self, ctype, layout):
        """What depends on a layout's catalogue: its live ids, names and order."""
        return self._db.execute(
            "SELECT COUNT(*), GROUP_CONCAT(id || ':' || name, '|') FROM (SELECT id, name FROM categories "
            "WHERE type = ? AND layout = ? AND removed_at IS NULL ORDER BY position, id)", (ctype, layout)).fetchone()

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
