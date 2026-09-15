import gzip
import json
import xml.etree.ElementTree as ET

import pytest

from conftest import (API_KEY, GUIDE, GUIDE_CATEGORIES, LIVE_CATEGORIES, LIVE_STREAMS, PASSWORD, USER, auth,
                      extra_category, guide_channels_for, load)
from middleware.app import create_app
from middleware.config import Config

Q = f"username={USER}&password={PASSWORD}"


async def refresh(client):
    r = await client.post("/middleware/v1/categories/refresh", json={"username": USER, "password": PASSWORD},
                          headers=auth())
    assert r.status == 200, await r.text()
    return await r.json()


async def pick(client, body):
    r = await client.put(f"/middleware/v1/users/{USER}/picks", json=body, headers=auth())
    assert r.status == 200, await r.text()
    return await r.json()


async def test_untouched_without_picks(mw):
    r = await mw.get(f"/player_api.php?{Q}&action=get_live_categories")
    assert r.status == 200
    assert len(await r.json()) == len(LIVE_CATEGORIES)
    r = await mw.get(f"/get.php?{Q}&type=m3u_plus&output=ts")
    assert (await r.read()).decode() == load("get_m3u_plus.m3u")
    r = await mw.get(f"/xmltv.php?{Q}", headers={"Accept-Encoding": "identity"})
    assert await r.read() == GUIDE


async def test_login_and_unknown_paths_pass_through(mw):
    r = await mw.get(f"/player_api.php?{Q}")
    assert (await r.json())["user_info"]["username"] == "whistv1"
    r = await mw.get("/api/v1/users")
    assert (await r.json())["from"] == "boss"


async def test_api_needs_key(mw):
    assert (await mw.get("/middleware/v1/categories")).status == 401
    assert (await mw.get("/middleware/v1/categories", headers={"X-Middleware-Key": "wrong"})).status == 401
    r = await mw.get("/middleware/v1/health")
    assert r.status == 200 and (await r.json())["boss"] == "ok"


async def test_refresh_builds_catalogue(mw):
    body = await refresh(mw)
    assert body["categories"] == {"live": 74, "vod": 22, "series": 13}
    assert body["layout"] == 2
    cats = await (await mw.get("/middleware/v1/categories?type=live", headers=auth())).json()
    assert len(cats["live"]) == 74 and cats["live"][0]["layout"] == 2


async def test_picks_filter_live_lists_only(mw):
    await refresh(mw)
    first = LIVE_CATEGORIES[0]["category_id"]
    await pick(mw, {"live": [first]})
    cats = await (await mw.get(f"/player_api.php?{Q}&action=get_live_categories")).json()
    assert [c["category_id"] for c in cats] == [first]
    streams = await (await mw.get(f"/player_api.php?{Q}&action=get_live_streams")).json()
    assert streams and all(s["category_id"] == first for s in streams)
    assert len(streams) == sum(1 for s in LIVE_STREAMS if s["category_id"] == first)
    other = LIVE_CATEGORIES[1]["category_id"]
    assert await (await mw.get(f"/player_api.php?{Q}&action=get_live_streams&category_id={other}")).json() == []
    vod = await (await mw.get(f"/player_api.php?{Q}&action=get_vod_categories")).json()
    assert len(vod) == 22


async def test_post_login_is_filtered_and_forwarded(mw):
    await refresh(mw)
    first = LIVE_CATEGORIES[0]["category_id"]
    await pick(mw, {"live": [first]})
    r = await mw.post("/player_api.php", data={"username": USER, "password": PASSWORD,
                                               "action": "get_live_categories"})
    assert [c["category_id"] for c in await r.json()] == [first]


async def test_new_category_follows_rule(mw):
    await refresh(mw)
    first = LIVE_CATEGORIES[0]["category_id"]
    await pick(mw, {"live": [first]})
    mw.boss.extra_live_category = extra_category()
    await refresh(mw)

    ids = [c["category_id"] for c in await (await mw.get(f"/player_api.php?{Q}&action=get_live_categories")).json()]
    assert ids == [first, "9999"]                      # default rule: show

    picks = await (await mw.get(f"/middleware/v1/users/{USER}/picks", headers=auth())).json()
    assert [u["id"] for u in picks["types"]["live"]["undecided"]] == ["9999"]

    r = await mw.put("/middleware/v1/settings", json={"new_categories": "hide"}, headers=auth())
    assert (await r.json())["new_categories"] == "hide"
    ids = [c["category_id"] for c in await (await mw.get(f"/player_api.php?{Q}&action=get_live_categories")).json()]
    assert ids == [first]

    await pick(mw, {"live": {"mode": "selected", "included": [first], "excluded": [],
                             "new_categories": "show"}})
    ids = [c["category_id"] for c in await (await mw.get(f"/player_api.php?{Q}&action=get_live_categories")).json()]
    assert "9999" in ids


