# Changelog

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
