"""Refresh cycle time: how long one round of channel lookups takes.

Run from the repo root, inside `nix develop`:

    python bench/cycle_time.py                    # default grid, scaled 100x faster
    python bench/cycle_time.py --channels 16 32 --levels 1 4 --runs 20
    python bench/cycle_time.py --timeout-rate 0.1 --scale 1   # real time
    python bench/cycle_time.py --timeouts 30 15 10 8          # compare lookup timeouts

A fake lookup stands in for streamlink. It takes 0.5-3 s (90%), 3-10 s (5%) or
hangs for --hang seconds and then fails (5%, --timeout-rate); 60% of channels are
live. A lookup that runs past the timeout fails at the timeout. Channels spread
evenly over the priority levels. Three ways to run the lookups, all bounded by the
processor's concurrency limit (4):

- sequential: one priority level after another, each waiting for the last.
- concurrent: every level at once, started in priority order.
- processor: StreamProcessor.process_stream_groups() with the fake fetcher, so
  the number follows whatever the processor does now.

--timeouts runs only the processor, once per timeout, with the real
StreamlinkFetcher around a fake blocking lookup. Its executor threads run every
lookup to the end, as streamlink does, so a hung lookup keeps its thread after
the timeout; --workers resizes the thread pool. Each cell adds the mean count of
failed lookups per refresh, and of those the lookups that would have succeeded
with no timeout ("cut").

Each run draws new latencies; every mode sees the same draws. Times print in
real seconds (scaled back), as the median and 90th percentile over --runs.
"""

import argparse
import asyncio
import random
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import structlog

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from quadlink.config.models import Config, StreamGroup  # noqa: E402
from quadlink.stream.fetcher import LookupFailed  # noqa: E402
from quadlink.stream.processor import StreamProcessor  # noqa: E402
from quadlink.types import ChannelState, Metadata, Stream, channel_login  # noqa: E402

CONCURRENCY = 4

Draws = dict[str, tuple[float, bool]]


def draw(rng: random.Random, hang: float, timeout_rate: float) -> tuple[float, bool]:
    """(latency, live) for one lookup; a lookup that takes `hang` seconds fails."""
    roll = rng.random()
    if roll < timeout_rate:
        return hang, False
    if roll < timeout_rate + 0.05:
        latency = rng.uniform(3, 10)
    else:
        latency = rng.uniform(0.5, 3)
    return latency, rng.random() < 0.6


def make_config(channels: int, levels: int) -> Config:
    priorities: dict[int, list[StreamGroup]] = {}
    for i in range(channels):
        level = 100 - 10 * (i % levels)
        priorities.setdefault(level, [StreamGroup(urls=[])])[0].urls.append(f"ch{i:03d}")
    return Config(mode="local", rulesets=[], priorities=priorities)


def result(url: str, draws: Draws, hang: float) -> Stream | None:
    channel = channel_login(url)
    latency, live = draws[channel]
    if latency >= hang:
        raise LookupFailed("hung")
    if not live:
        return None
    return Stream(url=url, metadata=Metadata(author=channel, category="Games", title="t"))


class FakeFetcher:
    def __init__(self, draws: Draws, scale: float, timeout: float, hang: float):
        self.draws = draws
        self.scale = scale
        self.timeout = timeout
        self.hang = hang

    async def fetch_stream_info(self, url: str) -> Stream | None:
        latency, _ = self.draws[channel_login(url)]
        await asyncio.sleep(min(latency, self.timeout) * self.scale)
        if latency > self.timeout:
            raise LookupFailed("timed out")
        return result(url, self.draws, self.hang)


class BlockingLookup:
    """Stands in for StreamlinkFetcher._fetch_stream_info_sync: sleeps in its thread."""

    def __init__(self, draws: Draws, scale: float, hang: float):
        self.draws = draws
        self.scale = scale
        self.hang = hang

    def __call__(self, url: str) -> Stream | None:
        latency, _ = self.draws[channel_login(url)]
        time.sleep(latency * self.scale)
        return result(url, self.draws, self.hang)


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


def draws_for(run: int, channels: int, args: argparse.Namespace) -> Draws:
    rng = random.Random(run)
    return {f"ch{i:03d}": draw(rng, args.hang, args.timeout_rate) for i in range(channels)}


