# Changelog

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