async def test_removed_category_is_flagged(mw):
    mw.boss.extra_live_category = extra_category()
    await refresh(mw)
    mw.boss.extra_live_category = None
    await refresh(mw)
    cats = await (await mw.get("/middleware/v1/categories?type=live&include_removed=1", headers=auth())).json()
    gone = [c for c in cats["live"] if c["id"] == "9999"]
    assert gone and gone[0]["removed_at"]
    active = await (await mw.get("/middleware/v1/categories?type=live", headers=auth())).json()
    assert all(c["id"] != "9999" for c in active["live"])


async def test_clearing_picks_restores_everything(mw):
    await refresh(mw)
    await pick(mw, {"live": [LIVE_CATEGORIES[0]["category_id"]]})
    r = await mw.delete(f"/middleware/v1/users/{USER}/picks", headers=auth())
    assert (await r.json())["types"]["live"]["mode"] == "all"
    assert len(await (await mw.get(f"/player_api.php?{Q}&action=get_live_categories")).json()) == 74


async def test_m3u_keeps_only_picked_groups(mw):
    await refresh(mw)
    first = LIVE_CATEGORIES[0]
    await pick(mw, {"live": [first["category_id"]]})
    r = await mw.get(f"/get.php?{Q}&type=m3u_plus&output=ts")
    assert r.headers["Content-Disposition"] == 'attachment; filename="playlist.m3u"'
    lines = (await r.read()).decode().splitlines()
    assert lines[0] == "#EXTM3U"
    groups = {l.split('group-title="')[1].split('"')[0] for l in lines if l.startswith("#EXTINF")}
    assert groups == {first["category_name"]}
    entries = [l for l in lines if l.startswith("#EXTINF")]
    urls = [l for l in lines if l.startswith("http")]
    assert len(entries) == len(urls) == sum(1 for s in LIVE_STREAMS if s["category_id"] == first["category_id"])


async def test_panel_api_filtered(mw):
    await refresh(mw)
    first = LIVE_CATEGORIES[0]["category_id"]
    await pick(mw, {"live": [first], "series": []})
    data = await (await mw.get(f"/panel_api.php?{Q}")).json()
    assert [c["category_id"] for c in data["categories"]["live"]] == [first]
    assert data["categories"]["series"] == []
    assert len(data["categories"]["movie"]) == 22
    kinds = {}
    for row in data["available_channels"].values():
        kinds.setdefault(row["stream_type"], set()).add(row["category_id"])
    assert kinds.get("live") == {first}
    assert "series" not in kinds


async def test_guide_contains_only_picked_channels(mw):
    await refresh(mw)
    chosen = GUIDE_CATEGORIES[1]
    await pick(mw, {"live": [chosen]})
    expected = guide_channels_for([chosen])
    r = await mw.get(f"/xmltv.php?{Q}", headers={"Accept-Encoding": "gzip"})
    assert r.status == 200
    raw = await r.read()
    tree = ET.fromstring(raw if raw.startswith(b"<?xml") else gzip.decompress(raw))
    assert [c.get("id") for c in tree.findall("channel")] == expected
    assert {p.get("channel") for p in tree.findall("programme")} == set(expected)
    assert len(tree.findall("programme")) == 3 * len(expected)

    r = await mw.get(f"/xmltv.php?{Q}", headers={"Accept-Encoding": "identity"})
    tree = ET.fromstring(await r.read())
    assert [c.get("id") for c in tree.findall("channel")] == expected


async def test_streams_outside_picks_are_refused(mw):
    await refresh(mw)
    first = LIVE_CATEGORIES[0]["category_id"]
    await pick(mw, {"live": [first]})
    await mw.get(f"/player_api.php?{Q}&action=get_live_streams")
    inside = next(s for s in LIVE_STREAMS if s["category_id"] == first)["stream_id"]
    outside = next(s for s in LIVE_STREAMS if s["category_id"] != first)["stream_id"]
    ok = await mw.get(f"/live/{USER}/{PASSWORD}/{inside}.ts", allow_redirects=False)
    assert ok.status == 302
    refused = await mw.get(f"/live/{USER}/{PASSWORD}/{outside}.ts", allow_redirects=False)
    assert refused.status == 403


