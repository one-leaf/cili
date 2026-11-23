"""浏览器标签页池：页码分配/回收、活跃页与空闲过期判定。"""

from __future__ import annotations


class TabPool:
    def __init__(self) -> None:
        # tab_index → (page, last_active_ts)
        self.pages: dict[int, tuple] = {}
        self.next_index = 1
        self.free: list[int] = []
        self.active: int | None = None

    def get(self, tab_index: int):
        entry = self.pages.get(tab_index)
        return entry[0] if entry else None

    def entry(self, tab_index: int):
        return self.pages.get(tab_index)

    def touch(self, tab_index: int, page, now: float) -> None:
        self.pages[tab_index] = (page, now)
        self.active = tab_index

    def allocate(self) -> int:
        if self.free:
            return self.free.pop()
        index = self.next_index
        self.next_index += 1
        return index

    def release(self, tab_index: int) -> None:
        self.pages.pop(tab_index, None)
        if tab_index not in self.free:
            self.free.append(tab_index)
        if self.active == tab_index:
            self.active = None

    def expired(self, now: float, idle_timeout: float) -> list[tuple[int, object]]:
        """超过空闲时限且非活跃的页。"""
        return [
            (idx, page) for idx, (page, ts) in self.pages.items()
            if now - ts > idle_timeout and idx != self.active
        ]

    def clear(self, *, reset_indices: bool = False) -> None:
        self.pages.clear()
        self.active = None
        self.free.clear()
        if reset_indices:
            self.next_index = 1
