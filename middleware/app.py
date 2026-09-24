"""The web app: proxies IPTV Boss, filters for customers with picks, and serves the panel API.

Customers without picks get IPTV Boss's answers untouched (apart from ids
kept inside 32 bits, see idmap.py). When filtering fails for any reason the
unfiltered answer is sent, so the middleware can show a customer too much
but never breaks their player.
"""
import asyncio
import gzip
import hmac
import json
import logging
import os
import re
import time
import zlib
from collections import OrderedDict
from urllib.parse import parse_qs, parse_qsl, urlencode

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web

from . import __version__, idmap, playlist
from .filters import empty_epg, filter_categories, filter_m3u, filter_panel_api, filter_rows
from .guide import GuideStore, file_version, gzip_stream
from .store import TYPES, Store
from .streams import StreamIndex, layout_of

log = logging.getLogger("middleware")

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
              "trailers", "transfer-encoding", "upgrade"}
CATEGORY_ACTIONS = {"get_live_categories": "live", "get_vod_categories": "vod", "get_series_categories": "series"}
LIST_ACTIONS = {"get_live_streams": "live", "get_vod_streams": "vod", "get_series": "series"}
# Answers that carry stream or series ids: they are kept inside 32 bits for players (idmap.py).
ID_ACTIONS = set(LIST_ACTIONS) | {"get_vod_info", "get_series_info", "get_short_epg", "get_simple_data_table"}
ID_PARAMS = ("stream_id", "series_id", "vod_id")
STREAM_PATHS = {"live": "live", "movie": "vod", "series": "series"}
MAP_MAX_AGE = 300
LIST_CACHE_MIN = 256 * 1024     # smaller lists are prepared per request; caching them buys nothing


def json_response(data, status=200):
    return web.json_response(data, status=status, dumps=lambda d: json.dumps(d, separators=(",", ":")))


def raw_response(status, headers, body):
    resp = web.Response(status=status, body=body)
    if headers.get("Content-Type"):
        resp.headers["Content-Type"] = headers["Content-Type"]
    return resp


def passthrough_response(status, headers, body):
    """A whole answer sent on with its headers, so a redirect keeps its Location."""
    resp = web.Response(status=status, body=body)
    for k, v in headers.items():
        if k.lower() not in HOP_BY_HOP and k.lower() not in ("content-length", "content-encoding"):
            resp.headers.add(k, v)
    return resp


