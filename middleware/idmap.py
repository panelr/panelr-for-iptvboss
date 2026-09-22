"""Keep IPTV Boss ids inside 32 bits for players that store them as int.

IPTV Boss builds a stream or series id as item * 10000 + layout (stream 40870002
is item 4087 in layout 2). A large layout pushes ids past 2,147,483,647 and
TiviMate stops reading the whole list at the first one, so movies and series
vanish. Ids over that limit are sent to players compacted as
item * 100 + layout * 10 + 7, and turned back into the real id whenever a player
sends one back. Ids under the limit are never touched, and a compacted id always
expands to an id over the limit, so the two forms cannot be confused.
"""
import re

LIMIT = 2_147_483_647
MARK = 7
ID_KEYS = (b"stream_id", b"series_id", b"vod_id")

_JSON_ID = re.compile(rb'("(?:' + b"|".join(ID_KEYS) + rb')"\s*:\s*)("?)(\d{10,})\2')


def compact(value):
    """Compacted form of an id over the limit; any other value comes back unchanged."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return value
    item, layout = divmod(number, 10000)
    if number <= LIMIT or not 1 <= layout <= 9:
        return value
    small = item * 100 + layout * 10 + MARK
    if small > LIMIT:
        return value
    return small if isinstance(value, int) else str(small)


def expand(value):
    """Real id for a compacted one; any other value comes back unchanged."""
    text = str(value)
    if not text.isdigit():
        return value
    number = int(text)
    if number % 10 != MARK:
        return value
    item, layout = divmod(number // 10, 10)
    if not 1 <= layout <= 9:
        return value
    real = item * 10000 + layout
    if real <= LIMIT:
        return value
    return real if isinstance(value, int) else str(real)


def compact_json(body):
    """Compact every oversized stream_id / series_id / vod_id in a JSON answer, byte for byte otherwise."""
    if not body:
        return body

    def swap(m):
        new = compact(int(m.group(3)))
        if isinstance(new, int) and new != int(m.group(3)):
            return m.group(1) + m.group(2) + str(new).encode() + m.group(2)
        return m.group(0)

    return _JSON_ID.sub(swap, body)


def expand_params(params, keys=("stream_id", "series_id", "vod_id")):
    """A copy of query/form parameters with compacted ids expanded. None when nothing changed."""
    changed = {k: expand(params[k]) for k in keys if k in params}
    changed = {k: v for k, v in changed.items() if v != params[k]}
    if not changed:
        return None
    out = dict(params)
    out.update(changed)
    return out


_TAIL_ID = re.compile(r"^(?P<id>\d+)(?P<ext>\.[A-Za-z0-9]+)?$")


def expand_stream_tail(tail):
    """Expand the id in a stream path tail such as '21474900027.mkv'. None when nothing changed."""
    head, _, last = tail.rpartition("/")
    m = _TAIL_ID.match(last)
    if not m:
        return None
    real = expand(m.group("id"))
    if real == m.group("id"):
        return None
    return (head + "/" if head else "") + real + (m.group("ext") or "")
