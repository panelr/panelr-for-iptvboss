"""The 1.1.0 fixes, each proven against the fake IPTV Boss."""
import gzip
import json
import xml.etree.ElementTree as ET

from conftest import (API_KEY, BIG_SHIFT, GUIDE, LIVE_CATEGORIES, LIVE_STREAMS, PASSWORD, USER, auth,
                      extra_category)
from middleware import idmap
from middleware.app import create_app
from middleware.config import Config

Q = f"username={USER}&password={PASSWORD}"


async def refresh(client, **extra):
    r = await client.post("/middleware/v1/categories/refresh", json={"username": USER, "password": PASSWORD, **extra},
                          headers=auth())
    assert r.status == 200, await r.text()
    return await r.json()


async def pick(client, body):
    r = await client.put(f"/middleware/v1/users/{USER}/picks", json=body, headers=auth())
    assert r.status == 200, await r.text()
    return await r.json()


# ---- 32-bit ids ---------------------------------------------------------------

async def test_oversized_ids_are_compacted_for_players_and_expanded_on_the_way_back(mw):
    mw.boss.big_ids = True
    real = LIVE_STREAMS[0]["stream_id"] + BIG_SHIFT
    streams = await (await mw.get(f"/player_api.php?{Q}&action=get_live_streams")).json()
    assert all(int(s["stream_id"]) <= idmap.LIMIT for s in streams)
    small = streams[0]["stream_id"]
    assert idmap.expand(small) == real
    # a player asks about the compacted id: IPTV Boss is asked for the real one
    await mw.get(f"/player_api.php?{Q}&action=get_short_epg&stream_id={small}")
    assert mw.boss.requests[-1][1]["stream_id"] == str(real)
    r = await mw.post("/player_api.php", data={"username": USER, "password": PASSWORD, "action": "get_short_epg",
                                               "stream_id": str(small)})
    assert r.status == 200 and mw.boss.requests[-1][1]["stream_id"] == str(real)
    # and a stream URL with the compacted id reaches IPTV Boss with the real one
    r = await mw.get(f"/live/{USER}/{PASSWORD}/{small}.ts", allow_redirects=False)
    assert r.status == 302 and r.headers["Location"].endswith(f"/{real}.ts")
    # series episode ids are never touched
    r = await mw.get(f"/series/{USER}/{PASSWORD}/{small}.mkv", allow_redirects=False)
    assert r.headers["Location"].endswith(f"/{small}.mkv")


async def test_small_ids_are_byte_identical(mw):
    body = await (await mw.get(f"/player_api.php?{Q}&action=get_live_streams")).read()
    assert json.loads(body) == LIVE_STREAMS


async def test_compacted_ids_survive_picks_and_enforcement(mw):
    mw.boss.big_ids = True
    await refresh(mw)
    first = LIVE_CATEGORIES[0]["category_id"]
    await pick(mw, {"live": [first]})
    streams = await (await mw.get(f"/player_api.php?{Q}&action=get_live_streams")).json()
    assert streams and all(s["category_id"] == first for s in streams)
    inside = streams[0]["stream_id"]
    outside_real = next(s for s in LIVE_STREAMS if s["category_id"] != first)["stream_id"] + BIG_SHIFT
    outside = idmap.compact(outside_real)
    assert (await mw.get(f"/live/{USER}/{PASSWORD}/{inside}.ts", allow_redirects=False)).status == 302
    assert (await mw.get(f"/live/{USER}/{PASSWORD}/{outside}.ts", allow_redirects=False)).status == 403


# ---- redirects from IPTV Boss pass through ----------------------------------------

async def test_info_redirects_pass_through_with_and_without_picks(mw):
    mw.boss.info_redirects = True
    r = await mw.get(f"/player_api.php?{Q}&action=get_vod_info&vod_id=1", allow_redirects=False)
    assert r.status == 302 and r.headers["Location"] == "http://provider.example/info"
    await refresh(mw)
    await pick(mw, {"vod": [json.loads(open(__file__.replace("test_fixes_1_1.py", "fixtures/get_vod_categories.json")).read())[0]["category_id"]]})
    vod = await (await mw.get(f"/player_api.php?{Q}&action=get_vod_streams")).json()
    r = await mw.get(f"/player_api.php?{Q}&action=get_vod_info&vod_id={vod[0]['stream_id']}", allow_redirects=False)
    assert r.status == 302 and r.headers["Location"] == "http://provider.example/info"


