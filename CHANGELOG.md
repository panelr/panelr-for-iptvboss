# Changelog

## 1.2.0

- **Fix: a customer created between syncs could not watch.** IPTV Boss writes each customer a
  playlist file during a sync and answers `get.php` with 403 until that file exists, so anyone
  added by API in between was refused, often for hours. The channels themselves are in IPTV Boss's
  database the moment the customer is created, so when IPTV Boss refuses the playlist is built
  from `get_live_categories` and `get_live_streams` and served straight away, in the same
  m3u_plus layout IPTV Boss writes, in IPTV Boss's channel order, with the customer's picks
  applied as usual. `output=m3u8` is honoured. If the channel list cannot be read, IPTV Boss's
  own refusal is passed on unchanged, so nothing is hidden.

## 1.1.0

Fixes found in production by NemosTV (thank you) and brought into the product in a
form that works for every install.

- **Fix: ids past 32 bits broke players.** IPTV Boss builds ids as item × 10000 + layout,
  and a big layout crosses 2,147,483,647. TiviMate reads ids as a Java int and drops
  the whole list at the first oversized one, so movies and series vanished. Oversized ids
  are now sent to players compacted (item × 100 + layout × 10 + 7) and expanded back on
  every request that carries one: `player_api.php` parameters (query and form) and
  `/live/` and `/movie/` stream paths. Ids inside the limit are untouched, byte for byte.
  Series episode ids are never touched.
- **Fix: `get_vod_info` and `get_series_info` lost their redirect.** IPTV Boss answers
  them with a 302 to the provider; the middleware now passes any non-JSON answer through
  with all of its headers.
- **Fix: an empty or cut guide could replace a good one.** IPTV Boss answers `xmltv.php`
  with an empty body while it rewrites a guide. A download that is not a whole XMLTV file
  is thrown away and the current guide stays; while one request rebuilds a guide, the
  others keep using the current one instead of waiting or rebuilding themselves.
- **Fix: category ids repeat across layouts.** The catalogue was keyed by (type, id), so
  two layouts with a group 12 overwrote each other and every service downloaded a mixed
  list. It is now keyed by (type, layout, id); a 1.0 database is migrated on start. Send
  `layout` on `categories/refresh` and on picks so the middleware never has to guess.
  Layout 0 is a real layout, not "unknown".
- **Change: a category that disappears is no longer marked removed.** Sports groups
  empty out between games and seasons, and a removal made panels delete the bouquet and
  the customer's link. New setting `MW_CATEGORY_GRACE_DAYS`: 0 (default) never marks a
  category removed; N marks it removed once it has been missing for N days. A category
  that comes back clears its own removal.
- **Faster: customers without picks no longer cost a JSON parse.** A full list is sent as
  IPTV Boss's own bytes (ids compacted), prepared once per distinct answer and kept in a
  cache bounded by `MW_LIST_CACHE_MB` (default 64, 0 = off); gzip only when the player
  asks for it, with `Vary: Accept-Encoding`.
- **Faster: category lists are written to the catalogue once per distinct answer**, not on
  every request.
- Picks saved for a line whose layout is not known yet hide the rest instead of excluding
  another layout's ids. `GET .../picks` now reports the customer's `layout`.
- Tests for all of it (19 new).

## 1.0.0

- **Fix: gzip sent to clients that didn't ask for it.** When a request had no
  `Accept-Encoding`, aiohttp added `Accept-Encoding: gzip` on the way to IPTV Boss, and
  the gzipped answer was streamed back undecoded. IPTV Boss's own desktop app (Java
  HttpClient) sends no `Accept-Encoding` and can't read gzip, so its XC cloud sync read
  the server revision as 0 and failed with "XC Server does not have an authoritative
  database backup" / "The local XC backup is ahead of the server backup". IPTV Boss is
  now asked for `identity` unless the client itself accepts compression. Players that
  send `Accept-Encoding: gzip` still get gzip.
- **Fix: request bodies lost their `Content-Length`.** Bodies were streamed to IPTV Boss
  chunked. IPTV Boss rejects cloud database uploads without a length, so the desktop's
  Push Now failed with "Backup size is missing or exceeds 1 GB". The client's
  `Content-Length` is now kept.
- **New setting `MW_FORWARDED_PROTO`.** IPTV Boss running with `BEHIND_HTTPS_PROXY=true`
  answers plain-HTTP requests without `X-Forwarded-Proto: https` with 426, which broke
  the middleware's own calls (health, category refresh, guide). Set it to `https` in that
  case. A player proxy's own `X-Forwarded-Proto` still wins. Default is unchanged.
- Tests for all three.

## 0.1.0

- First release.
