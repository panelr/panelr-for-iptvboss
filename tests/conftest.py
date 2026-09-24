"""A fake IPTV Boss built from real (redacted) responses, and a middleware in front of it."""
import copy
import gzip
import json
import os
import sys

import pytest
from aiohttp import web

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from middleware.app import create_app  # noqa: E402
from middleware.config import Config  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
USER, PASSWORD = "whistv1", "CUSTOMERPASS"
API_KEY = "test-key-0123456789abcdefghij"


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


LIVE_STREAMS = json.loads(load("get_live_streams.json"))
LIVE_CATEGORIES = json.loads(load("get_live_categories.json"))


def guide_channels_for(category_ids):
    ids, seen = [], set()
    for s in LIVE_STREAMS:
        if s["category_id"] in category_ids and s.get("epg_channel_id") and s["epg_channel_id"] not in seen:
            seen.add(s["epg_channel_id"])
            ids.append(s["epg_channel_id"])
    return ids


def build_guide(channel_ids, extra_channel="Orphan.ch", programmes_each=3):
    """XMLTV with channels first and programmes interleaved across channels, like IPTV Boss writes it.

    Ids are XML-escaped in attributes (A&E becomes A&amp;E), as IPTV Boss does.
    """
    from xml.sax.saxutils import escape
    parts = ['<?xml version="1.0" encoding="UTF-8"?>\n<tv source-info-name="IPTVBoss">\n']
    all_ids = list(channel_ids) + [extra_channel]
    for cid in all_ids:
        e = escape(cid, {'"': "&quot;"})
        parts.append(f'  <channel id="{e}">\n    <display-name>{escape(cid)}</display-name>\n  </channel>\n')
    for n in range(programmes_each):
        for cid in all_ids:
            e = escape(cid, {'"': "&quot;"})
            parts.append(f'  <programme start="2026091{n}000000 +0000" stop="2026091{n}010000 +0000" '
                         f'channel="{e}">\n    <title lang="en">Show {n} &amp; more</title>\n  </programme>\n')
    parts.append("</tv>\n")
    return "".join(parts).encode("utf-8")


# Categories 1 and 2 plus the first stream's epg ids of a third, so the guide covers a few categories.
GUIDE_CATEGORIES = [LIVE_CATEGORIES[0]["category_id"], LIVE_CATEGORIES[1]["category_id"],
                    LIVE_CATEGORIES[2]["category_id"]]
GUIDE = build_guide(guide_channels_for(GUIDE_CATEGORIES))
BIG_SHIFT = 300000 * 10000                    # pushes every id past 2,147,483,647 while keeping its layout digit


