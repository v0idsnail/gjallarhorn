#!/usr/bin/env python3
"""fetcher.py: multi-source ransomware feeds -> events.js for the Gjallarhorn display.

The ONLY network-touching component of the display. Applies the security
review ruleset (2026-10-24, incl. adversarial audit v2): treat every byte
from every API as hostile. This holds equally for operator-supplied paid
sources: their data is sanitized with the same pipeline, their credentials
are read from environment variables and never written to any artifact,
config, or log line.

Modes:
  --demo              generate synthetic events.js (offline, zero network:
                      the quick-start path; fake groups/victims, clearly
                      labeled, deterministic)
  --fixture FILE      primary-source offline mode (raw API-shaped JSON)
  --once              poll the live API once (PRO tier if the key env is set)
  --source NAME       poll only the named source (adapter testing)
  (run from a systemd timer with jitter, never a fixed-interval cron)

Second sources: a sources.json next to this script (ops/, gitignored: it
names paid-feed URLs and auth env vars) adds concurrent
feeds that are correlated against the primary. Each entry:

    {
      "name": "acme-ti",                  # slug, shown in the UI
      "url": "https://api.acme.example/v1/victims",   # https enforced
      "root": "victims",                  # optional: array key in the response
      "auth_header": "Authorization",     # optional
      "auth_env": "ACME_API_KEY",         # env var read at runtime; never stored
      "priority": 2,                      # lower wins display-field conflicts
      "map": {                            # their field names -> ours
        "group": "actor_name",
        "title": "victim",
        "sector": "industry",             # optional
        "country": "country_code",        # must be ISO-2 or it is dropped
        "description": "summary",         # optional
        "discovered": "first_seen",       # optional (one ts field required)
        "published": "announced"          # optional
      }
    }

Paid-feed operators act under their own responsibility and their API's
terms; this tool only ever ships the adapter config, never their data.

Correlation policy (merge):
  k            canonical hash of normalized (group, title): lowercase,
               punctuation stripped, legal suffixes (inc/ltd/gmbh/...)
               dropped, so "Acme Inc." and "acme incorporated" merge.
  g/t          reported by the highest-priority source covering the event
  a/c/d        first non-empty value in priority order (fill-down)
  di/pu        earliest valid timestamp across sources
  s            all source names that reported the event, priority order;
               len(s) > 1 = corroborated

API tiers (ransomware.live, terms 2026-08-19): the anonymous v1 endpoint is
DEPRECATED: personal use only, 1 req/min. Setting the RANSOMWARE_LIVE_API_KEY
environment variable (free key from my.ransomware.live) switches the primary
to the PRO API (api-pro.ransomware.live/victims/recent, X-API-KEY header,
free, 500k calls/month, fresher data). Data is cached 30 minutes upstream:
never poll faster than every 30 minutes. Attribution with a link to
ransomware.live is required on any display of the data; raw data must never
be republished; commercial use of the data requires the owner's approval.

Failure semantics: fail closed on the PRIMARY source (existing events.js
untouched); a failed secondary source is a warning and the display runs
without it. A non-empty response yielding zero valid events is always a
hard failure for that source (audit F2).

Field policy (content-injection review):
  keep    group_name, post_title, activity, country, description,
          discovered, published   (sanitized, capped)
  drop    post_url, screenshot, website, data_size, ransom
          (never rendered by the page; not carried into the artifact)

Output: <outdir>/events.js containing `window.EVENTS = [...]` with
ensure_ascii JSON (pure ASCII, cannot break JS string-literal context).
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request

API_URL = "https://api.ransomware.live/v1/recentvictims"
PRIMARY_NAME = "ransomware.live"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_EVENTS = 5000
TS_MAX_FUTURE_S = 300          # allow 5min clock skew
TS_MAX_AGE_S = 45 * 24 * 3600  # reject older-than-45d timestamps
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) gjallarhorn/1.1.0"  # generic

# API PRO tier (used when RANSOMWARE_LIVE_API_KEY is set): free token from
# my.ransomware.live; X-API-KEY header; renames victims' field names.
PRO_API_URL = "https://api-pro.ransomware.live/victims/recent"
PRO_KEY_ENV = "RANSOMWARE_LIVE_API_KEY"
PRO_FIELD_MAP = {  # their field -> legacy record field (transform pipeline).
    # NOTE: the PRO swagger does not list `description` (v1 has it). If the
    # PRO payload omits it, it maps to "" silently: tier 2 becomes rare and
    # the overlay shows no claim text. Verify against one live PRO payload
    # once a key is registered; harmless to keep meanwhile.
    "group": "group_name",
    "victim": "post_title",
    "activity": "activity",
    "country": "country",
    "description": "description",
    "discovered": "discovered",
    "attackdate": "published",
}

CAPS = {
    "group_name": 60,
    "post_title": 120,
    "activity": 60,
    "country": 2,
    "description": 900,
    "discovered": 40,
    "published": 40,
}

ISO_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)$")
ISO_COUNTRY = re.compile(r"^[A-Z]{2}$")
NAME_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")

# generic-adapter field map: config key -> ransomware.live record field
MAP_FIELDS = {
    "group": ("group_name", True),
    "title": ("post_title", True),
    "sector": ("activity", False),
    "country": ("country", False),
    "description": ("description", False),
    "discovered": ("discovered", False),
    "published": ("published", False),
}

# hostile characters: C0/C1 controls, DEL, bidi overrides & marks, zero-width,
# soft hyphen, tag/interlinear annotation. Stripped BEFORE and AFTER NFKC so
# compatibility-decomposed invisibles cannot survive (audit F3).
_BAD_CHARS = re.compile(
    "[\x00-\x1f\x7f-\x9f"
    "\u202a-\u202e\u2066-\u2069\u200e\u200f\u200b\u200c\u200d\u2060\ufeff"
    "\u00ad\u061c\u180e\u2ff0-\u2ffb\ufff9-\ufffb"
    "\U000E0000-\U000E007F]"
)


def sanitize_str(s: str, cap: int) -> str:
    import unicodedata

    s = _BAD_CHARS.sub("", s)
    s = unicodedata.normalize("NFKC", s)
    s = _BAD_CHARS.sub("", s)
    if len(s) > cap:
        s = s[:cap]
    return s


# ---- cross-source canonicalization -------------------------------------
# Legal-entity suffixes dropped for MATCHING ONLY (never from displayed
# text): symmetric on both sides, so "Blue Bottle Co" still matches itself.
_LEGAL_SUFFIXES = frozenset(
    "inc incorporated ltd limited llc plc gmbh ag ab as oy bv nv sa srl spa "
    "pty pvt co corp corporation company".split()
)
_PUNCT = re.compile(r"[^\w\s]+|_", re.UNICODE)  # \w keeps _; slug feeds use it as separator


def canon_text(s: str) -> str:
    s = _PUNCT.sub(" ", s.lower())  # \w keeps _; slug feeds use it as a separator
    return " ".join(t for t in s.split() if t not in _LEGAL_SUFFIXES)


def dedup_key(group: str, title: str) -> str:
    # Canonicalized pair: cross-source merge identity ("Acme Inc." ==
    # "acme incorporated"). JSON-encoded: no separator collision; ASCII-safe
    # after sanitize_str (audit F4).
    payload = json.dumps([canon_text(group), canon_text(title)], ensure_ascii=True)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()[:16]


def parse_ts(v: str, now: float) -> str:
    """Return the sanitized timestamp or '' if invalid/implausible."""
    if not ISO_TS.match(v):
        return ""
    import datetime

    try:
        # normalize fractional seconds to <=6 digits: datetime.fromisoformat
        # on Python <3.11 rejects other widths (portability, same verdict)
        norm = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], v)
        ts = datetime.datetime.fromisoformat(norm.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return ""
    if ts > now + TS_MAX_FUTURE_S or ts < now - TS_MAX_AGE_S:
        return ""
    return v


def transform(raw) -> list:
    """Validate + sanitize ransomware.live-shaped records.
    Raises on structural violations or total wipe."""
    if not isinstance(raw, list):
        raise ValueError("envelope is not a JSON array")
    if len(raw) > MAX_EVENTS:
        raise ValueError(f"event count {len(raw)} exceeds cap {MAX_EVENTS}")
    now = time.time()
    out, seen = [], set()
    for ev in raw:
        if not isinstance(ev, dict):
            continue  # drop the element, not the batch
        rec, ok = {}, True
        for f, cap in CAPS.items():
            v = ev.get(f)
            v = "" if v is None else v
            if not isinstance(v, str):
                ok = False
                break
            if f == "country":
                # validate BEFORE any truncation: capping first would turn
                # "CHN" into a wrong-but-valid "CH" (China plotted as
                # Switzerland). Regex-exact [A-Z]{2} needs no sanitize_str.
                rec[f] = v if ISO_COUNTRY.match(v) else ""
                continue
            rec[f] = sanitize_str(v, cap)
        if not ok:
            continue  # wrong-typed field: drop the element, not the batch
        if not rec["group_name"] or not rec["post_title"]:
            continue  # required fields (map_raw contract)
        rec["discovered"] = parse_ts(rec["discovered"], now)
        rec["published"] = parse_ts(rec["published"], now)
        if not rec["discovered"] and not rec["published"]:
            continue
        key = dedup_key(rec["group_name"], rec["post_title"])
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "k": key,
                "g": rec["group_name"],
                "t": rec["post_title"],
                "a": rec["activity"],
                "c": rec["country"],
                "d": rec["description"],
                "di": rec["discovered"],
                "pu": rec["published"],
            }
        )
    # fail-closed guard (audit F2): a non-empty response that produced zero
    # valid events is an attack or an API break; never blank the display
    if raw and not out:
        raise ValueError("non-empty response yielded zero valid events; refusing to wipe")
    return out


def _ts(v: str) -> float:
    """Parse an ISO timestamp already validated by parse_ts to an instant."""
    import datetime

    # same fraction normalization as parse_ts: Python <3.11 fromisoformat
    # rejects widths other than 3 or 6 digits
    norm = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], v)
    return datetime.datetime.fromisoformat(norm.replace("Z", "+00:00")).timestamp()


def merge(sources: list) -> list:
    """sources: [(name, priority, events)] in any order.
    Returns correlated events per the policy in the module docstring."""
    merged = {}
    for name, _pri, events in sorted(sources, key=lambda s: s[1]):
        for ev in events:
            cur = merged.get(ev["k"])
            if cur is None:
                merged[ev["k"]] = dict(ev, s=[name])
                continue
            if name not in cur["s"]:
                cur["s"].append(name)
            for f in ("a", "c", "d"):  # fill-down by priority
                if not cur.get(f) and ev.get(f):
                    cur[f] = ev[f]
            for f in ("di", "pu"):  # earliest wins (parsed instants: mixed
                # timezone offsets make lexicographic ISO comparison wrong)
                if ev.get(f) and (not cur.get(f) or _ts(ev[f]) < _ts(cur[f])):
                    cur[f] = ev[f]
    out = list(merged.values())
    if len(out) > MAX_EVENTS:
        raise ValueError(f"merged event count {len(out)} exceeds cap {MAX_EVENTS}")
    return out


def write_events_js(events: list, outdir: str) -> str:
    # "</" is escaped so a hostile post_title can never terminate the
    # inline script block; JSON-legal, browser-invisible (JS string "<\/" === "</")
    body = json.dumps(events, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")
    write_atomic(os.path.join(outdir, "events.json"), body + "\n")
    payload = "window.EVENTS=" + body + ";\n"
    return write_atomic(os.path.join(outdir, "events.js"), payload)


def write_atomic(dest: str, payload: str) -> str:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix=".events-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())  # durability before the atomic rename (audit F6)
        os.chmod(tmp, 0o644)  # mkstemp gives 0600; server may run as another user
        os.replace(tmp, dest)  # atomic
        return dest
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---- fetching ------------------------------------------------------------


def _redirect_guard(host: str):
    class _SafeRedirect(urllib.request.HTTPRedirectHandler):
        """Follow only https redirects that stay on this source's host (audit F7)."""

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            u = urllib.parse.urlparse(newurl)
            if u.scheme != "https" or u.hostname != host:
                return None
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    return _SafeRedirect


