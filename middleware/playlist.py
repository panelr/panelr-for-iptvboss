"""Build a customer's playlist from IPTV Boss's own channel list.

IPTV Boss answers get.php from a file it writes during a sync, so a customer created between
syncs has no file and is refused. The same channels are available from player_api.php, which
IPTV Boss answers from its database, so the playlist is built from there instead and the
customer can watch straight away.
"""

FIELDS = (("tvg-chno", "num"), ("tvg-id", "epg_channel_id"), ("tvg-name", "name"), ("tvg-logo", "stream_icon"))


def _quote(value):
    return str(value if value is not None else "").replace('"', "").replace("\n", " ").strip()


def build(categories, streams, base_url, username, password, extension="ts"):
    """An m3u_plus playlist, laid out the way IPTV Boss writes its own.

    `categories` and `streams` are the answers to get_live_categories and get_live_streams.
    Streams keep the order IPTV Boss gave them, which is the customer's channel order.
    """
    names = {}
    for row in categories or []:
        if isinstance(row, dict):
            names[str(row.get("category_id"))] = row.get("category_name") or ""

    lines = ["#EXTM3U"]
    for row in streams or []:
        if not isinstance(row, dict):
            continue
        stream_id = row.get("stream_id")
        if stream_id in (None, ""):
            continue
        attrs = " ".join('%s="%s"' % (attr, _quote(row.get(key))) for attr, key in FIELDS)
        group = _quote(names.get(str(row.get("category_id")), ""))
        name = _quote(row.get("name"))
        lines.append('#EXTINF:-1 %s group-title="%s",%s' % (attrs, group, name))
        lines.append("%s/live/%s/%s/%s.%s" % (base_url.rstrip("/"), username, password, stream_id, extension))
    return "\n".join(lines) + "\n"