async def test_short_epg_outside_picks_is_empty(mw):
    await refresh(mw)
    first = LIVE_CATEGORIES[0]["category_id"]
    await pick(mw, {"live": [first]})
    outside = next(s for s in LIVE_STREAMS if s["category_id"] != first)["stream_id"]
    data = await (await mw.get(f"/player_api.php?{Q}&action=get_short_epg&stream_id={outside}")).json()
    assert data == {"epg_listings": []}
    inside = next(s for s in LIVE_STREAMS if s["category_id"] == first)["stream_id"]
    data = await (await mw.get(f"/player_api.php?{Q}&action=get_short_epg&stream_id={inside}")).json()
    assert data["epg_listings"]


async def test_filter_failure_sends_unfiltered(mw):
    await refresh(mw)
    await pick(mw, {"live": [LIVE_CATEGORIES[0]["category_id"]]})
    mw.boss.broken = True
    r = await mw.get(f"/player_api.php?{Q}&action=get_live_streams")
    assert r.status == 200
    assert "not json" in await r.text()


async def test_invalid_picks_rejected(mw):
    r = await mw.put(f"/middleware/v1/users/{USER}/picks", json={"live": "all"}, headers=auth())
    assert r.status == 422
    r = await mw.put("/middleware/v1/settings", json={"new_categories": "maybe"}, headers=auth())
    assert r.status == 422


async def test_rename_moves_picks(mw):
    await refresh(mw)
    await pick(mw, {"live": [LIVE_CATEGORIES[0]["category_id"]]})
    r = await mw.post(f"/middleware/v1/users/{USER}/rename", json={"username": "renamed"}, headers=auth())
    assert (await r.json())["types"]["live"]["mode"] == "selected"
    old = await (await mw.get(f"/middleware/v1/users/{USER}/picks", headers=auth())).json()
    assert old["types"]["live"]["mode"] == "all"


async def test_client_without_accept_encoding_gets_plain_body(mw):
    # IPTV Boss's desktop app (Java HttpClient) sends no Accept-Encoding and cannot read gzip.
    r = await mw.get("/boss.php/cloud/v1/status", skip_auto_headers={"Accept-Encoding"})
    assert "Content-Encoding" not in r.headers
    assert json.loads(await r.read())["currentRevision"] == 285
    assert mw.boss.last_cloud()["accept_encoding"] == "identity"


async def test_client_asking_for_gzip_still_gets_gzip(mw):
    r = await mw.get("/boss.php/cloud/v1/status", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("Content-Encoding") == "gzip"
    assert mw.boss.last_cloud()["accept_encoding"] == "gzip"


async def test_streamed_upload_keeps_content_length(mw):
    # IPTV Boss rejects cloud database uploads that arrive without Content-Length.
    body = b"x" * 500_000
    r = await mw.post("/boss.php/cloud/v1/revisions", data=body, headers={"Content-Type": "application/octet-stream"})
    assert r.status == 200
    seen = mw.boss.last_cloud()
    assert seen["content_length"] == str(len(body)) and seen["transfer_encoding"] is None
    assert seen["size"] == len(body)


async def test_forwarded_proto_setting(aiohttp_server, aiohttp_client, boss, tmp_path):
    server = await aiohttp_server(boss.app())
    env = {"MW_BOSS_URL": str(server.make_url("")).rstrip("/"), "MW_API_KEY": API_KEY}
    with pytest.raises(SystemExit):
        Config.from_env({**env, "MW_FORWARDED_PROTO": "ftp"})
    for proto, expected in (("", None), ("https", "https")):
        config = Config.from_env({**env, "MW_DATA_DIR": str(tmp_path / f"data-{proto}"), "MW_FORWARDED_PROTO": proto})
        client = await aiohttp_client(create_app(config))
        await client.get("/boss.php/cloud/v1/status")
        assert boss.last_cloud()["forwarded_proto"] == expected
        await client.get("/boss.php/cloud/v1/status", headers={"X-Forwarded-Proto": "http"})
        assert boss.last_cloud()["forwarded_proto"] == "http"     # the player's proxy wins