def fetch_http(url: str, auth_header: str = "", auth_env: str = "") -> object:
    if urllib.parse.urlparse(url).scheme != "https":
        raise ValueError("non-https source URL refused")
    headers = {"User-Agent": USER_AGENT}
    if auth_header:
        key = os.environ.get(auth_env, "")
        if not key:
            raise ValueError(f"auth env var {auth_env} is not set")
        if re.search(r"[\x00-\x20\x7f-\xff]", key):
            # reject before http.client echoes the credential in a ValueError
            raise ValueError(f"auth env var {auth_env} contains illegal characters")
        headers[auth_header] = key
    host = urllib.parse.urlparse(url).hostname
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.build_opener(_redirect_guard(host)).open(req, timeout=30) as resp:
        ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ValueError(f"unexpected content-type {ctype!r}")
        blob = resp.read(MAX_RESPONSE_BYTES + 1)
    if len(blob) > MAX_RESPONSE_BYTES:
        raise ValueError("response exceeds size cap")
    return json.loads(blob)


def fetch_live() -> list:
    """Primary source: PRO tier when RANSOMWARE_LIVE_API_KEY is set (free key
    from my.ransomware.live), else the deprecated anonymous v1 endpoint
    (personal use only). PRO field names are projected onto the legacy
    record shape so the audited transform pipeline applies unchanged."""
    key = os.environ.get(PRO_KEY_ENV, "")
    if not key:
        return fetch_http(API_URL)
    return map_raw(fetch_http(PRO_API_URL, "X-API-KEY", PRO_KEY_ENV),
                   {"inv": PRO_FIELD_MAP})


