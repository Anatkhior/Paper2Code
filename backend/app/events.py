"""事件总线 + SSE 序列化。

对应 docs/v0-spec.md §9 的事件契约。

两个必须做到的点：
1. 每条事件有单调递增的 id，客户端可以带 Last-Event-ID 重连，不丢事件。
2. 事件同时落到 events.jsonl，所以刷新页面能完整回放（这也是 §8 幂等缓存的基础）。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, AsyncIterator


class RunBus:
    """一个 run 的事件总线（**一个 run 一条，阶段切换复用**）。

    事件 id 在 run 内全局单调递增；bus 构造时会加载 events.jsonl 里的历史，
    从历史最大 id 继续编号，所以前端的 Last-Event-ID 续传跨阶段依然成立。
    （早期版本是"每个阶段一个 bus"，结果阶段切换换掉了总线，
    先连流的浏览器永远收不到新事件——界面上表现为卡在"侦察中"。）
    """

    def __init__(self, run_id: str, jsonl_path: Path) -> None:
        self.run_id = run_id
        self.jsonl_path = jsonl_path
        self._events: list[dict[str, Any]] = load_events(jsonl_path)
        self._subscribers: set[asyncio.Queue[dict[str, Any] | None]] = set()
        self._lock = asyncio.Lock()

    # -- 写入 ---------------------------------------------------------------
    async def emit(self, type_: str, **data: Any) -> dict[str, Any]:
        async with self._lock:
            event = {
                "id": (self._events[-1]["id"] if self._events else 0) + 1,
                "type": type_,
                "ts": time.time(),
                "data": data,
            }
            self._events.append(event)
            with self.jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            for queue in list(self._subscribers):
                queue.put_nowait(event)
        return event

    # -- 读取 ---------------------------------------------------------------
    @property
    def history(self) -> list[dict[str, Any]]:
        return list(self._events)

    async def subscribe(self, last_event_id: int = 0) -> AsyncIterator[dict[str, Any]]:
        """先补发历史（id > last_event_id），再接实时流。

        「已经结束的 run」的收尾不在 bus 里做：run 永不关闭（阶段切换要复用），
        SSE 端点在回放到 run_end 且任务已结束时自己 break（见 main.run_events）。
        """
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            last_sent = last_event_id
            for event in self.history:
                if event["id"] > last_event_id:
                    last_sent = event["id"]
                    yield event
            while True:
                item = await queue.get()
                if item["id"] <= last_sent:
                    continue  # 去重：历史与实时队列可能重叠
                last_sent = item["id"]
                yield item
        finally:
            self._subscribers.discard(queue)


def to_sse(event: dict[str, Any]) -> dict[str, str]:
    """转成 sse-starlette 需要的形状。"""
    return {
        "event": event["type"],
        "id": str(event["id"]),
        "data": json.dumps(event, ensure_ascii=False, default=str),
    }


def load_events(jsonl_path: Path) -> list[dict[str, Any]]:
    """从 events.jsonl 回放（刷新页面 / 缓存命中时用）。"""
    if not jsonl_path.exists():
        return []
    events = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events