class FakeBoss:
    """Serves fixtures. Tests may change `extra_live_category` or `broken` to simulate server changes."""

    def __init__(self):
        self.extra_live_category = None     # (category row, stream row)
        self.broken = False
        self.big_ids = False                # serve ids past 2^31-1, as a very large layout does
        self.info_redirects = False         # answer get_vod_info / get_series_info with a 302, like IPTV Boss
        self.guide_body = None              # a fixed xmltv answer (b"" = the empty body IPTV Boss sends mid-rewrite)
        self.no_playlist_file = False       # refuse get.php, as IPTV Boss does before a sync has written the file
        self.layout_shift = 0               # add this to every stream id, to fake a second layout
        self.requests = []

    def app(self):
        app = web.Application()
        app.router.add_route("*", "/player_api.php", self.player_api)
        app.router.add_get("/get.php", self.get_php)
        app.router.add_get("/xmltv.php", self.xmltv)
        app.router.add_get("/panel_api.php", self.panel_api)
        app.router.add_get(r"/{kind:live|movie|series}/{u}/{p}/{file}", self.stream)
        app.router.add_get("/api/v1/users", self.api_users)
        app.router.add_route("*", "/boss.php/cloud/v1/{tail:.*}", self.cloud)
        return app

    def authed(self, request, form=None):
        q = form or request.query
        return q.get("username") == USER and q.get("password") == PASSWORD

    async def player_api(self, request):
        form = await request.post() if request.method == "POST" else None
        self.requests.append(("player_api", dict(form or request.query)))
        if not self.authed(request, form):
            return web.json_response({"user_info": {"auth": 0}})
        action = (form or request.query).get("action", "")
        if self.broken and action == "get_live_streams":
            return web.Response(text="<html>not json</html>", content_type="text/html")
        files = {"get_live_categories": "get_live_categories.json", "get_vod_categories": "get_vod_categories.json",
                 "get_series_categories": "get_series_categories.json", "get_live_streams": "get_live_streams.json",
                 "get_vod_streams": "get_vod_streams.json", "get_series": "get_series.json",
                 "get_short_epg": "get_short_epg.json", "get_simple_data_table": "get_simple_data_table.json",
                 "get_vod_info": "get_vod_info.json", "get_series_info": "get_series_info.json", "": "login.json"}
        if action not in files:
            return web.json_response([])
        if self.info_redirects and action in ("get_vod_info", "get_series_info"):
            return web.Response(status=302, headers={"Location": "http://provider.example/info"})
        data = json.loads(load(files[action]))
        if (self.big_ids or self.layout_shift) and isinstance(data, list):
            data = copy.deepcopy(data)
            for row in data:
                for key in ("stream_id", "series_id"):
                    if key in row and str(row[key]).isdigit():
                        value = int(row[key]) + (BIG_SHIFT if self.big_ids else 0) + self.layout_shift
                        row[key] = value if isinstance(row[key], int) else str(value)
        if self.extra_live_category and action == "get_live_categories":
            data = data + [self.extra_live_category[0]]
        if self.extra_live_category and action == "get_live_streams":
            data = data + [self.extra_live_category[1]]
        cat = request.query.get("category_id")
        if cat and isinstance(data, list):
            data = [r for r in data if r.get("category_id") == cat]
        body = json.dumps(data)
        return web.Response(body=body.encode(), headers={"Content-Type": "application/json;charset=utf-8"})

    async def get_php(self, request):
        if not self.authed(request):
            return web.Response(status=401)
        if self.no_playlist_file:
            return web.Response(status=403)
        return web.Response(body=load("get_m3u_plus.m3u").encode(),
                            headers={"Content-Type": "audio/x-mpegurl;charset=utf-8",
                                     "Content-Disposition": 'attachment; filename="playlist.m3u"'})

    async def xmltv(self, request):
        if not self.authed(request):
            return web.Response(status=401)
        if self.guide_body is not None:
            return web.Response(body=self.guide_body, headers={"Content-Type": "application/xml"})
        if "gzip" in request.headers.get("Accept-Encoding", ""):
            return web.Response(body=gzip.compress(GUIDE), headers={"Content-Type": "application/xml",
                                                                    "Content-Encoding": "gzip"})
        return web.Response(body=GUIDE, headers={"Content-Type": "application/xml"})

    async def panel_api(self, request):
        if not self.authed(request):
            return web.json_response({"user_info": {"auth": 0}})
        return web.Response(body=load("panel_api.json").encode(), headers={"Content-Type": "application/json"})

    async def stream(self, request):
        return web.Response(status=302, headers={"Location": f"http://provider.example/{request.match_info['file']}"})

    async def api_users(self, request):
        return web.json_response({"items": [], "from": "boss"})

    async def cloud(self, request):
        """IPTV Boss's cloud sync API: records what arrived and gzips JSON when asked, like the real server."""
        body = await request.read()
        self.requests.append(("cloud", {"accept_encoding": request.headers.get("Accept-Encoding"),
                                        "content_length": request.headers.get("Content-Length"),
                                        "transfer_encoding": request.headers.get("Transfer-Encoding"),
                                        "forwarded_proto": request.headers.get("X-Forwarded-Proto"),
                                        "size": len(body)}))
        data = json.dumps({"currentRevision": 285}).encode()
        if "gzip" in request.headers.get("Accept-Encoding", ""):
            return web.Response(body=gzip.compress(data), headers={"Content-Type": "application/json",
                                                                   "Content-Encoding": "gzip"})
        return web.Response(body=data, headers={"Content-Type": "application/json"})

    def last_cloud(self):
        return [seen for kind, seen in self.requests if kind == "cloud"][-1]


@pytest.fixture
def boss():
    return FakeBoss()


@pytest.fixture
async def mw(aiohttp_server, aiohttp_client, boss, tmp_path):
    server = await aiohttp_server(boss.app())
    config = Config.from_env({"MW_BOSS_URL": str(server.make_url("")).rstrip("/"), "MW_API_KEY": API_KEY,
                              "MW_DATA_DIR": str(tmp_path / "data"), "MW_GUIDE_RECHECK_SECONDS": "0"})
    client = await aiohttp_client(create_app(config))
    client.boss = boss
    return client


def auth():
    return {"X-Middleware-Key": API_KEY}


def extra_category():
    cat = {"category_id": "9999", "category_name": "BRAND NEW", "parent_id": 0}
    stream = copy.deepcopy(LIVE_STREAMS[0])
    stream.update({"stream_id": 99990002, "name": "New Channel", "category_id": "9999", "category_ids": ["9999"],
                   "epg_channel_id": "NewChannel.new"})
    return cat, stream