# ---- operator-supplied sources -------------------------------------------


def load_sources(path: str) -> list:
    """Parse + validate sources.json. Fail closed on any config error."""
    try:
        with open(path, "rb") as fh:
            blob = fh.read(MAX_RESPONSE_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"cannot read sources config: {exc}")
    if len(blob) > MAX_RESPONSE_BYTES:
        raise ValueError("sources config exceeds size cap")
    try:
        cfg = json.loads(blob)
    except ValueError as exc:
        raise ValueError(f"sources config is not valid JSON: {exc}")
    if not isinstance(cfg, list) or not cfg:
        raise ValueError("sources config must be a non-empty JSON array")
    out, names = [], {PRIMARY_NAME}
    for i, src in enumerate(cfg):
        where = f"sources[{i}]"
        if not isinstance(src, dict):
            raise ValueError(f"{where}: not an object")
        name = src.get("name", "")
        if not isinstance(name, str) or not NAME_SLUG.match(name):
            raise ValueError(f"{where}: bad name (slug ^[a-z0-9][a-z0-9._-]{{0,31}}$)")
        if name in names:
            raise ValueError(f"{where}: duplicate name {name!r}")
        names.add(name)

        entry = {"name": name, "priority": 50, "root": "", "fixture": ""}
        if "url" not in src and "fixture" not in src:
            raise ValueError(f"{where}: needs url or fixture")
        if "url" in src:
            if not isinstance(src["url"], str) or urllib.parse.urlparse(src["url"]).scheme != "https":
                raise ValueError(f"{where}: url must be https://")
            entry["url"] = src["url"]
        if "fixture" in src:
            if not isinstance(src["fixture"], str) or not src["fixture"]:
                raise ValueError(f"{where}: fixture must be a path")
            entry["fixture"] = src["fixture"]

        pri = src.get("priority", 50)
        if not isinstance(pri, int) or isinstance(pri, bool) or pri < 1:
            raise ValueError(f"{where}: priority must be an int >= 1")
        entry["priority"] = pri

        for opt in ("root", "auth_header", "auth_env"):
            if opt in src:
                if not isinstance(src[opt], str):
                    raise ValueError(f"{where}: {opt} must be a string")
                entry[opt] = src[opt]
        if bool(entry.get("auth_header")) != bool(entry.get("auth_env")):
            raise ValueError(f"{where}: auth_header and auth_env come together")

        mapping = src.get("map")
        if not isinstance(mapping, dict):
            raise ValueError(f"{where}: map must be an object")
        inv = {}
        for cfg_key, api_field in mapping.items():
            if cfg_key not in MAP_FIELDS:
                raise ValueError(f"{where}: unknown map key {cfg_key!r}")
            if not isinstance(api_field, str) or not api_field:
                raise ValueError(f"{where}: map.{cfg_key} must be a field name")
            if api_field in inv:
                raise ValueError(f"{where}: map target fields must be distinct (duplicate {api_field!r})")
            inv[api_field] = MAP_FIELDS[cfg_key][0]
        for cfg_key, (_rec, required) in MAP_FIELDS.items():
            if required and cfg_key not in mapping:
                raise ValueError(f"{where}: map.{cfg_key} is required")
        entry["inv"] = inv
        out.append(entry)
    return out


