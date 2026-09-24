# Panelr category picks for IPTV Boss
# panelr.app

IPTV Boss serves every customer on a layout the same lineup. This add-on lets
Panelr customers on IPTV Boss choose their own categories (live, movies and series
separately), the same way PlaylistLabs customers already can.

It is installed on your IPTV Boss XC server, not on your Panelr site. It sits
between customers' players and IPTV Boss, keeps each customer's picks, and trims
what their player receives. Panelr tells it what each customer picked. Nothing in
IPTV Boss changes, and it works with any IPTV Boss XC server.

## A customer added between syncs

IPTV Boss writes each customer their own playlist file when it syncs, and refuses `get.php`
until that file exists. A customer created by API in between is therefore refused until the
next sync, which may be hours away.

The add-on covers that gap. When IPTV Boss refuses a playlist, the add-on builds it from the
channel list IPTV Boss already holds for that customer and serves it. Nothing to switch on, and
nothing changes for a customer whose file does exist: IPTV Boss's own playlist is used, byte for
byte. A built playlist carries the same channels in the same order, honours `output=m3u8`, and
has the customer's category picks applied like any other.

Streams in a built playlist point at this server, which passes the player on to the provider,
exactly as `player_api.php` already does.

If the channel list cannot be read for any reason, IPTV Boss's refusal is passed on unchanged.

## Connect it to Panelr

1. Install it on your IPTV Boss server (below) and note its `MW_API_KEY`.
2. In Panelr, open the IPTV Boss service's settings.
3. Paste the key into **Category picks key**, and leave **Let customers pick their
   own categories** on.
4. Save, then run the service's editor sync. Panelr loads every live, movie and
   series category as bouquets.
5. Turn on bouquets for the service, and "let customers choose" if customers should
   pick them themselves.

From then on, IPTV Boss lines take categories everywhere Panelr offers bouquets:
the member area, the Telegram and Discord bots, the Panelr API, and the admin line
and work order pages.

What Panelr does with the add-on:

| In Panelr | Add-on call |
|---|---|
| Test connection | `GET /middleware/v1/settings` |
| Editor sync | `POST /middleware/v1/categories/refresh` with one active line's login, then `GET /middleware/v1/categories` |
| New line with bouquets | `PUT /middleware/v1/users/{username}/picks` once IPTV Boss has the user |
| Customer or admin changes bouquets | `PUT .../picks`, or `DELETE .../picks` when nothing is picked (everything) |
| Line sync | `GET .../picks`, saved back to the line's bouquets |
| Line deleted | `DELETE .../picks` |

Panelr reaches the add-on at the root of the service's **Server address**, so
customers, IPTV Boss's own API and the add-on share one address.

Turning **Let customers pick their own categories** off puts every customer back
on their whole layout. The key stays saved.

If the add-on is unreachable, Panelr still creates the line and logs a warning. The
customer gets their whole layout until the picks are saved again.

Other panels can use the same API below.

## How it works

```
player  ->  your web server / reverse proxy  ->  middleware :8002  ->  IPTV Boss XC server :8001
```

- IPTV Boss still checks logins and builds the playlist and guide.
- Customers without picks get IPTV Boss's answers untouched, apart from ids past 32 bits,
  which are compacted for players that store ids as an int (TiviMate) and expanded back
  on the way in.
- For customers with picks, the middleware trims:
  - category lists and channel, movie and series lists (`player_api.php`)
  - the M3U playlist (`get.php`)
  - the guide (`xmltv.php`), which contains only the picked channels
  - `panel_api.php`
  - now and next listings for channels outside their picks
- Live channels and movies outside a customer's picks are refused (403). Turn this
  off with `MW_ENFORCE_STREAMS=false`.
- Series episodes always play, because episode links can't be tied to a category.
- If filtering ever fails, the customer gets IPTV Boss's unfiltered answer. The
  middleware can show someone too much, but never breaks their player.

### Categories that appear later

Each customer's picks per type are three lists: included, excluded, and everything
else. A category added after they chose falls into "everything else" and follows a
rule:

- `show`: new categories appear automatically.
- `hide`: new categories stay hidden until the customer adds them.

The server-wide default is `MW_NEW_CATEGORIES`, which a panel can change later.
Each customer can override it. `GET .../picks` lists a customer's undecided
categories, so a panel can tell them something new is available.

Category ids are IPTV Boss group ids. Renaming or reordering a group, or rebuilding
the lineup, keeps them. They repeat across layouts, so the catalogue is kept per layout.
A group that disappears is kept (sports groups empty out between seasons) unless
`MW_CATEGORY_GRACE_DAYS` is set, in which case it is marked removed after that many days.

### The guide

After IPTV Boss rebuilds its guide, the middleware splits it by channel once
(about 1 second for a 120 MB guide). Each customer's guide is assembled from those
pieces, so every customer can pick a different combination with no extra storage.
Timings on a 120 MB, 7,000-channel guide:

| Picks | Channels | Gzip size | Assembly time |
|---|---|---|---|
| 5 categories | 890 | 1.5 MB | 0.1 s |
| 20 categories | 2,983 | 6 MB | 0.5 s |
| All 74 | 6,198 | 12 MB | 1.0 s |

## Install with Docker

