"""IPTV Boss refuses everything for about 80 seconds while it installs a revision (each time the
desktop closes). Players must not see that: their requests wait, streams are answered from the
last redirect seen, and only a panel's own API calls get the refusal straight away."""
import dataclasses
import time

from conftest import PASSWORD, USER


def creds(**extra):
    q = {"username": USER, "password": PASSWORD}
    q.update(extra)
    return q


def set_wait(mw, seconds):
    m = mw.app["middleware"]
    m.config = dataclasses.replace(m.config, busy_wait_seconds=seconds)


async def test_player_list_waits_through_a_busy_spell(mw):
    set_wait(mw, 5)
    mw.boss.busy_until = time.monotonic() + 1.0
    t = time.monotonic()
    r = await mw.get("/player_api.php", params=creds(action="get_live_categories"))
    assert r.status == 200
    assert time.monotonic() - t >= 0.9
    assert mw.boss.busy_hits >= 1
    assert isinstance(await r.json(), list)


async def test_playlist_waits_instead_of_building_or_failing(mw):
    set_wait(mw, 5)
    mw.boss.busy_until = time.monotonic() + 1.0
    r = await mw.get("/get.php", params=creds(type="m3u_plus", output="ts"))
    assert r.status == 200
    assert (await r.text()).startswith("#EXTM3U")


async def test_stream_is_served_from_the_remembered_redirect_at_once(mw):
    set_wait(mw, 5)
    first = await mw.get("/live/%s/%s/40870002.ts" % (USER, PASSWORD), allow_redirects=False)
    assert first.status == 302
    mw.boss.busy_until = time.monotonic() + 3.0
    t = time.monotonic()
    r = await mw.get("/live/%s/%s/40870002.ts" % (USER, PASSWORD), allow_redirects=False)
    assert r.status == 302
    assert r.headers["Location"] == first.headers["Location"]
    assert time.monotonic() - t < 0.5


async def test_unseen_stream_waits_for_iptvboss(mw):
    set_wait(mw, 5)
    mw.boss.busy_until = time.monotonic() + 1.0
    t = time.monotonic()
    r = await mw.get("/live/%s/%s/40880002.ts" % (USER, PASSWORD), allow_redirects=False)
    assert r.status == 302
    assert time.monotonic() - t >= 0.9


async def test_panel_api_calls_never_wait(mw):
    set_wait(mw, 5)
    mw.boss.busy_until = time.monotonic() + 2.0
    t = time.monotonic()
    r = await mw.get("/api/v1/users")
    assert r.status == 503
    assert time.monotonic() - t < 0.5


async def test_refusal_is_passed_on_when_the_wait_runs_out(mw):
    set_wait(mw, 1)
    mw.boss.busy_until = time.monotonic() + 3.0
    t = time.monotonic()
    r = await mw.get("/player_api.php", params=creds(action="get_live_categories"))
    assert r.status == 503
    assert 0.9 <= time.monotonic() - t < 2.5


async def test_wait_switched_off_behaves_as_before(mw):
    set_wait(mw, 0)
    mw.boss.busy_until = time.monotonic() + 1.0
    r = await mw.get("/player_api.php", params=creds(action="get_live_categories"))
    assert r.status == 503
