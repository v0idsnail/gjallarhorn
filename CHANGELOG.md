# Changelog

## v1.1.0

- Live display mode (default with real data): same-origin `events.json`
  polling once a minute, append-only event spawning, no replay loop. New
  fetcher output appears within about a minute, no page reload.
- Decay trail: settled pings persist on the map as small dimming markers,
  fading across ~2h to a visible floor.
- Replay mode renamed for what it is (demo/clip mode): default for
  `--demo` data, force with `?replay`, auto-fallback when fetch is
  unavailable.
- Scrollable persistent feed in live mode (200-row cap).
- On-screen tier legend and "post text:" label in the detail overlay.
- Fetcher writes `events.json` alongside `events.js` (both atomic).
- CSP updated to `connect-src 'self'` (same-origin polling only; external
  egress remains blocked).

## v1.0.0

Initial release.

- Zero-egress wall display (CSP-enforced, textContent-only rendering) for
  ransomware leak-site claims with world map, pings, attack cards, rolling
  log, and click-to-inspect detail overlay (Esc, click-outside, 30 s idle
  auto-dismiss).
- Hardened fetcher: hostile-input sanitization (control/bidi/zero-width/tags
  stripped around NFKC), field caps, timestamp and country validation,
  pure-ASCII atomic artifact writes, fail-closed on every error path.
- Free ransomware.live feed with optional PRO tier (free key via
  RANSOMWARE_LIVE_API_KEY env var).
- Bring-your-own-feed adapters (ops/sources.json) with cross-source
  correlation: canonical name merging, fill-down by priority, earliest
  timestamps, source attribution, corroborated events marked.
- Synthetic --demo mode for offline first runs; no API, no data.
