"""The current managed Run, visible at model and tool effect boundaries."""
from contextvars import ContextVar


current_run = ContextVar("current_run", default=None)


def track_session(session_id):
    lease = current_run.get()
    if lease is not None:
        lease.link(session_id=session_id)


def track_checkpoint(checkpoint_id):
    lease = current_run.get()
    if lease is not None:
        lease.link(checkpoint_id=checkpoint_id)
