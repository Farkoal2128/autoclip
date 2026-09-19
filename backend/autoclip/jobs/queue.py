"""In-process job queue.

One video is processed at a time, FIFO. That's a deliberate constraint rather
than a limitation: transcription, face detection, and encoding each want the
whole GPU, and running two jobs concurrently on a 6 GB card makes both slower
than running them in sequence.

The queue is durable because it lives in SQLite — a crash mid-job leaves the row
as ``running``, and startup requeues it so the resumable stages pick up where
they left off.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from dataclasses import dataclass

from ..db import store
from ..db.models import Job
from ..pipeline.runner import JobCancelled, PipelineRunner
from .events import Event, broker, progress_event

log = logging.getLogger(__name__)


@dataclass
class QueueStatus:
    running_job_id: str | None
    queued: int


class JobQueue:
    """Runs queued jobs one at a time."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        # Created in start(), not here: an asyncio.Event binds to the loop that
        # is running when it is constructed, and this object is a module-level
        # singleton built at import time. Constructing it eagerly makes every
        # subsequent event loop in the process fail with "bound to a different
        # event loop".
        self._wakeup: asyncio.Event | None = None
        self._running_job_id: str | None = None
        self._cancel_requested: set[str] = set()
        self._stopping = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Start the worker and requeue anything left running by a crash."""
        self._requeue_interrupted()
        self._stopping = False
        self._wakeup = asyncio.Event()
        self._task = asyncio.create_task(self._worker(), name="autoclip-job-queue")
        self.notify()

    async def stop(self) -> None:
        """Stop the worker without abandoning its blocking pipeline thread."""
        self._stopping = True
        if self._running_job_id is not None:
            self._cancel_requested.add(self._running_job_id)
        self.notify()

        # FFmpeg may be blocked in communicate() inside the worker thread. End
        # those children first so the runner can observe cancellation and unwind.
        from ..pipeline import ffmpeg

        await asyncio.to_thread(ffmpeg.terminate_all)

        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=20.0)
            except TimeoutError:
                log.warning("Job worker did not stop within 20 seconds; cancelling task wrapper.")
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
            self._task = None
        self._wakeup = None

    def notify(self) -> None:
        """Wake the worker — call after enqueuing a job.

        A no-op when the worker isn't running, so enqueuing still works in a
        process started without one.
        """
        if self._wakeup is not None:
            self._wakeup.set()

    def _requeue_interrupted(self) -> None:
        interrupted = store.list_jobs(limit=100, status="running")
        for job in interrupted:
            log.info("Requeuing job %s, interrupted by a restart.", job.id)
            store.update_job(job.id, status="queued", error=None)

    # -- control -----------------------------------------------------------

    def cancel(self, job_id: str) -> bool:
        """Request cancellation. Returns True if the job was running or queued."""
        job = store.get_job(job_id)
        if job is None or job.status in ("done", "failed", "cancelled"):
            return False

        self._cancel_requested.add(job_id)
        if job.status == "queued":
            store.update_job(job_id, status="cancelled")
            broker.publish(Event(type="cancelled", job_id=job_id))
        return True

    def cancel_all(self) -> int:
        """Cancel every queued/running job before application shutdown."""
        cancelled = 0
        for job in store.list_jobs(limit=100, status="queued"):
            if self.cancel(job.id):
                cancelled += 1

        if self._running_job_id is not None:
            job_id = self._running_job_id
            if job_id not in self._cancel_requested:
                self._cancel_requested.add(job_id)
                cancelled += 1
            store.update_job(job_id, status="cancelled", error=None)
        self.notify()
        return cancelled

    def is_cancelled(self, job_id: str) -> bool:
        return job_id in self._cancel_requested

    def status(self) -> QueueStatus:
        return QueueStatus(
            running_job_id=self._running_job_id,
            queued=len(store.list_jobs(limit=100, status="queued")),
        )

    # -- worker ------------------------------------------------------------

    async def _worker(self) -> None:
        wakeup = self._wakeup
        assert wakeup is not None

        while not self._stopping:
            job = await asyncio.to_thread(store.next_queued_job)
            if job is None:
                wakeup.clear()
                await wakeup.wait()
                continue

            await self._run_one(job)

    async def _run_one(self, job: Job) -> None:
        source = await asyncio.to_thread(store.get_source, job.source_id)
        if source is None:
            store.update_job(job.id, status="failed", error="Source not found.")
            broker.publish(Event(type="failed", job_id=job.id, data={"error": "Source not found."}))
            return

        self._running_job_id = job.id
        broker.publish(Event(type="started", job_id=job.id))
        log.info("Starting job %s (%s).", job.id, source.title)

        def on_progress(event) -> None:
            broker.publish(progress_event(job.id, event))

        runner = PipelineRunner(
            job,
            source,
            on_progress=on_progress,
            is_cancelled=lambda: self.is_cancelled(job.id),
        )

        try:
            clips = await _run_in_daemon_thread(runner)
        except JobCancelled:
            broker.publish(Event(type="cancelled", job_id=job.id))
            log.info("Job %s cancelled.", job.id)
        except Exception as exc:
            broker.publish(Event(type="failed", job_id=job.id, data={"error": str(exc)}))
            log.exception("Job %s failed.", job.id)
        else:
            broker.publish(Event(type="done", job_id=job.id, data={"clip_count": len(clips)}))
            log.info("Job %s finished with %d clips.", job.id, len(clips))
        finally:
            self._running_job_id = None
            self._cancel_requested.discard(job.id)


async def _run_in_daemon_thread(runner: PipelineRunner):
    """Run one pipeline without letting its thread keep Python alive on quit."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def resolve(result=None, error: Exception | None = None) -> None:
        if loop.is_closed():
            return

        def apply() -> None:
            if future.done():
                return
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(result)

        try:
            loop.call_soon_threadsafe(apply)
        except RuntimeError:
            # The server may already have closed its loop during hard shutdown.
            pass

    def target() -> None:
        try:
            resolve(result=_run_blocking(runner))
        except Exception as exc:
            resolve(error=exc)

    thread = threading.Thread(
        target=target,
        name=f"autoclip-pipeline-{runner.job.id[:8]}",
        daemon=True,
    )
    thread.start()
    return await future


def _run_blocking(runner: PipelineRunner):
    """Run the pipeline in a worker thread with its own event loop.

    The pipeline is overwhelmingly blocking work with one async stage, so it
    gets a private loop rather than competing for the server's.
    """
    return asyncio.run(runner.run())


#: Process-wide queue.
queue = JobQueue()
