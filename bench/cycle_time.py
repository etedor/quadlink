"""Refresh cycle time: how long one round of channel lookups takes.

Run from the repo root, inside `nix develop`:

    python bench/cycle_time.py                    # default grid, scaled 100x faster
    python bench/cycle_time.py --channels 16 32 --levels 1 4 --runs 20
    python bench/cycle_time.py --timeout-rate 0.1 --scale 1   # real time

A fake fetcher stands in for streamlink. Each lookup takes 0.5-3 s (90%), 3-10 s
(5%) or hits the lookup timeout (5%, --timeout-rate); 60% of channels are live.
Channels spread evenly over the priority levels. Three ways to run the lookups,
all bounded by the processor's concurrency limit (4):

- sequential: one priority level after another, each waiting for the last.
- concurrent: every level at once, started in priority order.
- processor: StreamProcessor.process_stream_groups() with the fake fetcher, so
  the number follows whatever the processor does now.

Each run draws new latencies; every mode sees the same draws. Times print in
real seconds (scaled back), as the median and 90th percentile over --runs.
"""

import argparse
import asyncio
import random
import statistics
import sys
import time
from pathlib import Path
from unittest.mock import patch

import structlog

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from quadlink.config.models import Config, StreamGroup  # noqa: E402
from quadlink.stream.fetcher import LookupFailed  # noqa: E402
from quadlink.stream.processor import StreamProcessor  # noqa: E402
from quadlink.types import Metadata, Stream, channel_login  # noqa: E402

CONCURRENCY = 4


def draw(rng: random.Random, timeout: float, timeout_rate: float) -> tuple[float, bool, bool]:
    """(latency, timed out, live) for one lookup."""
    roll = rng.random()
    if roll < timeout_rate:
        return timeout, True, False
    if roll < timeout_rate + 0.05:
        latency = rng.uniform(3, 10)
    else:
        latency = rng.uniform(0.5, 3)
    return latency, False, rng.random() < 0.6


def make_config(channels: int, levels: int) -> Config:
    priorities: dict[int, list[StreamGroup]] = {}
    for i in range(channels):
        level = 100 - 10 * (i % levels)
        priorities.setdefault(level, [StreamGroup(urls=[])])[0].urls.append(f"ch{i:03d}")
    return Config(mode="local", rulesets=[], priorities=priorities)


class FakeFetcher:
    def __init__(self, draws: dict[str, tuple[float, bool, bool]], scale: float):
        self.draws = draws
        self.scale = scale

    async def fetch_stream_info(self, url: str) -> Stream | None:
        channel = channel_login(url)
        latency, timed_out, live = self.draws[channel]
        await asyncio.sleep(latency * self.scale)
        if timed_out:
            raise LookupFailed("timed out")
        if not live:
            return None
        return Stream(url=url, metadata=Metadata(author=channel, category="Games", title="t"))


def levels_of(config: Config) -> list[list[str]]:
    return [
        [url for group in config.priorities[p] for url in group.urls]
        for p in sorted(config.priorities, reverse=True)
    ]


async def sequential(config: Config, fetcher: FakeFetcher) -> None:
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(url: str) -> None:
        async with sem:
            try:
                await fetcher.fetch_stream_info(url)
            except LookupFailed:
                pass

    for level in levels_of(config):
        await asyncio.gather(*(one(u) for u in level))


async def concurrent(config: Config, fetcher: FakeFetcher) -> None:
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(url: str) -> None:
        async with sem:
            try:
                await fetcher.fetch_stream_info(url)
            except LookupFailed:
                pass

    await asyncio.gather(*(one(u) for level in levels_of(config) for u in level))


async def processor(config: Config, fetcher: FakeFetcher) -> None:
    with patch("quadlink.stream.processor.StreamlinkFetcher", return_value=fetcher):
        proc = StreamProcessor(config, max_concurrent=CONCURRENCY)
    await proc.process_stream_groups()


MODES = {"sequential": sequential, "concurrent": concurrent, "processor": processor}


async def measure(args: argparse.Namespace) -> None:
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(40))
    print(
        f"timeout {args.timeout:g}s, timeout rate {args.timeout_rate:g}, "
        f"concurrency {CONCURRENCY}, {args.runs} runs, scale {args.scale:g}"
    )
    print(f"{'channels':>8} {'levels':>6}  " + "  ".join(f"{m:>22}" for m in MODES))
    for channels in args.channels:
        for levels in args.levels:
            config = make_config(channels, levels)
            times: dict[str, list[float]] = {m: [] for m in MODES}
            for run in range(args.runs):
                rng = random.Random(run)
                draws = {
                    f"ch{i:03d}": draw(rng, args.timeout, args.timeout_rate)
                    for i in range(channels)
                }
                for name, mode in MODES.items():
                    fetcher = FakeFetcher(draws, args.scale)
                    start = time.perf_counter()
                    await mode(config, fetcher)
                    times[name].append((time.perf_counter() - start) / args.scale)
            cells = []
            for name in MODES:
                t = sorted(times[name])
                p90 = t[min(len(t) - 1, int(0.9 * len(t)))]
                cells.append(f"{statistics.median(t):8.1f}s p90 {p90:7.1f}s")
            print(f"{channels:>8} {levels:>6}  " + "  ".join(f"{c:>22}" for c in cells))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--channels", type=int, nargs="+", default=[8, 16, 32, 64])
    parser.add_argument("--levels", type=int, nargs="+", default=[1, 3, 5])
    parser.add_argument("--runs", type=int, default=15)
    parser.add_argument("--timeout", type=float, default=30.0, help="lookup timeout, seconds")
    parser.add_argument("--timeout-rate", type=float, default=0.05)
    parser.add_argument("--scale", type=float, default=0.01, help="sleep factor")
    asyncio.run(measure(parser.parse_args()))


if __name__ == "__main__":
    main()
