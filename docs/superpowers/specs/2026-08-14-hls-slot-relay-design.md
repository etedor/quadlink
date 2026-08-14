# QuadLink self-hosted HLS slot relay — design

- **Date:** 2026-08-14
- **Status:** approved (brainstorming) → ready for implementation plan
- **Author:** Eric + Claude

## Background

QuadStream (`quadstream.tv`) is the tvOS app + web service QuadLink feeds. The Apple TV
displays a 2×2 quad; each tile is an independent player pointed at a **per-slot URL**.

On 2026-08-13 quadstream.tv suffered a ~33-hour outage (Cloudflare 522 → hosting 503 →
recovered 2026-08-14). QuadLink handled it correctly (caught/logged every failure, no crash,
self-recovered), but with quadstream.tv down the Apple TV had nothing to point at. We want a
**self-hosted fallback** on `duke` (the always-on NixOS box QuadLink already runs on) so an
extended quadstream.tv outage doesn't take the quad down.

### What quadstream.tv actually does (confirmed by live capture 2026-08-14)

The read interface is a **plain 302 redirector**, not a stitching relay:

```
Apple TV tile → GET http://quadstream.tv/stream/{short_id}/{slot}    (slot 1..4)
  → 301 http→https (Cloudflare)
  → 302 Found, Location: <current Twitch media-playlist URL>, empty body
     Cache-Control: no-cache, no-store, must-revalidate; Pragma: no-cache; past Expires
```

- Stack: PHP 5.6 / LiteSpeed behind Cloudflare.
- The redirect **target is a Twitch _media_ (variant) playlist** —
  `*.playlist.ttvnw.net/v1/playlist/{token}.m3u8`, containing `#EXT-X-TARGETDURATION:6`,
  `#EXT-X-MEDIA-SEQUENCE`, `#EXTINF:2.000` segments on `*.cloudfront.hls.ttvnw.net`,
  `Content-Type: application/vnd.apple.mpegurl`.
- QuadLink already computes exactly these URLs: `update_quad` POSTs `stream1..4` to
  `/stream/api/stream/{short_id}/update`, and quadstream stores them and redirects to them.
  (`Stream.master_url` — despite the name it is a media/variant playlist URL.)

### The problem the relay fixes: flaky switching

User-reported: when a Twitch stream ends and QuadLink swaps in another, the tile only
**sometimes** picks up the new stream. Best-supported mechanism (inferred): AVPlayer follows
the 302 once, then polls the resolved Twitch media-playlist URL **directly** — so it pins to
the old stream and QuadLink's swap has no effect on the running tile. It only breaks out when
the old playlist terminates (`#EXT-X-ENDLIST`/stale), and AVPlayer's failover-reload of the
original URL isn't guaranteed → "sometimes."

A plain redirector **cannot** fix this (the player still pins to a dying Twitch URL). The fix
is to serve a media playlist **we own** and keep continuously live at the stable slot URL, so
the player polls **us** and we control continuity across source swaps.

## Goals / non-goals