def map_raw(raw: object, src: dict) -> list:
    """Project a generic source's response onto ransomware.live-shaped
    records so the audited transform pipeline applies unchanged."""
    root = src.get("root", "")
    if root:
        if not isinstance(raw, dict) or not isinstance(raw.get(root), list):
            raise ValueError(f"root key {root!r} missing or not a list")
        raw = raw[root]
    if not isinstance(raw, list):
        raise ValueError("envelope is not a JSON array")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rec = {}
        for api_field, rec_field in src["inv"].items():
            v = item.get(api_field)
            if not isinstance(v, str):
                v = ""  # wrong type: empty, transform's required-field check drops it
            rec[rec_field] = v
        for rec_field in CAPS:
            rec.setdefault(rec_field, "")
        out.append(rec)
    return out


def collect_source(src: dict) -> list:
    if src.get("fixture"):
        with open(src["fixture"], "rb") as fh:
            blob = fh.read(MAX_RESPONSE_BYTES + 1)
        if len(blob) > MAX_RESPONSE_BYTES:
            raise ValueError("fixture exceeds size cap")
        raw = json.loads(blob)
    else:
        raw = fetch_http(src["url"], src.get("auth_header", ""), src.get("auth_env", ""))
    return transform(map_raw(raw, src))


def demo_events() -> list:
    """Deterministic synthetic events for --demo (offline quick start).
    Invented group/victim names, plainly labeled descriptions; flows through
    the same transform pipeline so the display exercises every tier without
    touching any API or republishing any real victim data."""
    import datetime

    groups = ["DEMOGRID", "DEMOLOCK", "DEMOWOLF", "DEMOHAMMER"]
    kinds = ["Corp", "Systems", "Logistics", "Klinik", "Bank", "Council", "Works"]
    sectors = ["Healthcare", "Energy & Utilities", "Financial Services",
               "Government & Defense", "Transportation", "Retail", "Manufacturing"]
    countries = ["SE", "DK", "DE", "NO", "FI", "NL", "FR", "US", "GB", "PL", "ES", "BR"]
    long_desc = (
        "Synthetic demonstration record generated by --demo. No real "
        "organization, group, or incident: every field is invented so the "
        "display can run offline without touching any API or republishing "
        "any real victim data. "
    ) * 4
    now = datetime.datetime.now(datetime.timezone.utc)
    recs = []
    for i in range(40):
        when = (now - datetime.timedelta(hours=int(i * 21.7))).isoformat()
        recs.append(
            {
                "group_name": groups[i % len(groups)],
                "post_title": "Example %s %d" % (kinds[i % len(kinds)], i + 1),
                "activity": sectors[(i * 3) % len(sectors)],
                "country": countries[(i * 7) % len(countries)],
                "description": long_desc if i % 3 == 0 else "",
                "discovered": when,
                "published": "",
            }
        )
    return transform(recs)


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))  # ops dir, outside the web root
    web_root = os.path.normpath(os.path.join(here, "..", "web"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true",
                    help="write a synthetic events.js (offline quick start; no network)")
    ap.add_argument("--fixture", help="primary-source local JSON instead of the network")
    ap.add_argument("--once", action="store_true", help="poll the live API once")
    ap.add_argument("--source", help="poll only the named source (adapter testing)")
    ap.add_argument("--sources", default=os.path.join(here, "sources.json"),
                    help="second-source config path (default: sources.json next to this script)")
    ap.add_argument("--outdir", default=web_root,
                    help="web root to receive events.js (default: sibling ../web dir)")
    args = ap.parse_args()

    try:
        sources = load_sources(args.sources) if os.path.exists(args.sources) else []
        single = None
        if args.source:
            if args.source == PRIMARY_NAME:
                sources = []
            else:
                sources = [s for s in sources if s["name"] == args.source]
                if not sources:
                    raise ValueError(f"unknown source {args.source!r}")
                single = sources[0]

        collected = []  # [(name, priority, events)]

        if single is None:
            # primary (fail closed: its failure aborts, last-good events.js stays)
            if args.demo:
                collected.append(("demo", 1, demo_events()))
            elif args.fixture:
                with open(args.fixture, "rb") as fh:
                    blob = fh.read(MAX_RESPONSE_BYTES + 1)
                if len(blob) > MAX_RESPONSE_BYTES:
                    raise ValueError("fixture exceeds size cap")
                raw = json.loads(blob)
                collected.append((PRIMARY_NAME, 1, transform(raw)))
            elif args.once:
                raw = fetch_live()
                collected.append((PRIMARY_NAME, 1, transform(raw)))
            else:
                ap.error("need --demo, --fixture FILE, or --once")

        # secondaries (best effort: warn + continue on failure)
        for src in sources:
            try:
                events = collect_source(src)
            except Exception as exc:
                print(f"warn: source {src['name']} failed: {exc}; continuing without it",
                      file=sys.stderr)
                continue
            collected.append((src["name"], src["priority"], events))

        if not collected:
            raise ValueError("no source produced data; refusing to write an empty display")
        events = merge(collected)
        if not events:
            raise ValueError("empty event set; refusing to write an empty display")
        dest = write_events_js(events, args.outdir)
    except Exception as exc:  # fail closed: last-good events.js untouched
        print(f"error: {exc}", file=sys.stderr)
        return 1
    corrobored = sum(1 for e in events if len(e.get("s", [])) > 1)
    print(f"ok: {len(events)} events ({corrobored} corroborated) -> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