class Middleware:
    def __init__(self, config):
        self.config = config
        os.makedirs(config.data_dir, exist_ok=True)
        self.store = Store(os.path.join(config.data_dir, "middleware.db"), config.new_categories,
                           config.category_grace_days)
        self.streams = StreamIndex()
        self.guides = GuideStore(config.data_dir)
        self.session = None
        self._guide_locks = {}
        self._map_locks = {}
        self._recorded = set()                    # (type, layout, size, crc32) already written to the store
        self._lists = OrderedDict()               # (size, crc32) -> (compacted, gzipped): full lists, no picks
        self._lists_bytes = 0
        self._list_locks = {}

    # ---- lifecycle ------------------------------------------------------

    async def start(self, app):
        timeout = ClientTimeout(total=None, sock_connect=10, sock_read=self.config.request_timeout)
        # Sent on every call to IPTV Boss unless the player's proxy already sent its own.
        headers = {"X-Forwarded-Proto": self.config.forwarded_proto} if self.config.forwarded_proto else None
        self.session = ClientSession(timeout=timeout, auto_decompress=False, connector=TCPConnector(limit=200),
                                     headers=headers)

    async def stop(self, app):
        if self.session:
            await self.session.close()
        self.store.close()

    # ---- helpers --------------------------------------------------------

    @staticmethod
    def path_qs(request):
        """What to ask IPTV Boss for: the player's own path, or one with its compacted ids expanded."""
        return request.get("mw_path_qs") or request.rel_url.path_qs

    def upstream(self, request):
        return self.config.boss_url + self.path_qs(request)

    @staticmethod
    def forward_headers(request, drop_encoding=False):
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
        headers.pop("Content-Length", None)
        # No Accept-Encoding from the client: ask for identity, else aiohttp adds gzip and we stream it back undecoded.
        if drop_encoding or "Accept-Encoding" not in headers:
            headers.pop("Accept-Encoding", None)
            headers["Accept-Encoding"] = "identity"
        if request.remote and "X-Forwarded-For" not in headers:
            headers["X-Forwarded-For"] = request.remote
        return headers

    def allows_fn(self, username):
        picks = self.store.picks(username)
        rule = self.store.new_categories_rule()
        return lambda ctype, cid: picks[ctype].allows(cid, rule)

    def filters(self, username, ctype=None):
        if not username:
            return False
        picks = self.store.picks(username)
        return picks[ctype].filters if ctype else any(p.filters for p in picks.values())

    @staticmethod
    async def params(request):
        """Query parameters merged with a form body. The raw body is kept for forwarding."""
        merged = dict(request.query)
        if request.method == "POST" and request.can_read_body:
            body = await request.read()
            request["mw_body"] = body
            if "form" in request.headers.get("Content-Type", ""):
                for key, values in parse_qs(body.decode("utf-8", "replace")).items():
                    merged.setdefault(key, values[0])
        return merged

    @staticmethod
    def expand_ids(request, params):
        """Turn compacted ids a player sends back into IPTV Boss's real ids, in the query and the form body."""
        expanded = idmap.expand_params(params, ID_PARAMS)
        if expanded is None:
            return params
        raw_query = request.rel_url.raw_query_string
        if raw_query:
            new_query = re.sub(r"((?:^|&)(?:stream_id|series_id|vod_id)=)(\d+)",
                               lambda m: m.group(1) + str(idmap.expand(m.group(2))), raw_query)
            if new_query != raw_query:
                request["mw_path_qs"] = request.rel_url.raw_path + "?" + new_query
        body = request.get("mw_body")
        if body and "form" in request.headers.get("Content-Type", ""):
            form = [(k, idmap.expand(v) if k in ID_PARAMS else v)
                    for k, v in parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True)]
            request["mw_body"] = urlencode(form).encode("utf-8")
        return expanded

    async def proxy(self, request, forbidden=None):
        """Pass a request to IPTV Boss and stream the answer back unchanged.

        `forbidden` is an optional coroutine called instead when IPTV Boss refuses the request,
        which is how it answers for a customer whose playlist file has not been written yet.
        """
        if "mw_body" in request:
            data = request["mw_body"]
        else:
            data = request.content if request.can_read_body else None
        headers = self.forward_headers(request)
        if "mw_body" not in request and request.content_length is not None:
            headers["Content-Length"] = str(request.content_length)  # keep the size; IPTVBoss rejects uploads without it
        try:
            async with self.session.request(request.method, self.upstream(request), data=data,
                                            headers=headers, allow_redirects=False) as up:
                if up.status == 403 and forbidden is not None:
                    built = await forbidden()
                    if built is not None:
                        return built
                resp = web.StreamResponse(status=up.status, reason=up.reason)
                for k, v in up.headers.items():
                    if k.lower() not in HOP_BY_HOP:
                        resp.headers.add(k, v)
                await resp.prepare(request)
                async for chunk in up.content.iter_chunked(1 << 16):
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
        except (asyncio.TimeoutError, OSError) as e:
            log.warning("IPTV Boss did not answer %s: %s", request.rel_url.path, e)
            return web.Response(status=502, text="Upstream unavailable")

    async def proxy_ids(self, request):
        """Like proxy(), but the whole answer is read so oversized ids can be compacted.

        Anything that is not a JSON 200 (IPTV Boss answers info lookups with a redirect to the
        provider) is sent on with all of its headers, so the redirect still works.
        """
        try:
            status, headers, body = await self.fetch_like(request)
        except (asyncio.TimeoutError, OSError) as e:
            log.warning("IPTV Boss did not answer %s: %s", request.rel_url.path, e)
            return web.Response(status=502, text="Upstream unavailable")
        if status == 200 and "json" in headers.get("Content-Type", "").lower():
            return raw_response(status, headers, idmap.compact_json(body))
        return passthrough_response(status, headers, body)

    async def fetch(self, method, path_qs, headers=None, data=None):
        """Fetch from IPTV Boss uncompressed. Returns (status, headers, body bytes)."""
        headers = dict(headers or {})
        headers.pop("Accept-Encoding", None)
        headers["Accept-Encoding"] = "identity"
        async with self.session.request(method, self.config.boss_url + path_qs, headers=headers, data=data,
                                        allow_redirects=False) as up:
            body = await up.read()
            if up.headers.get("Content-Encoding", "").lower() == "gzip":
                body = zlib.decompress(body, wbits=31)
            return up.status, up.headers, body

    async def fetch_like(self, request):
        """Repeat the player's own request against IPTV Boss, uncompressed."""
        return await self.fetch(request.method, self.path_qs(request), self.forward_headers(request, True),
                                request.get("mw_body"))

    async def ensure_map(self, ctype, username, password, layout=None):
        """Make sure the stream map for a customer's layout is loaded and recent. Returns the layout."""
        layout = layout or self.streams.layout_for_user(username)
        if layout and self.streams.is_fresh(ctype, layout, MAP_MAX_AGE):
            return layout
        lock = self._map_locks.setdefault((ctype, layout or username), asyncio.Lock())
        async with lock:
            layout = layout or self.streams.layout_for_user(username)
            if layout and self.streams.is_fresh(ctype, layout, MAP_MAX_AGE):
                return layout
            action = {"live": "get_live_streams", "vod": "get_vod_streams", "series": "get_series"}[ctype]
            qs = "/player_api.php?" + urlencode({"username": username, "password": password, "action": action})
            status, _, body = await self.fetch("GET", qs)
            if status != 200:
                return layout
            found = self.streams.update(ctype, json.loads(body), username)
            if found:
                self.store.record_layout_categories(ctype, found, self.streams.layout_categories(ctype, found))
            return found or layout

    def record_categories(self, ctype, layout, body, rows):
        """Write a category list to the catalogue once per distinct answer."""
        seen = (ctype, layout, len(body), zlib.crc32(body))
        if seen in self._recorded:
            return
        self.store.record_categories(ctype, rows, layout)
        if layout:
            self.store.record_layout_categories(
                ctype, layout, [r.get("category_id") for r in rows if isinstance(r, dict)], mark_removed=True)
        if len(self._recorded) > 512:
            self._recorded.clear()
        self._recorded.add(seen)

    # ---- full lists for customers without picks ---------------------------

    async def full_list(self, request, headers, body):
        """A full list for a customer without picks: ids compacted, no parsing, prepared once per distinct answer."""
        wants_gzip = "gzip" in request.headers.get("Accept-Encoding", "").lower()
        loop = asyncio.get_running_loop()
        budget = self.config.list_cache_mb * 1024 * 1024
        if len(body) < LIST_CACHE_MIN or budget <= 0:
            compacted = idmap.compact_json(body)
            payload = gzip.compress(compacted, compresslevel=5, mtime=0) if wants_gzip else compacted
        else:
            key = (len(body), await loop.run_in_executor(None, zlib.crc32, body))
            entry = self._lists.get(key)
            if entry is None:
                lock = self._list_locks.setdefault(key, asyncio.Lock())
                async with lock:
                    entry = self._lists.get(key)
                    if entry is None:
                        compacted = await loop.run_in_executor(None, idmap.compact_json, body)
                        zipped = await loop.run_in_executor(None, _gzip, compacted)
                        entry = (compacted, zipped)
                        self._lists[key] = entry
                        self._lists_bytes += len(compacted) + len(zipped)
                        while self._lists_bytes > budget and len(self._lists) > 1:
                            _, old = self._lists.popitem(last=False)
                            self._lists_bytes -= len(old[0]) + len(old[1])
                self._list_locks.pop(key, None)
            if key in self._lists:
                self._lists.move_to_end(key)
            payload = entry[1] if wants_gzip else entry[0]
        resp = web.Response(status=200, body=payload)
        resp.headers["Content-Type"] = headers.get("Content-Type", "application/json")
        resp.headers["Vary"] = "Accept-Encoding"
        if wants_gzip:
            resp.headers["Content-Encoding"] = "gzip"
        return resp

    # ---- player_api.php --------------------------------------------------

    async def player_api(self, request):
        p = self.expand_ids(request, await self.params(request))
        username, action = p.get("username", ""), p.get("action", "")
        watched = action in CATEGORY_ACTIONS or action in LIST_ACTIONS
        plain = self.proxy_ids if action in ID_ACTIONS else self.proxy
        if not username or (not watched and not self.filters(username)):
            return await plain(request)
        try:
            return await self._player_api(request, p)
        except Exception:
            log.exception("filtering player_api %s failed; sending it unfiltered", action)
            return await plain(request)

    async def _player_api(self, request, p):
        username, password, action = p.get("username", ""), p.get("password", ""), p.get("action", "")
        allows = self.allows_fn(username)

        if action in CATEGORY_ACTIONS:
            ctype = CATEGORY_ACTIONS[action]
            status, headers, body = await self.fetch_like(request)
            if status != 200:
                return raw_response(status, headers, body)
            rows = json.loads(body)
            if isinstance(rows, list):
                self.record_categories(ctype, self.streams.layout_for_user(username), body, rows)
            if not self.filters(username, ctype):
                return raw_response(status, headers, body)
            return json_response(filter_categories(rows, ctype, allows))

        if action in LIST_ACTIONS:
            ctype = LIST_ACTIONS[action]
            status, headers, body = await self.fetch_like(request)
            if status != 200:
                return passthrough_response(status, headers, body)
            if not self.filters(username, ctype):
                # Nothing to filter, so nothing is parsed: the map for this layout loads when it is needed.
                return await self.full_list(request, headers, body)
            rows = json.loads(body)
            category = p.get("category_id")
            if category in (None, "") and isinstance(rows, list):
                layout = self.streams.update(ctype, rows, username)
                if layout:
                    self.store.record_layout_categories(ctype, layout, self.streams.layout_categories(ctype, layout))
            if category not in (None, "") and not allows(ctype, category):
                return json_response([])
            return self.json_ids(filter_rows(rows, ctype, allows))

        if action in ("get_short_epg", "get_simple_data_table") and self.filters(username, "live"):
            if not await self.item_allowed("live", p.get("stream_id", ""), username, password, allows):
                return json_response(empty_epg())
            return await self.proxy_ids(request)

        if action == "get_vod_info" and self.filters(username, "vod"):
            if not await self.item_allowed("vod", p.get("vod_id", ""), username, password, allows):
                return json_response({"info": [], "movie_data": []})
            return await self.proxy_ids(request)

        if action == "get_series_info" and self.filters(username, "series"):
            if not await self.item_allowed("series", p.get("series_id", ""), username, password, allows):
                return json_response({"seasons": [], "info": [], "episodes": {}})
            return await self.proxy_ids(request)

        if action in ID_ACTIONS:
            return await self.proxy_ids(request)
        return await self.proxy(request)

    @staticmethod
    def json_ids(data):
        body = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return web.Response(body=idmap.compact_json(body), content_type="application/json")

    async def item_allowed(self, ctype, item_id, username, password, allows):
        layout = layout_of(item_id)
        if layout is None:
            return True
        self.streams.remember_user_layout(username, layout)
        cats = self.streams.categories_of(ctype, item_id)
        if cats is None:
            await self.ensure_map(ctype, username, password, layout)
            cats = self.streams.categories_of(ctype, item_id)
        return not cats or any(allows(ctype, c) for c in cats)

    # ---- panel_api.php ---------------------------------------------------

    async def panel_api(self, request):
        username = (await self.params(request)).get("username", "")
        if not self.filters(username):
            return await self.proxy_ids(request)
        try:
            status, headers, body = await self.fetch_like(request)
            if status != 200:
                return passthrough_response(status, headers, body)
            return self.json_ids(filter_panel_api(json.loads(body), self.allows_fn(username)))
        except Exception:
            log.exception("filtering panel_api failed; sending it unfiltered")
            return await self.proxy_ids(request)

    # ---- get.php ------------------------------------------------------------

    async def get_php(self, request):
        p = await self.params(request)
        username, password = p.get("username", ""), p.get("password", "")
        if not self.filters(username):
            return await self.proxy(request, forbidden=lambda: self.built_playlist(request, username, password))
        try:
            status, headers, body = await self.fetch_like(request)
            if status == 403:
                built = await self.built_playlist(request, username, password)
                if built is not None:
                    status, headers, body = 200, dict(built.headers), built.body
            text = body.decode("utf-8", "replace")
            if status != 200 or not text.lstrip().startswith("#EXTM3U"):
                return raw_response(status, headers, body)
            layout = await self.ensure_map("live", username, password)
            names = {}
            for row in self.store.categories(layout=layout):
                names.setdefault(row["name"], []).append((row["type"], row["id"]))
            allows = self.allows_fn(username)

            def allows_group(name):
                known = names.get(name)
                return not known or any(allows(t, cid) for t, cid in known)

            out = web.Response(status=200, body=filter_m3u(text, allows_group).encode("utf-8"))
            out.headers["Content-Type"] = headers.get("Content-Type", "audio/x-mpegurl; charset=utf-8")
            if headers.get("Content-Disposition"):
                out.headers["Content-Disposition"] = headers["Content-Disposition"]
            return out
        except Exception:
            log.exception("filtering get.php failed; sending it unfiltered")
            return await self.proxy(request)

    async def built_playlist(self, request, username, password):
        """The customer's playlist, built from IPTV Boss's channel list.

        IPTV Boss writes each customer a playlist file during a sync and refuses get.php until
        that file exists, so anyone created between syncs cannot watch. The channels themselves
        come from the database, so the playlist is built from player_api.php instead and the
        customer is served straight away. Returns None when that cannot be done, which leaves
        IPTV Boss's own answer in place.
        """
        if not username or not password:
            return None
        try:
            rows = {}
            for key, action in (("categories", "get_live_categories"), ("streams", "get_live_streams")):
                qs = "/player_api.php?" + urlencode({"username": username, "password": password, "action": action})
                status, headers, body = await self.fetch("GET", qs)
                if status != 200 or "json" not in headers.get("Content-Type", "").lower():
                    return None
                rows[key] = json.loads(body)
            if not isinstance(rows["streams"], list) or not rows["streams"]:
                return None
            extension = "m3u8" if request.query.get("output", "ts").lower() == "m3u8" else "ts"
            text = playlist.build(rows["categories"], rows["streams"], self.public_base(request),
                                  username, password, extension)
        except (asyncio.TimeoutError, OSError, ValueError):
            log.exception("building a playlist for %s failed; leaving IPTV Boss's answer alone", username)
            return None
        log.info("playlist built for %s from the channel list: %d channels (IPTV Boss has no file yet)",
                 username, len(rows["streams"]))
        resp = web.Response(status=200, body=text.encode("utf-8"))
        resp.headers["Content-Type"] = "audio/x-mpegurl; charset=utf-8"
        resp.headers["Content-Disposition"] = 'attachment; filename="playlist.m3u"'
        return resp

    def public_base(self, request):
        """The address the player used, so the URLs in the playlist come back to this server."""
        proto = request.headers.get("X-Forwarded-Proto") or request.scheme
        host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or request.url.host
        return "%s://%s" % (proto.split(",")[0].strip(), host.split(",")[0].strip())

    # ---- xmltv.php ----------------------------------------------------------

    async def xmltv(self, request):
        p = await self.params(request)
        username, password = p.get("username", ""), p.get("password", "")
        if not self.filters(username, "live"):
            return await self.proxy(request)
        try:
            layout = await self.ensure_map("live", username, password)
            index = await self.ensure_guide(layout, username, password) if layout else None
            allows = self.allows_fn(username)
            channels = self.streams.epg_channels(layout, lambda cid: allows("live", cid)) if index else None
            if channels is None:
                return await self.proxy(request)
        except Exception:
            log.exception("building the guide failed; sending it unfiltered")
            return await self.proxy(request)

        gzip_ok = "gzip" in request.headers.get("Accept-Encoding", "").lower()
        resp = web.StreamResponse(status=200)
        resp.headers["Content-Type"] = "application/xml; charset=utf-8"
        resp.headers["Content-Disposition"] = 'attachment; filename="guide.xml"'
        if gzip_ok:
            resp.headers["Content-Encoding"] = "gzip"
        await resp.prepare(request)
        source = index.chunks(channels)
        if gzip_ok:
            source = gzip_stream(source)
        loop = asyncio.get_running_loop()
        done = object()
        while True:
            chunk = await loop.run_in_executor(None, next, source, done)
            if chunk is done:
                break
            if chunk:
                await resp.write(chunk)
        await resp.write_eof()
        return resp

    async def ensure_guide(self, layout, username, password):
        """Rebuild the layout's guide index when IPTV Boss's guide has changed.

        While one request rebuilds, the others keep using the index that is already there. A
        download that is not a whole guide (IPTV Boss answers with an empty body while it is
        writing a new file) is thrown away and the current index stays.
        """
        index = self.guides.get(layout)
        if index and self.guides.checked_recently(layout, self.config.guide_recheck_seconds):
            return index
        lock = self._guide_locks.setdefault(layout, asyncio.Lock())
        if lock.locked() and index:
            return index
        async with lock:
            index = self.guides.get(layout)
            if index and self.guides.checked_recently(layout, self.config.guide_recheck_seconds):
                return index
            spool = self.guides.spool()
            raw_path, xml_path = spool.name, spool.name + ".xml"
            try:
                url = self.config.boss_url + "/xmltv.php?" + urlencode({"username": username, "password": password})
                async with self.session.get(url, headers={"Accept-Encoding": "gzip"}, allow_redirects=False) as up:
                    if up.status != 200:
                        return index
                    compressed = up.headers.get("Content-Encoding", "").lower() == "gzip"
                    async for chunk in up.content.iter_chunked(1 << 20):
                        spool.write(chunk)
                spool.close()
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, _unpack, raw_path, xml_path, compressed)
                if not await loop.run_in_executor(None, _whole_guide, xml_path):
                    log.warning("IPTV Boss sent an incomplete guide for layout %s; keeping the current one", layout)
                    return index
                version = await loop.run_in_executor(None, file_version, xml_path)
                self.guides.mark_checked(layout)
                if index and index.version == version:
                    return index
                started = time.time()
                index = await loop.run_in_executor(None, self.guides.install, layout, xml_path, version)
                log.info("guide for layout %s rebuilt: %d channels in %.1fs", layout, len(index.order),
                         time.time() - started)
                return index
            finally:
                spool.close()
                for path in (raw_path, xml_path):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    # ---- streams ------------------------------------------------------------

    async def stream(self, request):
        kind = request.match_info["kind"]
        ctype = STREAM_PATHS.get(kind)
        username = request.match_info.get("username", "")
        tail = request.match_info.get("tail", "")
        # Series URLs carry episode ids, which are not IPTV Boss stream ids: never expanded, never refused.
        if kind != "series":
            real = idmap.expand_stream_tail(tail)
            if real is not None:
                tail = real
                head, _, last = request.rel_url.raw_path.rpartition("/")
                query = ("?" + request.rel_url.raw_query_string) if request.rel_url.raw_query_string else ""
                request["mw_path_qs"] = head + "/" + idmap.expand_stream_tail(last) + query
        if ctype and ctype != "series" and self.config.enforce_streams and self.filters(username, ctype):
            item = re.sub(r"\.[A-Za-z0-9]+$", "", tail.rsplit("/", 1)[-1])
            cats = self.streams.categories_of(ctype, item)
            if cats:
                allows = self.allows_fn(username)
                if not any(allows(ctype, c) for c in cats):
                    return web.Response(status=403, text="Not in your package")
        return await self.proxy(request)

    # ---- panel API ----------------------------------------------------------

    def authorized(self, request):
        given = request.headers.get("X-Middleware-Key", "")
        return bool(given) and hmac.compare_digest(given, self.config.api_key)

    @web.middleware
    async def api_auth(self, request, handler):
        if request.path.startswith("/middleware/v1/") and request.path != "/middleware/v1/health":
            if not self.authorized(request):
                return json_response({"error": "unauthorized",
                                      "message": "A valid X-Middleware-Key header is required."}, 401)
        return await handler(request)

    async def api_health(self, request):
        boss = "unreachable"
        try:
            async with self.session.get(self.config.boss_url + "/player_api.php",
                                        timeout=ClientTimeout(total=5)) as up:
                boss = "ok" if up.status < 500 else f"error {up.status}"
        except (asyncio.TimeoutError, OSError):
            pass
        return json_response({"status": "ok", "version": __version__, "boss": boss})

    async def api_settings(self, request):
        if request.method == "PUT":
            body = await _json_body(request)
            try:
                self.store.set_new_categories_rule(str(body.get("new_categories", "")))
            except ValueError as e:
                return json_response({"error": "invalid", "message": str(e)}, 422)
        elif request.method != "GET":
            return json_response({"error": "method_not_allowed"}, 405)
        return json_response({"new_categories": self.store.new_categories_rule(),
                              "enforce_streams": self.config.enforce_streams,
                              "category_grace_days": self.config.category_grace_days})

    async def api_categories(self, request):
        ctype = request.query.get("type")
        if ctype and ctype not in TYPES:
            return json_response({"error": "invalid", "message": "type must be live, vod or series"}, 422)
        layout = request.query.get("layout")
        rows = self.store.categories(ctype, request.query.get("include_removed") == "1",
                                     int(layout) if layout and layout.isdigit() else None)
        grouped = {t: [] for t in ((ctype,) if ctype else TYPES)}
        for r in rows:
            grouped[r["type"]].append({k: r[k] for k in ("id", "name", "position", "layout", "first_seen",
                                                          "removed_at")})
        return json_response(grouped)

    async def api_refresh(self, request):
        body = await _json_body(request)
        username, password = str(body.get("username", "")), str(body.get("password", ""))
        if not username or not password:
            return json_response({"error": "invalid", "message": "username and password are required"}, 422)
        told = body.get("layout")
        told = int(told) if isinstance(told, int) or (isinstance(told, str) and told.isdigit()) else None
        counts = {}
        for action, ctype in CATEGORY_ACTIONS.items():
            qs = "/player_api.php?" + urlencode({"username": username, "password": password, "action": action})
            status, _, raw = await self.fetch("GET", qs)
            if status != 200:
                return json_response({"error": "boss", "message": f"IPTV Boss answered {status}"}, 502)
            rows = json.loads(raw)
            if not isinstance(rows, list):
                return json_response({"error": "boss", "message": "IPTV Boss rejected the login"}, 502)
            # The panel knows which layout its service is on and says so; otherwise the login's own ids tell.
            found = await self.ensure_map(ctype, username, password)
            layout = told if told is not None else found
            self.record_categories(ctype, layout, raw, rows)
            counts[ctype] = len(rows)
        layout = told if told is not None else self.streams.layout_for_user(username)
        return json_response({"categories": counts, "layout": layout})

    async def api_picks(self, request):
        username = request.match_info["username"]
        if request.method == "DELETE":
            self.store.clear_picks(username)
        elif request.method == "PUT":
            body = await _json_body(request)
            try:
                self.save_picks(username, body)
            except ValueError as e:
                return json_response({"error": "invalid", "message": str(e)}, 422)
        elif request.method != "GET":
            return json_response({"error": "method_not_allowed"}, 405)
        return json_response(self.describe_picks(username))

    def save_picks(self, username, body):
        if not isinstance(body, dict):
            raise ValueError("body must be an object keyed by live, vod and series")
        rule = str(body.get("new_categories", "default"))
        told = body.get("layout")
        told = int(told) if isinstance(told, int) or (isinstance(told, str) and str(told).isdigit()) else None
        layout = told if told is not None else self.streams.layout_for_user(username)
        for ctype in TYPES:
            if ctype not in body:
                continue
            value = body[ctype]
            if value is None:
                self.store.save_picks(username, ctype, "all", [], [], rule)
            elif isinstance(value, list):
                chosen = {str(v) for v in value}
                if layout is not None:
                    known = {r["id"] for r in self.store.categories(ctype, layout=layout)}
                    self.store.save_picks(username, ctype, "selected", chosen, known - chosen, rule)
                else:
                    # Which layout this customer is on is not known yet (a brand new line): hide the rest,
                    # rather than excluding another layout's ids by mistake.
                    self.store.save_picks(username, ctype, "selected", chosen, [],
                                          "hide" if rule == "default" else rule)
            elif isinstance(value, dict):
                self.store.save_picks(username, ctype, str(value.get("mode", "selected")),
                                      value.get("included") or [], value.get("excluded") or [],
                                      str(value.get("new_categories", rule)))
            else:
                raise ValueError(f"{ctype} must be null, a list of category ids, or an object")

    def describe_picks(self, username):
        picks = self.store.picks(username)
        rule = self.store.new_categories_rule()
        layout = self.streams.layout_for_user(username)
        out = {"username": username, "default_new_categories": rule, "layout": layout, "types": {}}
        for ctype, pk in picks.items():
            entry = pk.as_dict()
            entry["effective_new_categories"] = pk.rule(rule)
            entry["undecided"] = []
            if pk.filters:
                for row in self.store.categories(ctype, layout=layout):
                    if row["id"] not in pk.included and row["id"] not in pk.excluded:
                        entry["undecided"].append({"id": row["id"], "name": row["name"],
                                                   "first_seen": row["first_seen"]})
            out["types"][ctype] = entry
        return out

    async def api_rename(self, request):
        body = await _json_body(request)
        new = str(body.get("username", "")).strip()
        if not new:
            return json_response({"error": "invalid", "message": "username is required"}, 422)
        self.store.rename_user(request.match_info["username"], new)
        return json_response(self.describe_picks(new))

    # ---- catch-all ------------------------------------------------------------

    async def passthrough(self, request):
        return await self.proxy(request)


