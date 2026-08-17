import logging
import queue
import shutil
import threading

from jo_web.config import WebConfig
from jo_web.registry import COMPLETE, RunRegistry, orphan_directories
from jo_web.service import PipelineService

LOGGER = logging.getLogger(__name__)

SHUTDOWN = None
JANITOR_INTERVAL_SECONDS = 300.0
JOIN_TIMEOUT_SECONDS = 30.0


class RunWorker:
    def __init__(self, config: WebConfig, registry: RunRegistry, service: PipelineService):
        self.config = config
        self.registry = registry
        self.service = service
        self.queue = queue.Queue()
        self._thread = threading.Thread(target=self._drain, name="jo-web-worker", daemon=True)

    def start(self):
        self._thread.start()
        LOGGER.info("run worker started")

    def stop(self):
        self.queue.put(SHUTDOWN)
        self._thread.join(timeout=JOIN_TIMEOUT_SECONDS)
        LOGGER.info("run worker stopped")

    def submit(self, run_id: str):
        self.queue.put(run_id)
        LOGGER.info(f"{run_id}: submitted to the worker queue at depth {self.queue.qsize()}")

    def depth(self) -> int:
        return self.queue.qsize()

    def _drain(self):
        while True:
            run_id = self.queue.get()
            if run_id is SHUTDOWN:
                LOGGER.info("run worker draining complete, thread exiting")
                return
            self._process(run_id)

    def _process(self, run_id: str):
        if self.registry.get(run_id) is None:
            LOGGER.info(f"{run_id}: dropped from the queue, run no longer registered")
            return

        try:
            self.service.execute(run_id)
        except Exception as error:
            self.registry.set_failed(run_id, f"{type(error).__name__}: {error}")
            LOGGER.exception(f"{run_id}: run failed during processing")
            return

        state = self.registry.get(run_id)
        total = state.total if state else 0
        self.registry.set_stage(run_id, COMPLETE, total, total)
        LOGGER.info(f"{run_id}: run complete")


class RunJanitor:
    def __init__(self, config: WebConfig, registry: RunRegistry):
        self.config = config
        self.registry = registry
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._sweep_forever, name="jo-web-janitor", daemon=True)

    def start(self):
        self._thread.start()
        LOGGER.info(f"run janitor started with a {self.config.run_ttl_minutes} minute retention window")

    def stop(self):
        self._stopped.set()
        self._thread.join(timeout=JOIN_TIMEOUT_SECONDS)
        LOGGER.info("run janitor stopped")

    def sweep(self):
        for run_id in self.registry.expired():
            self.registry.delete(run_id)

        known = {state.run_id for state in self.registry.snapshot()}
        for path in orphan_directories(self.config.runs_dir, known):
            shutil.rmtree(path, ignore_errors=True)
            LOGGER.info(f"{path.name}: orphan run directory removed")

    def _sweep_forever(self):
        while not self._stopped.wait(JANITOR_INTERVAL_SECONDS):
            self.sweep()
        LOGGER.info("run janitor thread exiting")