def cell(times: list[float]) -> str:
    t = sorted(times)
    p90 = t[min(len(t) - 1, int(0.9 * len(t)))]
    return f"{statistics.median(t):6.1f}s p90 {p90:6.1f}s"


async def measure(args: argparse.Namespace) -> None:
    print(
        f"timeout {args.timeout:g}s, hang rate {args.timeout_rate:g}, "
        f"concurrency {CONCURRENCY}, {args.runs} runs, scale {args.scale:g}"
    )
    print(f"{'channels':>8} {'levels':>6}  " + "  ".join(f"{m:>18}" for m in MODES))
    for channels in args.channels:
        for levels in args.levels:
            config = make_config(channels, levels)
            times: dict[str, list[float]] = {m: [] for m in MODES}
            for run in range(args.runs):
                draws = draws_for(run, channels, args)
                for name, mode in MODES.items():
                    fetcher = FakeFetcher(draws, args.scale, args.timeout, args.hang)
                    start = time.perf_counter()
                    await mode(config, fetcher)
                    times[name].append((time.perf_counter() - start) / args.scale)
            cells = [cell(times[name]) for name in MODES]
            print(f"{channels:>8} {levels:>6}  " + "  ".join(f"{c:>18}" for c in cells))


async def refresh(
    config: Config, lookup: BlockingLookup, timeout: float, workers: int | None
) -> tuple[float, int]:
    """Seconds one refresh takes, and how many lookups failed."""
    proc = StreamProcessor(config, max_concurrent=CONCURRENCY, lookup_timeout=timeout)
    proc.fetcher._fetch_stream_info_sync = lookup  # type: ignore[method-assign]
    if workers is not None:
        proc.fetcher.executor.shutdown()
        proc.fetcher.executor = ThreadPoolExecutor(max_workers=workers)
    start = time.perf_counter()
    await proc.process_stream_groups()
    elapsed = time.perf_counter() - start
    failed = sum(1 for s in proc.statuses() if s.state is ChannelState.LOOKUP_FAILED)
    proc.close()
    return elapsed, failed


async def compare_timeouts(args: argparse.Namespace) -> None:
    print(
        f"hang {args.hang:g}s, hang rate {args.timeout_rate:g}, concurrency {CONCURRENCY}, "
        f"{args.runs} runs, scale {args.scale:g}, executor workers {args.workers or 'default'}; "
        "refresh median and p90, "
        "mean failed lookups per refresh (cut: would have succeeded)"
    )
    heads = [f"{t:g}s timeout" for t in args.timeouts]
    print(f"{'channels':>8} {'levels':>6}  " + "  ".join(f"{h:>34}" for h in heads))
    for channels in args.channels:
        for levels in args.levels:
            config = make_config(channels, levels)
            cells = []
            for timeout in args.timeouts:
                times: list[float] = []
                failed = cut = 0
                for run in range(args.runs):
                    draws = draws_for(run, channels, args)
                    lookup = BlockingLookup(draws, args.scale, args.hang)
                    elapsed, n = await refresh(config, lookup, timeout * args.scale, args.workers)
                    times.append(elapsed / args.scale)
                    failed += n
                    cut += sum(1 for lat, _ in draws.values() if timeout < lat < args.hang)
                runs = args.runs
                cells.append(f"{cell(times)} fail {failed / runs:3.1f} cut {cut / runs:3.1f}")
            print(f"{channels:>8} {levels:>6}  " + "  ".join(f"{c:>34}" for c in cells))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--channels", type=int, nargs="+", default=[8, 16, 32, 64])
    parser.add_argument("--levels", type=int, nargs="+", default=[1, 3, 5])
    parser.add_argument("--runs", type=int, default=15)
    parser.add_argument("--timeout", type=float, default=30.0, help="lookup timeout, seconds")
    parser.add_argument("--timeout-rate", type=float, default=0.05, help="share of hung lookups")
    parser.add_argument("--hang", type=float, default=40.0, help="hung lookup length, seconds")
    parser.add_argument("--timeouts", type=float, nargs="+", help="compare these lookup timeouts")
    parser.add_argument("--workers", type=int, help="executor threads with --timeouts")
    parser.add_argument("--scale", type=float, default=0.01, help="sleep factor")
    args = parser.parse_args()
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(40))
    asyncio.run(compare_timeouts(args) if args.timeouts else measure(args))


if __name__ == "__main__":
    main()
