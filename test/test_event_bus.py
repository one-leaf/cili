"""测试全局事件总线（EventBus）——发布订阅、过滤、并发、积压丢弃。"""

import queue
from concurrent.futures import ThreadPoolExecutor

from core.event_bus import EventBus, get_event_bus


class TestEventBus:
    """事件总线单元测试"""

    def test_subscribe_publish_unsubscribe(self):
        """基本流：订阅后收到事件，退订后不再收到"""
        bus = EventBus()
        q = queue.Queue()
        bus.subscribe(q)
        bus.publish({"type": "text", "content": "hello"})
        assert q.get(timeout=1)["type"] == "text"
        bus.unsubscribe(q)
        bus.publish({"type": "text", "content": "after"})
        assert q.empty()

    def test_workspace_session_filter(self):
        """按 workspace_uuid / session_id 过滤"""
        bus = EventBus()
        q_ws1_s1 = queue.Queue()
        bus.subscribe(q_ws1_s1, "ws1", "s1")
        q_all = queue.Queue()
        bus.subscribe(q_all)
        q_ws2 = queue.Queue()
        bus.subscribe(q_ws2, "ws2")

        bus.publish({"type": "text", "workspace_uuid": "ws1", "session_id": "s1", "content": "a"})
        bus.publish({"type": "text", "workspace_uuid": "ws1", "session_id": "s2", "content": "b"})
        bus.publish({"type": "text", "workspace_uuid": "ws2", "session_id": "s1", "content": "c"})

        # 双过滤订阅者只收 ws1/s1
        assert q_ws1_s1.get(timeout=1)["content"] == "a"
        assert q_ws1_s1.empty()
        # 无过滤订阅者收到全部
        contents = sorted(q_all.get(timeout=1)["content"] for _ in range(3))
        assert contents == ["a", "b", "c"]
        # 单过滤订阅者只收 ws2
        assert q_ws2.get(timeout=1)["content"] == "c"
        assert q_ws2.empty()

    def test_publish_no_subscriber(self):
        """无订阅者时 publish 无副作用"""
        bus = EventBus()
        bus.publish({"type": "text", "workspace_uuid": "ws1", "session_id": "s1"})

    def test_concurrent_publish(self):
        """并发 publish 不丢事件（Lock 保护 + put_nowait）"""
        bus = EventBus()
        q = queue.Queue()
        bus.subscribe(q)
        N = 200

        def worker(i):
            bus.publish({"type": "text", "workspace_uuid": "ws1", "session_id": "s1", "i": i})

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(worker, range(N)))

        received = {q.get(timeout=2)["i"] for _ in range(N)}
        assert len(received) == N

    def test_queue_full_discard(self):
        """订阅者积压满时静默丢弃，不阻塞发布线程"""
        bus = EventBus()
        q = queue.Queue(maxsize=2)
        bus.subscribe(q)
        for _ in range(2):
            bus.publish({"type": "text", "workspace_uuid": "ws1", "session_id": "s1"})
        # 溢出事件静默丢弃（不抛异常）
        bus.publish({"type": "text", "workspace_uuid": "ws1", "session_id": "s1"})
        assert q.qsize() == 2

    def test_get_event_bus_singleton(self):
        """模块级单例"""
        assert get_event_bus() is get_event_bus()