# ---- guide guards ----------------------------------------------------------------

async def test_an_empty_or_cut_guide_never_replaces_a_good_one(mw):
    await refresh(mw)
    await pick(mw, {"live": [LIVE_CATEGORIES[0]["category_id"]]})
    good = await (await mw.get(f"/xmltv.php?{Q}", headers={"Accept-Encoding": "identity"})).read()
    assert ET.fromstring(good).findall("channel")
    mw.boss.guide_body = b""                                  # IPTV Boss mid-rewrite
    again = await (await mw.get(f"/xmltv.php?{Q}", headers={"Accept-Encoding": "identity"})).read()
    assert again == good
    mw.boss.guide_body = GUIDE[: len(GUIDE) // 2]            # a download that stopped half way
    again = await (await mw.get(f"/xmltv.php?{Q}", headers={"Accept-Encoding": "identity"})).read()
    assert again == good


async def test_an_empty_guide_with_no_index_yet_is_passed_through(mw):
    await refresh(mw)
    await pick(mw, {"live": [LIVE_CATEGORIES[0]["category_id"]]})
    mw.boss.guide_body = b""
    r = await mw.get(f"/xmltv.php?{Q}", headers={"Accept-Encoding": "identity"})
    assert r.status == 200 and await r.read() == b""        # IPTV Boss's own answer, not a cached one


# ---- catalogue writes and per-layout catalogue ---------------------------------------

async def test_unchanged_category_lists_are_not_rewritten(mw):
    store = mw.server.app["middleware"].store
    await refresh(mw)
    writes = store.writes
    for _ in range(3):
        await mw.get(f"/player_api.php?{Q}&action=get_live_categories")
    assert store.writes == writes
    mw.boss.extra_live_category = extra_category()
    await mw.get(f"/player_api.php?{Q}&action=get_live_categories")
    assert store.writes == writes + 1


async def test_same_category_id_on_two_layouts_stays_apart(mw):
    await refresh(mw)                                         # layout 2, from the fixtures' ids
    mw.boss.layout_shift = 1                                  # the same lists, now ending in 3: layout 3
    mw.boss.requests.clear()
    await refresh(mw, layout=3)
    both = await (await mw.get("/middleware/v1/categories?type=live", headers=auth())).json()
    assert {c["layout"] for c in both["live"]} == {2, 3}
    only2 = await (await mw.get("/middleware/v1/categories?type=live&layout=2", headers=auth())).json()
    assert {c["layout"] for c in only2["live"]} == {2} and len(only2["live"]) == len(LIVE_CATEGORIES)
    only3 = await (await mw.get("/middleware/v1/categories?type=live&layout=3", headers=auth())).json()
    assert {c["layout"] for c in only3["live"]} == {3}


async def test_layout_zero_is_a_real_layout(mw):
    mw.boss.layout_shift = -2                                 # ids ending in 0: IPTV Boss can hand out layout 0
    body = await refresh(mw, layout=0)
    assert body["layout"] == 0
    rows = await (await mw.get("/middleware/v1/categories?type=live&layout=0", headers=auth())).json()
    assert rows["live"] and all(c["layout"] == 0 for c in rows["live"])


async def test_picks_for_a_line_whose_layout_is_unknown_hide_the_rest(mw):
    await refresh(mw)
    r = await mw.put("/middleware/v1/users/newline/picks", json={"live": ["1"]}, headers=auth())
    live = (await r.json())["types"]["live"]
    assert live["mode"] == "selected" and live["included"] == ["1"] and live["excluded"] == []
    assert live["effective_new_categories"] == "hide"
    r = await mw.put("/middleware/v1/users/newline2/picks", json={"live": ["1"], "layout": 2}, headers=auth())
    live = (await r.json())["types"]["live"]
    assert live["excluded"]                                   # the panel said layout 2, so the rest is excluded


# ---- grace days ------------------------------------------------------------------

async def test_a_missing_category_is_kept_unless_grace_days_say_otherwise(mw):
    mw.boss.extra_live_category = extra_category()
    await refresh(mw)
    mw.boss.extra_live_category = None
    mw.server.app["middleware"]._recorded.clear()
    await refresh(mw)
    cats = await (await mw.get("/middleware/v1/categories?type=live", headers=auth())).json()
    assert any(c["id"] == "9999" and c["removed_at"] is None for c in cats["live"])


async def test_grace_days_mark_a_category_removed_after_the_period(aiohttp_server, aiohttp_client, boss, tmp_path):
    server = await aiohttp_server(boss.app())
    config = Config.from_env({"MW_BOSS_URL": str(server.make_url("")).rstrip("/"), "MW_API_KEY": API_KEY,
                              "MW_DATA_DIR": str(tmp_path / "data"), "MW_GUIDE_RECHECK_SECONDS": "0",
                              "MW_CATEGORY_GRACE_DAYS": "1"})
    client = await aiohttp_client(create_app(config))
    boss.extra_live_category = extra_category()
    await refresh(client)
    boss.extra_live_category = None
    mw = client.server.app["middleware"]
    mw._recorded.clear()
    await refresh(client)
    cats = await (await client.get("/middleware/v1/categories?type=live&include_removed=1", headers=auth())).json()
    assert next(c for c in cats["live"] if c["id"] == "9999")["removed_at"] is None       # missing, within grace
    mw.store._db.execute("UPDATE categories SET last_seen = last_seen - 200000 WHERE id = '9999'")
    mw._recorded.clear()
    await refresh(client)
    cats = await (await client.get("/middleware/v1/categories?type=live&include_removed=1", headers=auth())).json()
    assert next(c for c in cats["live"] if c["id"] == "9999")["removed_at"]


# ---- full lists without picks --------------------------------------------------------

async def test_full_lists_are_prepared_once_and_gzipped_only_when_asked(mw):
    plain = await mw.get(f"/player_api.php?{Q}&action=get_live_streams", headers={"Accept-Encoding": "identity"})
    assert "Content-Encoding" not in plain.headers and plain.headers["Vary"] == "Accept-Encoding"
    zipped = await mw.get(f"/player_api.php?{Q}&action=get_live_streams", headers={"Accept-Encoding": "gzip"})
    raw = await zipped.read()
    if raw[:2] == b"\x1f\x8b":                               # the test client may have inflated it already
        raw = gzip.decompress(raw)
    assert zipped.headers.get("Content-Encoding") == "gzip"
    assert json.loads(raw) == await plain.json()


async def test_list_cache_stays_inside_its_budget(aiohttp_server, aiohttp_client, boss, tmp_path):
    server = await aiohttp_server(boss.app())
    config = Config.from_env({"MW_BOSS_URL": str(server.make_url("")).rstrip("/"), "MW_API_KEY": API_KEY,
                              "MW_DATA_DIR": str(tmp_path / "data"), "MW_LIST_CACHE_MB": "1"})
    client = await aiohttp_client(create_app(config))
    mw = client.server.app["middleware"]
    mw.config = mw.config.__class__(**{**mw.config.__dict__})
    import middleware.app as appmod
    old = appmod.LIST_CACHE_MIN
    appmod.LIST_CACHE_MIN = 1                                 # every list is big enough to cache in this test
    try:
        for shift in range(0, 12):                            # a dozen distinct answers of ~350 KB each
            boss.layout_shift = shift * 10000
            await client.get(f"/player_api.php?{Q}&action=get_live_streams")
        one = sum(len(x) for x in next(iter(mw._lists.values())))
        assert len(mw._lists) == 1 and mw._lists_bytes == one   # each answer is bigger than the whole budget: one stays
        assert len(mw._recorded) <= 512
    finally:
        appmod.LIST_CACHE_MIN = old
