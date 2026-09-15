"""What IPTV Boss is serving: which category each stream is in, per layout.

IPTV Boss ids end in the layout number: stream 40870002 is layout 2. Maps are
filled from the full lists customers' players already request, and refreshed
on demand when an answer is needed and the map is missing or old.
"""
import time

ID_KEYS = {"live": "stream_id", "vod": "stream_id", "series": "series_id"}


def layout_of(item_id):
    try:
        value = int(str(item_id).split(".")[0])
    except (TypeError, ValueError):
        return None
    layout = value % 10000
    return layout or None


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

    def update(self, ctype, rows, username=None):
        """Replace the map for the layout these rows belong to. Returns the layout, or None."""
        if not isinstance(rows, list) or not rows:
            return None
        key = ID_KEYS[ctype]
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
        if username and layout:
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