def _unpack(raw_path, xml_path, compressed):
    if not compressed:
        os.replace(raw_path, xml_path)
        return
    d = zlib.decompressobj(wbits=31)
    with open(raw_path, "rb") as src, open(xml_path, "wb") as dst:
        while True:
            block = src.read(1 << 20)
            if not block:
                break
            data = block
            while data:
                dst.write(d.decompress(data))
                if d.eof:
                    data = d.unused_data
                    d = zlib.decompressobj(wbits=31)
                else:
                    data = b""
        dst.write(d.flush())


def _gzip(data):
    return gzip.compress(data, compresslevel=5, mtime=0)


def _whole_guide(xml_path):
    """A guide is only worth installing when it is a complete XMLTV file: non-empty and closed with </tv>."""
    try:
        size = os.path.getsize(xml_path)
        if size < 16:
            return False
        with open(xml_path, "rb") as f:
            f.seek(max(0, size - 64))
            return b"</tv>" in f.read()
    except OSError:
        return False


async def _json_body(request):
    try:
        return await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPUnprocessableEntity(text=json.dumps({"error": "invalid", "message": "body must be JSON"}),
                                          content_type="application/json")


def create_app(config):
    mw = Middleware(config)
    app = web.Application(middlewares=[mw.api_auth], client_max_size=16 * 1024 * 1024)
    app["middleware"] = mw
    app.on_startup.append(mw.start)
    app.on_cleanup.append(mw.stop)
    r = app.router
    r.add_get("/middleware/v1/health", mw.api_health)
    r.add_route("*", "/middleware/v1/settings", mw.api_settings)
    r.add_get("/middleware/v1/categories", mw.api_categories)
    r.add_post("/middleware/v1/categories/refresh", mw.api_refresh)
    r.add_route("*", "/middleware/v1/users/{username}/picks", mw.api_picks)
    r.add_post("/middleware/v1/users/{username}/rename", mw.api_rename)
    r.add_route("*", "/player_api.php", mw.player_api)
    r.add_route("*", "/panel_api.php", mw.panel_api)
    r.add_route("*", "/get.php", mw.get_php)
    r.add_route("*", "/xmltv.php", mw.xmltv)
    r.add_route("*", r"/{kind:live|movie|series}/{username}/{password}/{tail:.+}", mw.stream)
    r.add_route("*", "/{tail:.*}", mw.passthrough)
    return app
