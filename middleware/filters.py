"""Trim IPTV Boss answers down to a customer's picks.

Every function takes an `allows(ctype, category_id)` callable. Rows whose
category can't be determined are kept, so a format change in IPTV Boss shows a
customer too much rather than breaking their player.
"""
import re

from .streams import category_of_row

PANEL_TYPES = {"live": "live", "created_live": "live", "radio_streams": "live", "movie": "vod", "series": "series"}
PANEL_CATEGORY_TYPES = {"live": "live", "movie": "vod", "series": "series"}
GROUP_TITLE = re.compile(r'group-title="([^"]*)"')


def _visible(row, ctype, allows):
    cats = category_of_row(row)
    return not cats or any(allows(ctype, c) for c in cats)


def filter_categories(rows, ctype, allows):
    if not isinstance(rows, list):
        return rows
    return [r for r in rows if not isinstance(r, dict) or allows(ctype, r.get("category_id"))]


def filter_rows(rows, ctype, allows):
    if not isinstance(rows, list):
        return rows
    return [r for r in rows if not isinstance(r, dict) or _visible(r, ctype, allows)]


def filter_panel_api(data, allows):
    if not isinstance(data, dict):
        return data
    cats = data.get("categories")
    if isinstance(cats, dict):
        for key, rows in list(cats.items()):
            ctype = PANEL_CATEGORY_TYPES.get(key)
            if ctype:
                cats[key] = filter_categories(rows, ctype, allows)
    chans = data.get("available_channels")
    if isinstance(chans, dict):
        kept = {}
        for sid, row in chans.items():
            ctype = PANEL_TYPES.get(str(row.get("stream_type", ""))) if isinstance(row, dict) else None
            if ctype is None or _visible(row, ctype, allows):
                kept[sid] = row
        data["available_channels"] = kept
    elif isinstance(chans, list):
        data["available_channels"] = [
            r for r in chans
            if not isinstance(r, dict) or PANEL_TYPES.get(str(r.get("stream_type", ""))) is None
            or _visible(r, PANEL_TYPES[str(r.get("stream_type"))], allows)]
    return data


def filter_m3u(text, allows_group):
    """Keep playlist entries whose group-title is allowed.

    An entry is an #EXTINF line plus everything up to and including its URL.
    Lines before the first entry (#EXTM3U and friends) are always kept.
    """
    out, entry, keep, in_entry = [], [], True, False
    for line in text.splitlines():
        if line.startswith("#EXTINF"):
            if in_entry and keep:
                out.extend(entry)
            m = GROUP_TITLE.search(line)
            keep = allows_group(m.group(1)) if m else True
            entry, in_entry = [line], True
            continue
        if not in_entry:
            out.append(line)
            continue
        entry.append(line)
        if line and not line.startswith("#"):
            if keep:
                out.extend(entry)
            entry, in_entry, keep = [], False, True
    if in_entry and keep:
        out.extend(entry)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def empty_epg():
    return {"epg_listings": []}
