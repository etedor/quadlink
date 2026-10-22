# Benchmarks

Run every script from the repo root, inside `nix develop`. Each script has a module docstring with all options. Raw output goes to `bench/data/out/` (not in git).

| Script | Measures |
|---|---|
| `lookup_latency.py` | Real Twitch lookup times through `StreamlinkFetcher` and the ttvlol plugin |
| `cycle_time.py` | Refresh time for many channels, with a fake lookup |
| `relay_memory.py` | Relay memory and CPU for 4 slots through `SlotRelay`, `SegmentProxy` and ffmpeg |

## Real lookup latency

Command:

```bash
python bench/lookup_latency.py --rounds 20 --pause 20 --out bench/data/out/lookup-latency.jsonl
```

Setup: 24 well-known channels (11 live, 13 offline), concurrency 4, no timeout, 25 rounds in two runs. Host: machina (macOS), 2026-10-22, default `proxy_playlist` (`https://eu.luminous.dev`).

| Outcome | Lookups | p50 (s) | p90 (s) | p99 (s) | Max (s) |
|---|---|---|---|---|---|
| All | 600 | 0.61 | 0.73 | 1.16 | 1.70 |
| Live | 275 | 0.64 | 0.78 | 1.21 | 1.70 |
| Offline | 325 | 0.51 | 0.62 | 1.05 | 1.08 |

No lookup failed and no lookup hung. One round of 24 channels took 3.5 to 5.1 seconds.

## Lookup timeout

Command:

```bash
python bench/cycle_time.py --timeouts 30 15 10 8 --channels 16 32 64 --levels 1 3 5 --runs 15
python bench/cycle_time.py --timeouts 30 15 10 8 --channels 16 32 64 --levels 1 3 5 --runs 15 --workers 8
```

The fake lookup is more pessimistic than the real one. 90% take 0.5 to 3 seconds, 5% take 3 to 10 seconds, and 5% hang for 40 seconds and then fail. The real `StreamlinkFetcher` runs the fake lookup in its thread pool, so a hung lookup keeps its thread after the timeout. Concurrency is 4. Each cell shows the median and p90 refresh time in seconds, and the mean count of failed lookups per refresh. "Cut" is the count of failed lookups that would have succeeded with no timeout.

Thread pool of 4 threads (equal to the concurrency limit):

| Channels | Levels | 30 s | 15 s | 10 s | 8 s |
|---|---|---|---|---|---|
| 16 | 1 | 32.2 / 36.8, fail 0.9 | 18.2 / 22.2, fail 0.9 | 14.0 / 22.8, fail 1.0 | 13.1 / 20.5, fail 1.7, cut 0.5 |
| 16 | 3 | 32.5 / 37.9, fail 0.9 | 18.0 / 22.4, fail 0.9 | 14.6 / 19.7, fail 1.0 | 14.3 / 17.0, fail 1.5, cut 0.5 |
| 16 | 5 | 30.1 / 39.4, fail 0.9 | 18.4 / 24.4, fail 0.9 | 14.8 / 21.0, fail 1.1 | 13.2 / 18.2, fail 1.5, cut 0.5 |
| 32 | 1 | 36.8 / 73.1, fail 1.5 | 26.1 / 57.1, fail 1.7 | 25.6 / 51.1, fail 3.0 | 25.2 / 48.9, fail 4.3, cut 0.8 |
| 32 | 3 | 37.0 / 60.5, fail 1.5 | 30.7 / 57.0, fail 1.8 | 26.7 / 50.5, fail 2.7 | 24.7 / 45.9, fail 3.8, cut 0.8 |
| 32 | 5 | 34.3 / 66.8, fail 1.5 | 24.6 / 54.6, fail 2.1 | 24.7 / 50.2, fail 3.5 | 25.0 / 44.2, fail 4.5, cut 0.8 |
| 64 | 1 | 77.6 / 103.1, fail 3.7 | 66.0 / 91.1, fail 4.4 | 61.7 / 81.7, fail 6.5 | 59.3 / 82.5, fail 8.8, cut 1.1 |
| 64 | 3 | 79.5 / 98.5, fail 3.7 | 67.5 / 93.7, fail 4.0 | 67.7 / 91.1, fail 6.5 | 65.3 / 92.0, fail 10.0, cut 1.1 |
| 64 | 5 | 81.8 / 96.8, fail 3.7 | 69.1 / 89.2, fail 4.1 | 64.4 / 88.1, fail 6.4 | 62.0 / 87.1, fail 10.4, cut 1.1 |

With 4 threads, a shorter timeout helps little from 32 channels on, and more lookups fail. A hung lookup keeps its thread for 40 seconds. The next lookups wait for a free thread, and their timeout runs while they wait.

Thread pool of 8 threads (twice the concurrency limit, matching `StreamProcessor`):

