import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Event, Lock

class ResearchCancelled(RuntimeError):
    pass


@dataclass
class CancellationToken:
    event: Event = field(default_factory=Event)
    lock: Lock = field(default_factory=Lock)
    loop: asyncio.AbstractEventLoop | None = None
    task: asyncio.Task | None = None

    def cancel(self):
        self.event.set()
        with self.lock:
            if self.loop is not None and self.task is not None:
                self.loop.call_soon_threadsafe(self.task.cancel)

    def check(self):
        if self.event.is_set():
            raise ResearchCancelled('research_cancelled')

    def bind_task(self, loop, task):
        with self.lock:
            self.loop, self.task = loop, task
            if self.event.is_set():
                loop.call_soon_threadsafe(task.cancel)

    def clear_task(self):
        with self.lock:
            self.loop = self.task = None


current_token: ContextVar[CancellationToken | None] = ContextVar('research_cancel_token', default=None)
