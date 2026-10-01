"""Per-channel guide answers (get_short_epg, get_simple_data_table) taken from the current guide.

IPTV Boss answers these from a programmes database it only rebuilds while Universal EPG output is
on; with that off its answers go stale. The middleware answers them from the guide instead.
"""
import base64
import json
import time

from conftest import LIVE_CATEGORIES, LIVE_STREAMS, PASSWORD, USER, auth, load
from middleware import guide
from middleware.app import create_app
from middleware.config import Config

Q = f"username={USER}&password={PASSWORD}"
BOSS_SHORT = json.loads(load("get_short_epg.json"))
BOSS_TABLE = json.loads(load("get_simple_data_table.json"))
CHANNEL = next(s for s in LIVE_STREAMS if s.get("epg_channel_id"))
EPG_ID = CHANNEL["epg_channel_id"]
STREAM = CHANNEL["stream_id"]


def xmltv(t):
    return time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(t))


def current_guide(channel_ids, hours_back=3, hours_ahead=6, title="Show"):
    """A guide with one-hour programmes around now for these channels."""
    from xml.sax.saxutils import escape
    now = int(time.time()) // 3600 * 3600
    parts = ['<?xml version="1.0" encoding="UTF-8"?>\n<tv source-info-name="IPTVBoss">\n']
    for cid in channel_ids:
        parts.append(f'  <channel id="{escape(cid, {chr(34): "&quot;"})}"><display-name>x</display-name></channel>\n')
    for h in range(-hours_back, hours_ahead):
        for cid in channel_ids:
            e = escape(cid, {'"': "&quot;"})
            parts.append(f'  <programme start="{xmltv(now + h * 3600)}" stop="{xmltv(now + (h + 1) * 3600)}" '
                         f'channel="{e}"><title lang="en">{title} {h} &amp; more</title>'
                         f'<desc lang="en">About {h}</desc></programme>\n')
    parts.append("</tv>\n")
    return "".join(parts).encode("utf-8")


async def settle(client):
    """Let the background guide refresh finish."""
    mw = client.server.app["middleware"]
    for task in list(mw._guide_refresh.values()):
        await task


async def warm(client, action="get_short_epg"):
    """First ask (falls back to IPTV Boss while the guide loads), then wait for the guide."""
    await client.get(f"/player_api.php?{Q}&action={action}&stream_id={STREAM}")
    await settle(client)


async def short(client, extra=""):
    r = await client.get(f"/player_api.php?{Q}&action=get_short_epg&stream_id={STREAM}{extra}")
    assert r.status == 200
    return await r.json()


def b64(s):
    return base64.b64decode(s).decode("utf-8")


async def test_short_epg_comes_from_the_guide_in_boss_format(mw):
    mw.boss.guide_body = current_guide([EPG_ID])
    await warm(mw)
    data = await short(mw)
    rows = data["epg_listings"]
    assert len(rows) == 4                                   # IPTV Boss's default
    assert set(rows[0]) == set(BOSS_SHORT["epg_listings"][0])
    assert b64(rows[0]["title"]) == "Show 0 & more"         # the programme on now comes first
    assert b64(rows[0]["description"]) == "About 0"
    assert rows[0]["channel_id"] == EPG_ID and rows[0]["lang"] == "en"
    now = time.time()
    assert rows[0]["start_timestamp"] <= now < rows[0]["stop_timestamp"]
    assert rows[0]["start"] == time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(rows[0]["start_timestamp"]))
    assert rows[0]["stop"] == rows[0]["end"]


async def test_short_epg_honours_limit(mw):
    mw.boss.guide_body = current_guide([EPG_ID])
    await warm(mw)
    assert len((await short(mw, "&limit=2"))["epg_listings"]) == 2
    assert len((await short(mw, "&limit=50"))["epg_listings"]) == 6     # everything from now on
    assert len((await short(mw, "&limit=junk"))["epg_listings"]) == 4


