"""Keep SSE connections alive during retrieval and upstream model waits."""
from __future__ import annotations

import asyncio
from contextlib import suppress


async def heartbeat_stream(source, interval=5.0):
    """Forward without buffering tokens; cancel pending work on disconnect.

    A heartbeat must not cancel the upstream iterator: wait_for(anext(...))
    would close a slow model/retrieval operation on each timer tick.
    """
    queue = asyncio.Queue(maxsize=1)
    finished = object()

    async def produce():
        try:
            async for item in source:
                await queue.put(item)
        except Exception as exc:
            await queue.put(exc)
        finally:
            await source.aclose()

    task = asyncio.create_task(produce())
    pending = None
    try:
        while True:
            pending = asyncio.create_task(queue.get())
            ready, _ = await asyncio.wait({pending, task}, timeout=interval,
                                         return_when=asyncio.FIRST_COMPLETED)
            while not ready:
                yield ": keep-alive\n\n"
                ready, _ = await asyncio.wait({pending, task}, timeout=interval,
                                             return_when=asyncio.FIRST_COMPLETED)
            if pending.done():
                item = pending.result()
            elif not queue.empty():
                item = queue.get_nowait()
                pending.cancel()
            else:
                # Propagate a producer cancellation/failure, if any.
                task.result()
                item = finished
                pending.cancel()
            if item is finished:
                break
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        if pending is not None:
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

