# -*- coding: utf-8 -*-
"""分区布局计算：算出每个桌面图标应该摆在哪个位置。

两种模式：
  - keep（一键整理）：已固定的图标位置不动，只给新图标在对应分区里补空位
  - compact（重新排布）：按分区顺序整体紧凑重排（会重算所有位置）

位置单位都是桌面像素，与 explorer 桌面控件一致。
"""
from __future__ import annotations

from dataclasses import dataclass

from classify import ZONE_ORDER

DEFAULT_MARGIN = (13, 2)      # 桌面左上留白
DEFAULT_CELL = (75, 100)      # 图标单元格（宽, 高）
DEFAULT_ZONE_GAP = 1          # 分区之间空几列


@dataclass
class Slot:
    """一个桌面图标。"""

    key: str      # 稳定标识（名称，重名时加序号）
    name: str     # 桌面显示名
    zone: str     # 所属分区
    index: int    # 桌面控件里的序号
    x: int        # 当前横坐标
    y: int        # 当前纵坐标


def rows_per_column(desktop_h: int, margin_y: int, cell_h: int) -> int:
    return max(1, (desktop_h - margin_y * 2) // cell_h)


def _common_step(values: list[int], low: int = 36, high: int = 220) -> int | None:
    """取一组坐标里最常见的间隔（用于推断图标网格尺寸）。"""
    unique = sorted(set(values))
    if len(unique) < 2:
        return None
    counts: dict[int, int] = {}
    for first, second in zip(unique, unique[1:]):
        step = second - first
        if low <= step <= high:
            counts[step] = counts.get(step, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda item: item[1])[0]


def detect_cell_size(slots: list[Slot], fallback: tuple[int, int] = DEFAULT_CELL) -> tuple[int, int]:
    """从现有图标位置推断网格尺寸，推断不出来就用默认值。"""
    if not slots:
        return fallback
    width = _common_step([slot.x for slot in slots]) or fallback[0]
    height = _common_step([slot.y for slot in slots]) or fallback[1]
    return int(width), int(height)


def _remembered(remembered: dict, slot: Slot):
    """取某个图标记住的位置（key 优先，其次按名称）。"""
    position = remembered.get(slot.key)
    if position is None:
        position = remembered.get(slot.name)
    if position is None:
        return None
    try:
        return int(position[0]), int(position[1])
    except (TypeError, ValueError, IndexError):
        return None


def keep_plan(
    slots: list[Slot],
    remembered: dict,
    cell: tuple[int, int],
    desktop_size: tuple[int, int],
    margin: tuple[int, int] = DEFAULT_MARGIN,
    zone_gap_cells: int = DEFAULT_ZONE_GAP,
) -> dict[str, tuple[int, int]]:
    """归位：保留已固定位置，其余图标在所属分区内补空位。"""
    cell_w, cell_h = cell
    desktop_w, desktop_h = desktop_size
    margin_x, margin_y = margin
    rows = rows_per_column(desktop_h, margin_y, cell_h)
    gap = zone_gap_cells * cell_w

    occupied: dict[tuple[int, int], str] = {}
    result: dict[str, tuple[int, int]] = {}

    # 1) 已固定位置优先保留
    for slot in slots:
        position = _remembered(remembered, slot)
        if position is None:
            continue
        x, y = position
        if not (0 <= x <= desktop_w - cell_w and 0 <= y <= desktop_h - cell_h):
            continue
        if (x, y) in occupied:
            continue
        result[slot.key] = (x, y)
        occupied[(x, y)] = slot.key

    # 2) 各分区的起始列：按分区顺序依次往右排，先满足已固定位置，再按当前数量预留列
    zone_start: dict[str, int] = {}
    cursor = margin_x
    for zone in ZONE_ORDER:
        columns = [
            result[slot.key][0]
            for slot in slots
            if slot.zone == zone and slot.key in result
        ]
        count = sum(1 for slot in slots if slot.zone == zone)
        need_columns = (count + rows - 1) // rows if count else 0
        start = min(columns) if columns else cursor
        zone_start[zone] = start
        if need_columns or columns:
            extent_end = start + max(
                need_columns * cell_w,
                (max(columns) + cell_w - start) if columns else 0,
            )
            cursor = max(cursor, extent_end + gap)

    def zone_slots(start_x: int):
        x = max(margin_x, start_x)
        while x + cell_w <= desktop_w:
            for row in range(rows):
                yield (x, margin_y + row * cell_h)
            x += cell_w

    global_slots = [
        (x, margin_y + row * cell_h)
        for row in range(rows)
        for x in range(margin_x, desktop_w - cell_w + 1, cell_w)
    ]

    # 3) 新图标补位：先在分区里找空位，分区满了再全桌面找第一个空位
    for zone in ZONE_ORDER:
        pending = [slot.key for slot in slots if slot.zone == zone and slot.key not in result]
        for key in pending:
            placed = False
            for position in zone_slots(zone_start[zone]):
                if position not in occupied:
                    result[key] = position
                    occupied[position] = key
                    placed = True
                    break
            if placed:
                continue
            for position in global_slots:
                if position not in occupied:
                    result[key] = position
                    occupied[position] = key
                    break
    return result


def compact_plan(
    slots: list[Slot],
    remembered: dict,
    cell: tuple[int, int],
    desktop_size: tuple[int, int],
    margin: tuple[int, int] = DEFAULT_MARGIN,
    zone_gap_cells: int = DEFAULT_ZONE_GAP,
) -> dict[str, tuple[int, int]]:
    """重新排布：按分区顺序从左到右紧凑重排所有图标。"""
    cell_w, cell_h = cell
    desktop_w, desktop_h = desktop_size
    margin_x, margin_y = margin
    gap = zone_gap_cells * cell_w

    grouped: dict[str, list[Slot]] = {zone: [] for zone in ZONE_ORDER}
    for slot in slots:
        grouped.setdefault(slot.zone, []).append(slot)

    # 分区内顺序：已固定位置的按原本的上→下、左→右，其余按名称排在后
    for group in grouped.values():
        group.sort(key=lambda slot: (
            (_remembered(remembered, slot) or (10 ** 6, 10 ** 6))[1],
            (_remembered(remembered, slot) or (10 ** 6, 10 ** 6))[0],
            slot.name.lower(),
        ))

    result: dict[str, tuple[int, int]] = {}
    band_y = margin_y
    cursor_x = margin_x
    rows = rows_per_column(desktop_h, margin_y, cell_h)

    for zone in ZONE_ORDER:
        group = grouped.get(zone, [])
        if not group:
            continue
        index = 0
        while index < len(group):
            if cursor_x + cell_w > desktop_w - margin_x:
                band_y += rows * cell_h + cell_h
                cursor_x = margin_x
                rows = max(1, (desktop_h - band_y - margin_y) // cell_h)
                if band_y + cell_h > desktop_h:  # 极端兜底：从左上重排
                    band_y, cursor_x = margin_y, margin_x
                    rows = rows_per_column(desktop_h, margin_y, cell_h)
            available = max(1, (desktop_w - margin_x - cursor_x + cell_w) // cell_w)
            take = min(len(group) - index, available * rows)
            if take <= 0:
                break
            for offset in range(take):
                column, row = divmod(offset, rows)
                result[group[index + offset].key] = (
                    cursor_x + column * cell_w,
                    band_y + row * cell_h,
                )
            index += take
            used_columns = (take + rows - 1) // rows
            cursor_x += used_columns * cell_w + gap
    return result