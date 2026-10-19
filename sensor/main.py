from AlgorithmImports import *

import time

from watch_core import FORMAT_VERSION, Walker, encode, peek, scan_dataset, tail_facts


def _zip_lines(path):
    """First entry of a zip through LEAN's own reader (project files may not import compression packages)."""
    return list(Compression.read_lines(path))


class DatasetWatch(QCAlgorithm):
    """Sensor for dataset-monitor: reads the published data folder and logs facts about every dataset.

    Backtest mode is a sweep: the work is spread over simulated days so no single time step runs long, and the
    result goes out through the log, a few chunks per day, because a backtest gets about 10 kB of log per day and
    has no writable store. dataset-monitor reads the log back over the API, decodes it and does the judging.
    """

    # Folders under the data folder to sweep, from the project parameter "roots" (comma-separated).
    DEFAULT_ROOTS = "alternative,equity/usa/universes/etf"
    # Seconds of scanning per simulated day; well under the per-step limits of the engine.
    BUDGET = 120
    CHUNKS_PER_DAY = 3

    def initialize(self):
        self.set_start_date(2024, 1, 1)
        self.set_end_date(2025, 12, 31)
        self.set_cash(100000)
        self.add_equity("SPY", Resolution.DAILY)  # a clock; nothing is traded

        self._root = Globals.data_folder
        roots = (self.get_parameter("roots") or self.DEFAULT_ROOTS).split(",")
        self._only = [p for p in (self.get_parameter("only") or "").split(",") if p]
        self._tails = [p.strip() for p in (self.get_parameter("tails") or "").split(",") if p.strip()]
        self._peeks = [p.strip() for p in (self.get_parameter("peek") or "").split(",") if p.strip()]
        self._started = time.time()
        self._walker = Walker(self._root, [r.strip() for r in roots if r.strip()])
        self._queue = None
        self._facts, self._chunks = [], None
        self.schedule.on(self.date_rules.every_day("SPY"), self.time_rules.after_market_open("SPY", 1), self.step)

    def step(self):
        if self._queue is None:
            if not self._walker.walk(self.BUDGET):
                return
            self._queue = list(self._walker.found)
            if self._only:
                self._queue = [p for p in self._queue if any(f"/{o}" in p.replace("\\", "/") for o in self._only)]
            self.log(f"DSW-START dirs={len(self._queue)} visited={self._walker.visited}")
            return
        if self._queue:
            deadline = time.time() + self.BUDGET
            while self._queue and time.time() < deadline:
                path = self._queue.pop()
                try:
                    self._facts.append(scan_dataset(path, self._root, deadline=deadline + 60, zip_reader=_zip_lines))
                except Exception as error:
                    self._facts.append({"p": path.replace(self._root, "").strip("/\\"), "err": str(error)[:200]})
            return
        if self._chunks is None:
            tails = {p: tail_facts(self._root, p, zip_reader=_zip_lines) for p in self._tails}
            payload = {"v": FORMAT_VERSION, "at": int(time.time()), "seconds": int(time.time() - self._started),
                       "visited": self._walker.visited, "capped": self._walker.visited >= self._walker.max_dirs,
                       "datasets": self._facts, "tails": tails,
                       "peek": {p: peek(self._root, p) for p in self._peeks}}
            self._chunks = encode(payload, chunk_chars=2800)
            self.log(f"DSW-DONE datasets={len(self._facts)} chunks={len(self._chunks)}")
        for _ in range(self.CHUNKS_PER_DAY):
            if not self._chunks:
                self.log("DSW-END")
                self.quit()
                return
            self.log(self._chunks.pop(0))