async def test_simple_data_table_lists_the_whole_guide_with_now_playing(mw):
    mw.boss.guide_body = current_guide([EPG_ID])
    await warm(mw, "get_simple_data_table")
    r = await mw.get(f"/player_api.php?{Q}&action=get_simple_data_table&stream_id={STREAM}")
    rows = (await r.json())["epg_listings"]
    assert len(rows) == 9
    assert set(rows[0]) == set(BOSS_TABLE["epg_listings"][0])
    assert [b64(x["title"]) for x in rows if x["now_playing"]] == ["Show 0 & more"]
    assert all(x["has_archive"] == 0 for x in rows)


async def test_first_request_falls_back_to_boss_while_the_guide_loads(mw):
    mw.boss.guide_body = current_guide([EPG_ID])
    data = await short(mw)
    assert data == BOSS_SHORT                               # no guide yet: IPTV Boss's answer, no waiting
    await settle(mw)
    assert b64((await short(mw))["epg_listings"][0]["title"]) == "Show 0 & more"


async def test_channel_missing_from_the_guide_is_empty_not_boss(mw):
    """IPTV Boss's own answer is frozen while Universal EPG is off and can list games that are not on."""
    mw.boss.guide_body = current_guide(["Somebody.else"])
    await warm(mw)
    assert await short(mw) == {"epg_listings": []}


async def test_guide_with_nothing_current_is_empty_not_boss(mw):
    mw.boss.guide_body = current_guide([EPG_ID], hours_back=5, hours_ahead=-2)    # all over
    await warm(mw)
    assert await short(mw) == {"epg_listings": []}
    r = await mw.get(f"/player_api.php?{Q}&action=get_simple_data_table&stream_id={STREAM}")
    rows = (await r.json())["epg_listings"]
    assert rows and not any(x["now_playing"] for x in rows)    # the table still lists what the guide has


async def test_stream_without_a_guide_id_is_empty(mw):
    no_epg = next((s for s in LIVE_STREAMS if not s.get("epg_channel_id")), None)
    mw.boss.guide_body = current_guide([EPG_ID])
    await warm(mw)
    if no_epg is None:
        return
    r = await mw.get(f"/player_api.php?{Q}&action=get_short_epg&stream_id={no_epg['stream_id']}")
    assert await r.json() == {"epg_listings": []}


async def test_every_listing_is_marked_epg_id_zero(mw):
    """Panelr tells the middleware's listings from IPTV Boss's by epg_id "0". Never change it."""
    mw.boss.guide_body = current_guide([EPG_ID])
    await warm(mw)
    rows = (await short(mw, "&limit=50"))["epg_listings"]
    r = await mw.get(f"/player_api.php?{Q}&action=get_simple_data_table&stream_id={STREAM}")
    rows += (await r.json())["epg_listings"]
    assert rows and {x["epg_id"] for x in rows} == {"0"}
    assert all(x["epg_id"] != "0" for x in BOSS_SHORT["epg_listings"])


async def test_refresh_call_loads_a_new_guide_at_once(mw):
    """After a sync the server calls POST /middleware/v1/guides/refresh; the new guide is used straight away."""
    mw.boss.guide_body = current_guide([EPG_ID], title="Old")
    await warm(mw)
    mw.boss.guide_body = current_guide([EPG_ID], title="New")
    assert b64((await short(mw))["epg_listings"][0]["title"]).startswith("Old")   # within the recheck time
    r = await mw.post("/middleware/v1/guides/refresh", headers=auth())
    assert r.status == 200
    layout = int(STREAM) % 10000
    assert (await r.json())["refreshing"] == [layout]
    await settle(mw)
    assert b64((await short(mw))["epg_listings"][0]["title"]).startswith("New")


async def test_refresh_call_needs_the_key(mw):
    r = await mw.post("/middleware/v1/guides/refresh")
    assert r.status == 401