1. Copy `docker/compose.example.yml` next to your IPTV Boss compose file, or merge
   the service into it.
2. Set `MW_BOSS_URL` to where the middleware reaches IPTV Boss's XC server.
3. Set `MW_API_KEY` to a long random value, for example `openssl rand -hex 32`.
4. Start it: `docker compose up -d --build boss-middleware`.
5. Point your reverse proxy at port `8002` instead of IPTV Boss's port. Customers'
   player addresses don't change.
6. Check it: `curl http://127.0.0.1:8002/middleware/v1/health`.

The `/data` volume holds the picks database and the guide split. Back it up with
IPTV Boss's data.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `MW_BOSS_URL` | `http://127.0.0.1:8001` | IPTV Boss XC server address |
| `MW_API_KEY` | required | Key the panel sends in `X-Middleware-Key`, at least 24 characters |
| `MW_NEW_CATEGORIES` | `show` | Starting rule for undecided categories: `show` or `hide` |
| `MW_ENFORCE_STREAMS` | `true` | Refuse live channels and movies outside a customer's picks |
| `MW_LISTEN_HOST` / `MW_LISTEN_PORT` | `0.0.0.0` / `8002` | Where the middleware listens |
| `MW_DATA_DIR` | `/data` | Database and guide files |
| `MW_GUIDE_RECHECK_SECONDS` | `60` | How often a guide request checks IPTV Boss for a newer guide |
| `MW_REQUEST_TIMEOUT` | `300` | Seconds to wait on IPTV Boss |
| `MW_FORWARDED_PROTO` | empty | `https` when IPTV Boss runs with `BEHIND_HTTPS_PROXY=true` and no proxy sets `X-Forwarded-Proto` |
| `MW_CATEGORY_GRACE_DAYS` | `0` | Days a category may be missing before it is marked removed; `0` never marks one removed |
| `MW_LIST_CACHE_MB` | `64` | Memory for prepared full lists (customers without picks); `0` turns the cache off |
| `MW_FORWARDED_PROTO` | empty | `X-Forwarded-Proto` sent to IPTV Boss when the player's proxy doesn't send one. Set `https` if IPTV Boss runs with `BEHIND_HTTPS_PROXY=true`, otherwise it answers the middleware's own calls with 426 |

## Panel API

Every request except `health` needs the header `X-Middleware-Key: <MW_API_KEY>`.
Everything under `/api/v1/` is passed to IPTV Boss unchanged, so a panel can use one
address for both IPTV Boss's own API and this one.

### `GET /middleware/v1/health`

`{"status": "ok", "version": "1.1.0", "boss": "ok"}`

### `POST /middleware/v1/categories/refresh`

Loads every category from IPTV Boss using one customer's login. Run it once after
installing, and whenever you want the list current before any player has connected.

Body: `{"username": "...", "password": "...", "layout": 2}`. Send the service's layout id:
category ids repeat across layouts, and this is what files the list under the right one.
Answer: `{"categories": {"live": 74, "vod": 22, "series": 13}, "layout": 2}`

The middleware also learns categories from players' own requests.

### `GET /middleware/v1/categories`

Optional query: `type=live|vod|series`, `layout=2`, `include_removed=1`.

```json
{"live": [{"id": "1", "name": "US| Ambience", "position": 0, "layout": 2, "first_seen": 1789300000, "removed_at": null}],
 "vod": [], "series": []}
```

### `GET /middleware/v1/users/{username}/picks`

```json
{"username": "customer1", "default_new_categories": "show",
 "types": {"live": {"mode": "selected", "included": ["1", "4"], "excluded": ["2", "3"],
                    "new_categories": "default", "effective_new_categories": "show",
                    "saved_at": 1789300000,
                    "undecided": [{"id": "9", "name": "NEW SPORTS", "first_seen": 1789400000}]},
           "vod": {"mode": "all", "included": [], "excluded": [], "new_categories": "default",
                   "effective_new_categories": "show", "saved_at": 0, "undecided": []},
           "series": {"...": "..."}}}
```

### `PUT /middleware/v1/users/{username}/picks`

Send only the types you're changing.

```json
{"live": ["1", "4"], "vod": null, "new_categories": "hide"}
```

- A list means only these categories. Every category known right now that isn't
  in the list is saved as excluded; anything added later follows the rule.
- `null` means everything for that type.
- `new_categories` is optional: `default` follows the server setting, or `show` / `hide`.
- `layout` is optional but recommended: the layout the line is on, so "everything else" means that
  layout's categories and not another's. Without it, for a line the middleware has not seen play yet,
  the categories not listed are hidden rather than excluded.
- For full control, send an object instead of a list:
  `{"mode": "selected", "included": [...], "excluded": [...], "new_categories": "show"}`.

Answer: the same shape as `GET`.

### `DELETE /middleware/v1/users/{username}/picks`

Back to everything for every type.

### `POST /middleware/v1/users/{username}/rename`

Body: `{"username": "new-name"}`. Moves picks when a customer's IPTV Boss user name changes.

### `GET` / `PUT /middleware/v1/settings`

`{"new_categories": "show", "enforce_streams": true}`. Only `new_categories` can be changed.

## Development

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest pytest-aiohttp
.venv/bin/python -m pytest
```
