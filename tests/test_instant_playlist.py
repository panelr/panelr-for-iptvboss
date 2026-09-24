"""A customer created between syncs still gets their playlist.

IPTV Boss writes each customer a playlist file during a sync and answers get.php with 403 until
that file exists. These tests cover the playlist being built from the channel list instead.
"""
import pytest

from conftest import LIVE_CATEGORIES, PASSWORD, USER, auth


def creds(**extra):
    q = {"username": USER, "password": PASSWORD, "type": "m3u_plus", "output": "ts"}
    q.update(extra)
    return q


async def test_playlist_is_built_when_iptvboss_has_no_file(mw):
    mw.boss.no_playlist_file = True
    r = await mw.get("/get.php", params=creds())
    assert r.status == 200
    text = await r.text()
    assert text.startswith("#EXTM3U")
    assert text.count("#EXTINF") > 0
    assert 'group-title="' in text
    assert "/live/%s/%s/" % (USER, PASSWORD) in text


async def test_built_playlist_matches_the_channel_list(mw):
    mw.boss.no_playlist_file = True
    listed = await (await mw.get("/player_api.php", params={"username": USER, "password": PASSWORD,
                                                            "action": "get_live_streams"})).json()
    text = await (await mw.get("/get.php", params=creds())).text()
    assert text.count("#EXTINF") == len(listed)
    for row in listed:
        assert str(row["stream_id"]) in text


async def test_output_m3u8_gives_m3u8_urls(mw):
    mw.boss.no_playlist_file = True
    text = await (await mw.get("/get.php", params=creds(output="m3u8"))).text()
    assert ".m3u8" in text and ".ts\n" not in text


async def test_built_playlist_respects_a_customers_picks(mw):
    mw.boss.no_playlist_file = True
    full = await (await mw.get("/get.php", params=creds())).text()
    keep = LIVE_CATEGORIES[0]
    r = await mw.post("/middleware/v1/categories/refresh",
                      json={"username": USER, "password": PASSWORD}, headers=auth())
    assert r.status == 200, await r.text()
    r = await mw.put(f"/middleware/v1/users/{USER}/picks",
                     json={"live": [keep["category_id"]], "vod": [], "series": []}, headers=auth())
    assert r.status == 200, await r.text()
    trimmed = await (await mw.get("/get.php", params=creds())).text()
    assert 0 < trimmed.count("#EXTINF") < full.count("#EXTINF")
    assert 'group-title="%s"' % keep["category_name"] in trimmed


async def test_iptvboss_answer_is_used_when_it_has_one(mw):
    r = await mw.get("/get.php", params=creds())
    assert r.status == 200
    text = await r.text()
    assert text.startswith("#EXTM3U")
    # the fixture playlist, not one we built: its urls are the provider's, not this server's
    assert "/live/%s/%s/" % (USER, PASSWORD) not in text


async def test_refusal_stands_when_the_channel_list_is_unusable(mw):
    mw.boss.no_playlist_file = True
    mw.boss.broken = True
    r = await mw.get("/get.php", params=creds())
    assert r.status == 403


async def test_wrong_password_is_still_refused(mw):
    mw.boss.no_playlist_file = True
    r = await mw.get("/get.php", params={"username": USER, "password": "wrong", "type": "m3u_plus"})
    assert r.status in (401, 403)