**Goals**
- 4 stable per-slot endpoints on duke that the Apple TV can point at instead of quadstream.tv.
- Reliable, prompt switching when QuadLink swaps a slot — the player never pins to a dying URL.
- Work entirely while quadstream.tv is down; duke relays only playlist text, never video.
- Dual-home: QuadLink keeps updating quadstream.tv in parallel (no regression when it's up).

**Non-goals**
- Zero-glitch switching. A source change with different encoding causes a brief rebuffer at the
  `EXT-X-DISCONTINUITY` splice; no HLS design avoids that. "Reliable within a second or two."
- Proxying video segments through duke (segments stay absolute Twitch CDN URLs).
- Transcoding / compositing (the tvOS app does the 2×2 layout client-side).
- Any change to QuadLink's stream selection, filtering, or quad-building logic.

## Accepted tradeoff

The relay puts duke in the **steady-state playback path** (the player fetches the playlist
from duke every couple seconds; segments still come from Twitch CDN). The redirector didn't —
after the initial redirect, playback was player↔Twitch. Accepted because duke is always-on and
switching is the thing being fixed.

## Architecture

Three pieces, all in-process in the existing daemon (daemon loop and aiohttp web UI already
share one asyncio event loop, so state is a plain shared object — no file, no locking):

```
StreamProcessor → QuadBuilder → Quad (4 media-playlist URLs)
         │
         ├─ QuadStreamClient.update_quad(...)      # unchanged, dual-home
         └─ SlotStore.update(quad.to_list())       # NEW: write current 4 URLs each cycle

Apple TV tile ──GET /streams/{n}──▶ SlotRelay ──fetch (cached ~1s)──▶ Twitch media playlist
                                        │
                                        └─ emit OUR live media playlist (own MEDIA-SEQUENCE,
                                           DISCONTINUITY on source change, no ENDLIST)
                                        ◀── player fetches .ts segments straight from Twitch CDN
```

### Component 1 — `SlotStore`

- Holds `{1: url|None, 2: ..., 3: ..., 4: ...}`.
- `update(urls: list[str])` — called by the daemon each cycle with `quad.to_list()`.
- `get(slot: int) -> str | None`.
- Trivial; no persistence (rebuilt within one cycle on restart).

### Component 2 — `SlotRelay` (the per-slot republisher)

Per-slot state:
- `emitted`: deque of published segments — each `(seq, uri, extinf, pdt|None, discontinuity: bool)`.
- `next_seq`: monotonic counter (our `EXT-X-MEDIA-SEQUENCE` source).
- `discontinuity_seq`: incremented as a discontinuity segment rolls out of the window
  (our `EXT-X-DISCONTINUITY-SEQUENCE`).
- `last_source_url`: to detect swaps.
- `emitted_uris`: membership set/deque to dedupe source segments already published.
- `target_duration`: tracked from source.

On `GET /streams/{n}`:
1. `url = SlotStore.get(n)`; if `None` → `503`.
2. If `url != last_source_url` (and we have prior state) → mark a **pending discontinuity**;
   set `last_source_url = url`. (New source's segment URIs are all new, so dedupe treats them
   as fresh automatically.)
3. Fetch `url` server-side, coalesced by a **~1s TTL cache keyed by url** (so rapid reloads /
   restarts don't hammer Twitch). On fetch failure → serve the current window unchanged; if the
   window is empty → `503`.
4. Parse the source media playlist → ordered `(uri, extinf, pdt)`. For each source segment not
   in `emitted_uris`: append to `emitted` with `next_seq++`, carrying the discontinuity flag on
   the **first** appended after a swap; update `target_duration`.
5. Trim `emitted` to the window (~10–12 segments, matching Twitch's ~10s window so segment
   tokens stay fresh); advance `EXT-X-MEDIA-SEQUENCE`/`EXT-X-DISCONTINUITY-SEQUENCE` for anything
   trimmed.
6. Render our media playlist: `#EXTM3U`, `#EXT-X-VERSION:6` (≥6 is required once we emit
   `#EXT-X-DISCONTINUITY-SEQUENCE`; we own our playlist version independent of the source's v3),
   `#EXT-X-TARGETDURATION`,
   `#EXT-X-MEDIA-SEQUENCE`, `#EXT-X-DISCONTINUITY-SEQUENCE`; then per segment: `#EXT-X-DISCONTINUITY`
   where flagged, optional `#EXT-X-PROGRAM-DATE-TIME`, `#EXTINF`, absolute segment URL.
   **Never** emit `#EXT-X-ENDLIST`.
7. Respond `200` with `Content-Type: application/vnd.apple.mpegurl` and the no-cache headers.

### Component 3 — HTTP routes

- Add `GET /streams/{n}` (n = 1..4) to the existing `WebUI` aiohttp app (`webui.py`).
- The web UI has no auth middleware, so these routes are public by default — required (the
  Apple TV can't authenticate).
- `WebUI` gains a reference to the `SlotRelay` (constructed by the daemon and passed in).

### Daemon integration (`daemon.py`)

- Construct `SlotStore` + `SlotRelay`; pass into `WebUI`.
- In `_main_loop`, after `build_quad`, call `SlotStore.update(quad.to_list())` **every cycle**,
  regardless of `quad_changed` and independent of the quadstream update result (so the local
  relay tracks reality even while quadstream.tv is down / the update errors).
- The existing `update_quad` + webhook path is unchanged (dual-home).

## Failure handling

| Condition | Behavior |
|---|---|
| Slot empty (startup, no quad yet) | `503` |
| Source playlist fetch fails / 5xx | Serve current window unchanged (stale-OK); `503` if window empty; log at warning |
| Source stream ended (`#EXT-X-ENDLIST`/404) | Keep serving last window (no ENDLIST) until QuadLink swaps the slot |
| Segment token expiry | Small window keeps segments fresh; player fetches promptly (same constraint Twitch's own player has) |
| quadstream.tv down | Irrelevant to the relay; update path errors are logged as today |

## Config / deployment

- Path: `/streams/{1..4}` (single-user box — no account-id segment).
- Port 8081 (existing web UI), already Docker-exposed and nginx-fronted. **Requires an nginx
  `location /streams/` block** proxying to the same container if nginx isn't a catch-all proxy.
- Apple TV tiles repointed from `http://quadstream.tv/stream/{id}/{n}` to the duke/nginx URL.
- No new bind, no new port, no new service.

## Testing / validation

- **Unit** (pytest): playlist synthesis — monotonic `MEDIA-SEQUENCE`; `DISCONTINUITY` inserted
  exactly on source-URL change; `DISCONTINUITY-SEQUENCE` advances on trim; never emits `ENDLIST`;
  correct `Content-Type` + no-cache headers; `503` when slot empty; dedupe across polls; window
  trimming.
- **Integration**: run the relay locally fed by real QuadLink-resolved URLs; point **Safari**
  (same AVFoundation/CoreMedia HLS engine as tvOS) and `ffprobe` at `/streams/1`; force a slot
  swap and confirm the switch. Run emitted playlists through Apple's **`mediastreamvalidator`**.
- **Field**: deploy to duke, point one Apple TV tile at duke, watch switching and the aiohttp
  access log (confirms AVPlayer's real reload cadence, validating the mechanism inference above).

## Out of scope / future

- Health-check + log-noise improvements to the existing quadstream update path (flagged earlier;
  separate change).
- Optional: retire the parallel quadstream.tv update entirely (local-only) if desired later.
