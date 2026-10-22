"""Real lookup latency: time StreamlinkFetcher against Twitch.

Run from the repo root, inside `nix develop` (the shell provides the ttvlol plugin):

    python bench/lookup_latency.py                      # default channels, 5 rounds
    python bench/lookup_latency.py --rounds 3 --concurrency 4 kaicenat xqc

Each round looks up every channel once, at most --concurrency at a time, as the
processor does, with no lookup timeout. The output lists every lookup, then the
latency percentiles per outcome (live, offline, failed) and for all lookups.
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

import structlog

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from quadlink.stream.fetcher import LookupFailed, StreamlinkFetcher  # noqa: E402

# well-known channels across regions; at any hour some are live and some offline
CHANNELS = [
    "kaicenat",
    "xqc",
    "shroud",
    "pokimane",
    "summit1g",
    "lirik",
    "sodapoppin",
    "hasanabi",
    "ninja",
    "timthetatman",
    "asmongold",
    "zackrawrr",
    "caseoh_",
    "jynxzi",
    "ibai",
    "auronplay",
    "rubius",
    "gaules",
    "loltyler1",
    "esl_csgo",
    "riotgames",
    "tarik",
    "squeezie",
    "elraenn",
]


def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


async def measure(args: argparse.Namespace) -> None:
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(40))
    fetcher = StreamlinkFetcher(max_workers=args.concurrency, timeout=None)
    sem = asyncio.Semaphore(args.concurrency)
    samples: list[dict[str, object]] = []

    async def one(rnd: int, channel: str) -> None:
        async with sem:
            start = time.perf_counter()
            try:
                stream = await fetcher.fetch_stream_info(channel)
                outcome = "live" if stream else "offline"
            except LookupFailed as e:
                outcome = f"failed: {e}"
            elapsed = time.perf_counter() - start
        samples.append({"round": rnd, "channel": channel, "s": elapsed, "outcome": outcome})
        print(f"{rnd} {channel:>14} {elapsed:6.2f}s {outcome}", flush=True)

    for rnd in range(args.rounds):
        start = time.perf_counter()
        await asyncio.gather(*(one(rnd, c) for c in args.channels))
        print(f"round {rnd}: {time.perf_counter() - start:.1f}s", flush=True)
        if rnd + 1 < args.rounds:
            await asyncio.sleep(args.pause)
    fetcher.executor.shutdown(wait=False)

    groups: dict[str, list[float]] = {"all": []}
    for s in samples:
        key = str(s["outcome"]).split(":")[0]
        groups.setdefault(key, []).append(float(s["s"]))  # type: ignore[arg-type]
        groups["all"].append(float(s["s"]))  # type: ignore[arg-type]
    print(f"\n{'outcome':>8} {'n':>4} {'p50':>6} {'p90':>6} {'p99':>6} {'max':>6}")
    for key, values in groups.items():
        print(
            f"{key:>8} {len(values):>4} {statistics.median(values):6.2f} "
            f"{pct(values, 0.9):6.2f} {pct(values, 0.99):6.2f} {max(values):6.2f}"
        )
    if args.out:
        Path(args.out).write_text("\n".join(json.dumps(s) for s in samples) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("channels", nargs="*", default=CHANNELS)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--pause", type=float, default=30, help="seconds between rounds")
    parser.add_argument("--out", help="write samples as JSON lines")
    asyncio.run(measure(parser.parse_args()))


if __name__ == "__main__":
    main()
