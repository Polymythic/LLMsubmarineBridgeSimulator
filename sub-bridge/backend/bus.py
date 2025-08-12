import asyncio
from collections import defaultdict
from typing import Any, AsyncIterator, Dict, List, Optional


class AsyncTopicBroker:
    def __init__(self) -> None:
        self._topics: Dict[str, List[asyncio.Queue]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def publish(self, topic: str, message: Any) -> None:
        async with self._lock:
            queues = list(self._topics.get(topic, []))
        for q in queues:
            if not q.full():
                q.put_nowait(message)

    async def subscribe(self, topic: str, max_queue: int = 100) -> AsyncIterator[Any]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue)
        async with self._lock:
            self._topics[topic].append(queue)
        try:
            while True:
                message = await queue.get()
                yield message
        finally:
            async with self._lock:
                if queue in self._topics[topic]:
                    self._topics[topic].remove(queue)

    async def next_message(self, topic: str, timeout: Optional[float] = None) -> Any:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        async with self._lock:
            self._topics[topic].append(queue)
        try:
            return await asyncio.wait_for(queue.get(), timeout=timeout)
        finally:
            async with self._lock:
                if queue in self._topics[topic]:
                    self._topics[topic].remove(queue)


BUS = AsyncTopicBroker()
