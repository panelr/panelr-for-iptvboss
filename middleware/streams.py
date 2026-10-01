"""What IPTV Boss is serving: which category each stream is in, per layout.

IPTV Boss ids end in the layout number: stream 40870002 is layout 2. Maps are
filled from the full lists customers' players already request, and refreshed
on demand when an answer is needed and the map is missing or old.
"""
import time

ID_KEYS = {"live": "stream_id", "vod": "stream_id", "series": "series_id"}


def layout_of(item_id):
    """The layout an id belongs to, or None when it is not an IPTV Boss id. Layout 0 is a real layout."""
    try:
        value = int(str(item_id).split(".")[0])
    except (TypeError, ValueError):
        return None
    return value % 10000


guide_layout = layout_of


def category_of_row(row):
    ids = row.get("category_ids")
    if isinstance(ids, list) and ids:
        return [str(i) for i in ids]
    cid = row.get("category_id")
    return [str(cid)] if cid not in (None, "") else []


class LayoutMap:
    def __init__(self):
        self.categories = {}     # item id -> [category ids]
        self.epg = []            # live only: (category ids, epg channel id) in list order
        self.updated_at = 0.0


class StreamIndex:
    def __init__(self):
        self._maps = {}          # (type, layout) -> LayoutMap
        self._user_layout = {}   # username -> layout
        self._epg_ids = {}       # live stream id -> guide channel id ('' when none), every layout incl. 0
        self._epg_loaded = {}    # guide_layout() -> when its live list was last read

    def update(self, ctype, rows, username=None):
        """Replace the map for the layout these rows belong to. Returns the layout, or None."""
        if not isinstance(rows, list) or not rows:
            return None
        key = ID_KEYS[ctype]
        if ctype == "live":
            self._remember_epg_ids(rows)
        by_layout = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            layout = layout_of(row.get(key))
            if layout is None:
                continue
            m = by_layout.setdefault(layout, LayoutMap())
            cats = category_of_row(row)
            m.categories[str(row.get(key))] = cats
            if ctype == "live":
                epg = row.get("epg_channel_id")
                if epg:
                    m.epg.append((cats, str(epg)))
        if not by_layout:
            return None
        now = time.time()
        for layout, m in by_layout.items():
            m.updated_at = now
            self._maps[(ctype, layout)] = m
        main = max(by_layout, key=lambda lay: len(by_layout[lay].categories))
        if username:
            self._user_layout[username] = main
        return main

    def layout_for_user(self, username):
        return self._user_layout.get(username)

    def remember_user_layout(self, username, layout):
        if username and layout is not None:
            self._user_layout[username] = layout

    def get(self, ctype, layout):
        return self._maps.get((ctype, layout))

    def is_fresh(self, ctype, layout, max_age):
        m = self._maps.get((ctype, layout))
        return m is not None and time.time() - m.updated_at < max_age

    def categories_of(self, ctype, item_id):
        m = self._maps.get((ctype, layout_of(item_id)))
        if m is None:
            return None
        return m.categories.get(str(item_id))

    def _remember_epg_ids(self, rows):
        now, seen = time.time(), set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            sid = row.get("stream_id")
            layout = guide_layout(sid)
            if layout is None:
                continue
            seen.add(layout)
            self._epg_ids[str(sid)] = str(row.get("epg_channel_id") or "")
        for layout in seen:
            self._epg_loaded[layout] = now

    def epg_of(self, stream_id):
        """The guide channel id of a live stream, '' when it has none, or None when its layout's list is not read."""
        if guide_layout(stream_id) not in self._epg_loaded:
            return None
        return self._epg_ids.get(str(stream_id), "")

    def epg_fresh(self, layout, max_age):
        return time.time() - self._epg_loaded.get(layout, 0) < max_age

    def layout_categories(self, ctype, layout):
        m = self._maps.get((ctype, layout))
        if m is None:
            return set()
        return {c for cats in m.categories.values() for c in cats}

    def epg_channels(self, layout, allows):
        """Guide channel ids for the live streams a customer can see, in list order, without repeats."""
        m = self._maps.get(("live", layout))
        if m is None:
            return None
        seen, ordered = set(), []
        for cats, epg in m.epg:
            if epg in seen:
                continue
            if not cats or any(allows(c) for c in cats):
                seen.add(epg)
                ordered.append(epg)
        return ordered