async def test_picks_still_hide_channels_outside_them(mw):
    mw.boss.guide_body = current_guide([s["epg_channel_id"] for s in LIVE_STREAMS if s.get("epg_channel_id")])
    r = await mw.post("/middleware/v1/categories/refresh", json={"username": USER, "password": PASSWORD},
                      headers=auth())
    assert r.status == 200
    first = CHANNEL["category_id"]
    r = await mw.put(f"/middleware/v1/users/{USER}/picks", json={"live": [first]}, headers=auth())
    assert r.status == 200
    await warm(mw)
    outside = next(s for s in LIVE_STREAMS if s["category_id"] != first and s.get("epg_channel_id"))
    r = await mw.get(f"/player_api.php?{Q}&action=get_short_epg&stream_id={outside['stream_id']}")
    assert await r.json() == {"epg_listings": []}
    assert b64((await short(mw))["epg_listings"][0]["title"]) == "Show 0 & more"


async def test_newer_guide_is_picked_up_after_the_recheck(mw):
    mw.boss.guide_body = current_guide([EPG_ID], title="Old")
    await warm(mw)
    assert b64((await short(mw))["epg_listings"][0]["title"]).startswith("Old")
    mw.boss.guide_body = current_guide([EPG_ID], title="New")
    mw.server.app["middleware"].guides._checked.clear()     # as if MW_EPG_RECHECK_SECONDS had passed
    await short(mw)                                         # still answers at once from the current guide
    await settle(mw)
    assert b64((await short(mw))["epg_listings"][0]["title"]).startswith("New")


async def test_switched_off_leaves_it_to_boss(aiohttp_server, aiohttp_client, boss, tmp_path):
    server = await aiohttp_server(boss.app())
    config = Config.from_env({"MW_BOSS_URL": str(server.make_url("")).rstrip("/"), "MW_API_KEY": "k" * 30,
                              "MW_DATA_DIR": str(tmp_path / "d"), "MW_EPG_FROM_GUIDE": "false"})
    client = await aiohttp_client(create_app(config))
    boss.guide_body = current_guide([EPG_ID])
    for _ in range(2):
        r = await client.get(f"/player_api.php?{Q}&action=get_short_epg&stream_id={STREAM}")
        assert await r.json() == BOSS_SHORT
    assert not client.server.app["middleware"]._guide_refresh


# ---- parsing ----------------------------------------------------------------------

def test_xmltv_times():
    assert guide.xmltv_time("20261001174600 +0000") == 1790876760
    assert guide.xmltv_time("20261001134600 -0400") == 1790876760
    assert guide.xmltv_time("20261001194600 +0200") == 1790876760
    assert guide.xmltv_time("202610011746 +0000") == 1790876760
    assert guide.xmltv_time("20261001174600") == 1790876760
    assert guide.xmltv_time("garbage") is None


def test_a_stray_entity_does_not_lose_the_channel():
    raw = (b'<programme start="20261001170000 +0000" stop="20261001180000 +0000" channel="A&amp;E">'
           b'<title lang="en">Caf&eacute; &amp; Bar</title></programme>\n'
           b'<programme start="20261001180000 +0000" stop="20261001190000 +0000" channel="A&amp;E">'
           b'<title>Next</title></programme>\n')
    rows = guide.parse_programmes(raw)
    assert [r["title"] for r in rows] == ["Caf&eacute; & Bar", "Next"]
    assert rows[1]["lang"] is None and rows[0]["desc"] == ""


async def test_unchanged_guide_is_not_split_again(mw):
    mw.boss.guide_body = current_guide([EPG_ID])
    store = mw.server.app["middleware"].guides
    installs = []
    real_install = store.install
    store.install = lambda *a: installs.append(a) or real_install(*a)
    await warm(mw)
    for _ in range(3):
        store._checked.clear()
        await short(mw)
        await settle(mw)
    assert len(installs) == 1


async def test_layout_zero_is_answered_too(mw):
    """Layout 0 is a real layout (WhisperTV Max): its ids end in 0000."""
    mw.boss.layout_shift = -(STREAM % 10000)
    stream = STREAM - STREAM % 10000
    mw.boss.guide_body = current_guide([EPG_ID])
    await mw.get(f"/player_api.php?{Q}&action=get_short_epg&stream_id={stream}")
    await settle(mw)
    r = await mw.get(f"/player_api.php?{Q}&action=get_short_epg&stream_id={stream}")
    assert b64((await r.json())["epg_listings"][0]["title"]) == "Show 0 & more"