| Channels | Levels | 30 s | 15 s | 10 s | 8 s |
|---|---|---|---|---|---|
| 16 | 1 | 32.2 / 36.1, fail 0.9 | 18.3 / 21.7, fail 0.9 | 13.4 / 18.3, fail 1.0 | 12.1 / 17.1, fail 1.5, cut 0.5 |
| 16 | 3 | 32.6 / 38.1, fail 0.9 | 18.1 / 22.7, fail 0.9 | 13.7 / 18.9, fail 1.1 | 14.3 / 16.9, fail 1.5, cut 0.5 |
| 16 | 5 | 30.2 / 39.2, fail 0.9 | 18.5 / 23.6, fail 0.9 | 14.7 / 19.6, fail 1.0 | 13.4 / 17.4, fail 1.5, cut 0.5 |
| 32 | 1 | 37.1 / 66.8, fail 1.5 | 26.5 / 40.2, fail 1.5 | 23.0 / 31.2, fail 1.6 | 23.0 / 28.8, fail 2.6, cut 0.8 |
| 32 | 3 | 37.1 / 60.5, fail 1.5 | 29.3 / 38.5, fail 1.5 | 26.9 / 33.5, fail 1.7 | 23.4 / 31.2, fail 2.5, cut 0.8 |
| 32 | 5 | 35.3 / 67.2, fail 1.5 | 24.9 / 40.9, fail 1.5 | 25.1 / 31.3, fail 1.7 | 23.8 / 30.4, fail 2.6, cut 0.8 |
| 64 | 1 | 75.6 / 92.9, fail 3.7 | 56.1 / 65.0, fail 3.7 | 49.4 / 56.0, fail 3.9 | 46.4 / 53.3, fail 5.0, cut 1.1 |
| 64 | 3 | 76.5 / 91.4, fail 3.7 | 54.0 / 66.3, fail 3.7 | 47.2 / 57.3, fail 3.8 | 45.4 / 54.6, fail 5.0, cut 1.1 |
| 64 | 5 | 81.2 / 87.9, fail 3.7 | 56.6 / 62.3, fail 3.7 | 51.3 / 53.9, fail 3.8 | 46.3 / 50.9, fail 5.1, cut 1.1 |

Result: `lookup_timeout` is 10 seconds by default, and the thread pool has twice the concurrency limit.

- 10 seconds is 8.6 times the real p99 (1.16 s) and 5.9 times the real maximum (1.70 s).
- Compared with a 30 s timeout and 4 threads, a 10 s timeout with 8 threads makes the median refresh 55-58% shorter for 16 channels, 27-37% shorter for 32 channels and 36-41% shorter for 64 channels. The p90 is 42-57% shorter.
- 8 seconds cuts lookups that would succeed (the 8-10 s tail of the fake model) and gains little more.
- A channel that times out gets 3 times the timeout (30 s) on its next lookup. Thus a channel that is always slow fails on one refresh and then resolves on each refresh after it.

## Relay regression check

Command (Linux container, see the `relay_memory.py` docstring and `bench/data/dock.sh`):

```bash
git archive <commit> src | tar -x -C bench/data/<commit>
python bench/relay_memory.py run --duration 600 --kf-slots 3 --fmp4-slots 2,4 \
    --workroot /tmp/qr --src bench/data/<commit>/src --label <label>
```

Setup: 4 slots, accelerated x8 (0.25 s per 2 s segment), 600 real seconds (80 stream minutes), about 2400 segments per slot. Slot 3 has an extra keyframe in each segment. Slots 2 and 4 are fMP4. Each slot switches channel every 300 stream seconds and stalls every 420 stream seconds. The "before" tree is `main` (`bdee2f9`). The "after" tree is `cleanup` (`e9fa0f7`). Run A ran the two trees one after the other. Run B ran them at the same time.

| Measure | Before, run A | After, run A | Before, run B | After, run B |
|---|---|---|---|---|
| Python anon, last / peak (MiB) | 273 / 299 | 294 / 313 | 274 / 296 | 270 / 292 |
| Python RSS, last / peak (MiB) | 281 / 309 | 301 / 320 | 282 / 304 | 277 / 299 |
| ffmpeg anon, last / peak (MiB) | 68 / 97 | 66 / 97 | 62 / 82 | 62 / 97 |
| ffmpeg RSS, last / peak (MiB) | 262 / 333 | 258 / 334 | 224 / 312 | 224 / 334 |
| Python CPU (ms per segment) | 7.3 | 7.2 | 8.3 | 8.4 |
| ffmpeg CPU (ms per segment) | 22.0 | 22.4 | 24.6 | 24.2 |
| Segments served | 9513 | 9516 | 9511 | 9516 |
| Filler segments | 96 | 93 | 99 | 93 |
| Late or failed warnings | 0 | 0 | 0 | 0 |
| Short segments | 0 | 0 | 0 | 0 |
| Segments at 44.1 kHz | 0 | 0 | 0 | 0 |
| HTTP errors (404s) | 0 | 0 | 0 | 0 |
| Segment latency, p95 / max (s) | 0.004 / 1.07 | 0.004 / 1.06 | 0.004 / 1.24 | 0.004 / 1.13 |

Result: no regression. The Python anon difference in run A (about 20 MiB) does not occur in run B, where "after" is 4 MiB lower. The difference comes from dead httpx responses that only the cyclic GC frees (58-66 MiB at the end of each run).

## Refresh time by priority level

Command: `python bench/cycle_time.py` (30 s timeout). It compares lookups one level at a time ("sequential") with all levels at once ("concurrent", the processor's mode). With 3 or more levels, the concurrent mode makes the median 25-60% shorter and the p90 about 2 times shorter. With 1 level, there is no difference.
