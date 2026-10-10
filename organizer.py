# -*- coding: utf-8 -*-
"""整理逻辑：配置读写，以及归位 / 固定 / 撤销 / 重新排布的实现。

只计算并写回图标坐标，不涉及任何文件操作。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from classify import SYSTEM_GROUP, ZONE_ORDER, build_matcher, scan_entries
from desktop_api import (
    DesktopError,
    auto_arrange_enabled,
    client_size,
    disable_auto_arrange,
    find_desktop_listview,
    read_items,
    set_position,
)
from layout import (
    DEFAULT_CELL,
    DEFAULT_MARGIN,
    DEFAULT_ZONE_GAP,
    Slot,
    compact_plan,
    detect_cell_size,
    keep_plan,
)
from paths import CONFIG_PATH


DEFAULT_CONFIG = {
    "margin": list(DEFAULT_MARGIN),   # 桌面左上留白
    "cell": list(DEFAULT_CELL),       # 图标单元格（宽, 高）
    "zone_gap_cells": DEFAULT_ZONE_GAP,
    "positions": {},                  # 固定位置：key -> [x, y]
    "undo": {},                       # 上次整理前的位置（用于撤销）
    "last_action": "",
}


class Config:
    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self.data = json.loads(json.dumps(DEFAULT_CONFIG))
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for key, value in raw.items():
            if key in ("positions", "undo") and isinstance(value, dict):
                self.data[key] = value
            elif key in ("margin", "cell") and isinstance(value, list) and len(value) == 2:
                self.data[key] = [int(value[0]), int(value[1])]
            elif key == "zone_gap_cells":
                self.data[key] = int(value)
            elif key == "last_action":
                self.data["last_action"] = str(value)

    def save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    @property
    def positions(self) -> dict:
        return self.data["positions"]

    @property
    def margin(self) -> tuple[int, int]:
        return tuple(self.data["margin"])

    @property
    def cell(self) -> tuple[int, int]:
        return tuple(self.data["cell"])

    @property
    def zone_gap_cells(self) -> int:
        return self.data["zone_gap_cells"]


class Organizer:
    """桌面整理核心逻辑。"""

    def __init__(self, config: Config):
        self.cfg = config

    # ---------- 读取 ----------
    def snapshot(self) -> tuple[int, list[Slot], tuple[int, int], bool]:
        """读取桌面图标并归类。"""
        listview = find_desktop_listview()
        icons = read_items(listview)
        matcher = build_matcher(scan_entries())
        seen: dict[str, int] = {}
        slots: list[Slot] = []
        for icon in icons:
            count = seen.get(icon.name, 0) + 1
            seen[icon.name] = count
            key = icon.name if count == 1 else f"{icon.name}#{count}"
            entry = matcher.get(icon.name.lower())
            slots.append(Slot(
                key=key,
                name=icon.name,
                zone=entry.zone if entry else SYSTEM_GROUP,
                index=icon.index,
                x=icon.x,
                y=icon.y,
            ))
        return listview, slots, client_size(listview), auto_arrange_enabled(listview)

    @staticmethod
    def zone_counts(slots: list[Slot]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for slot in slots:
            counts[slot.zone] = counts.get(slot.zone, 0) + 1
        return counts

    def plan(self, mode: str, slots: list[Slot], desktop_size: tuple[int, int]):
        if self.cfg.positions:
            # 已经固定过位置：网格以配置为准，避免被桌面当前的临时摆放带偏
            cell = self.cfg.cell
        else:
            # 首次使用才按现有摆放推断网格
            cell = detect_cell_size(slots, self.cfg.cell)
        planning = compact_plan if mode == "compact" else keep_plan
        return planning(
            slots, self.cfg.positions, cell, desktop_size,
            self.cfg.margin, self.cfg.zone_gap_cells,
        ), cell

    # ---------- 执行 ----------
    def _ordered_moves(
        self,
        slots: list[Slot],
        plan: dict[str, tuple[int, int]],
        cell: tuple[int, int],
        desktop_size: tuple[int, int],
    ) -> list[tuple[int, int, int]]:
        """算出落位顺序（图标序号, x, y）。

        桌面控件不允许把图标放进“已被占用”的格子（会被系统挤到下一格），
        所以先移动目标格已经空出来的图标；遇到互相占位的死循环，
        先把其中一个挪到临时空格，再继续，保证每次目标格都是空的。
        """
        cell_w, cell_h = cell
        desktop_w, desktop_h = desktop_size
        margin_x, margin_y = self.cfg.margin

        current = {slot.key: (slot.x, slot.y) for slot in slots}
        target = {slot.key: (int(plan[slot.key][0]), int(plan[slot.key][1]))
                  for slot in slots if slot.key in plan}
        index_of = {slot.key: slot.index for slot in slots}

        occupied = {(slot.x, slot.y): slot.key for slot in slots}
        target_cells = set(target.values())
        pending = {key: dest for key, dest in target.items() if current.get(key) != dest}

        def take_scratch():
            for y in range(margin_y, desktop_h - cell_h + 1, cell_h):
                for x in range(margin_x, desktop_w - cell_w + 1, cell_w):
                    if (x, y) in occupied or (x, y) in target_cells:
                        continue
                    return (x, y)
            return None

        moves: list[tuple[int, int, int]] = []
        while pending:
            progressed = False
            for key, dest in list(pending.items()):
                if dest in occupied:      # 目标格还被别的图标占着，先跳过
                    continue
                origin = current.get(key)
                if origin is not None and occupied.get(origin) == key:
                    occupied.pop(origin)
                occupied[dest] = key
                current[key] = dest
                moves.append((index_of[key], dest[0], dest[1]))
                pending.pop(key)
                progressed = True
            if not progressed:            # 剩下的互相占位：借一个临时格打破循环
                key = next(iter(pending))
                scratch = take_scratch()
                if scratch is None:
                    break
                origin = current.get(key)
                if origin is not None and occupied.get(origin) == key:
                    occupied.pop(origin)
                occupied[scratch] = key
                current[key] = scratch
                moves.append((index_of[key], scratch[0], scratch[1]))
        return moves

    def apply(self, mode: str = "keep", dry_run: bool = False) -> str:
        listview, slots, desktop_size, auto_on = self.snapshot()
        if not slots:
            raise DesktopError("桌面上没有图标")
        plan, cell = self.plan(mode, slots, desktop_size)
        moves = [slot for slot in slots if plan.get(slot.key) != (slot.x, slot.y)]

        if dry_run:
            return self._describe(mode, slots, cell, auto_on, 0, applied=False)

        auto_changed = disable_auto_arrange(listview)
        self.cfg.data["undo"] = {slot.key: [slot.x, slot.y] for slot in slots}

        for index, x, y in self._ordered_moves(slots, plan, cell, desktop_size):
            set_position(listview, index, x, y)

        # 回读校验，确认位置真的生效（Explorer 移动图标带滑动动画，稍等一下再读，否则会误判）
        time.sleep(0.5)
        verified = {icon.index: icon for icon in read_items(listview)}
        mismatched = 0
        for slot in slots:
            target = plan.get(slot.key)
            icon = verified.get(slot.index)
            if target is None or icon is None:
                continue
            if (icon.x, icon.y) != target:
                mismatched += 1

        for slot in slots:
            target = plan.get(slot.key)
            if target is not None:
                self.cfg.positions[slot.key] = [target[0], target[1]]
        self.cfg.data["cell"] = [cell[0], cell[1]]
        self.cfg.data["last_action"] = mode
        self.cfg.save()

        text = self._describe(mode, slots, cell, auto_on, len(moves), applied=True)
        if auto_changed:
            text += "\n已顺带关闭桌面的“自动排列图标”。"
        if mismatched:
            text += f"\n注意：有 {mismatched} 个图标位置未生效，请检查桌面的“自动排列图标”。"
        return text

    def undo(self) -> str:
        undo = self.cfg.data.get("undo") or {}
        if not undo:
            return "没有可以撤销的整理记录。"
        listview, slots, _size, _auto = self.snapshot()
        by_key = {slot.key: slot for slot in slots}
        restored = 0
        for key, position in undo.items():
            slot = by_key.get(key)
            if slot is None:
                continue
            set_position(listview, slot.index, int(position[0]), int(position[1]))
            self.cfg.positions.pop(key, None)
            restored += 1
        self.cfg.data["undo"] = {}
        self.cfg.save()
        return f"已把 {restored} 个图标恢复到整理之前的位置。"

    def fix_current(self) -> str:
        """把桌面此刻的图标位置全部记为固定布局。"""
        listview, slots, _size, auto_on = self.snapshot()
        if not slots:
            raise DesktopError("桌面上没有图标")
        auto_changed = disable_auto_arrange(listview)
        self.cfg.data["positions"] = {slot.key: [slot.x, slot.y] for slot in slots}
        self.cfg.data["last_action"] = "fix"
        self.cfg.save()
        text = (f"已把当前 {len(slots)} 个图标的位置固定下来。\n"
                "以后不管怎么拖乱，点“一键整理”就会回到这个布局。")
        if auto_changed or auto_on:
            text += "\n已同时关闭桌面的“自动排列图标”。"
        return text

    def unfix(self) -> str:
        """解除固定：清掉位置记录（图标保持原地不动）。"""
        count = len(self.cfg.data["positions"])
        self.cfg.data["positions"] = {}
        self.cfg.data["last_action"] = "unfix"
        self.cfg.save()
        if not count:
            return "当前没有固定任何位置。"
        return (f"已解除固定，清除 {count} 个图标的位置记录。\n"
                "图标保持现在的样子不动；想重新按分区摆可以点“一键整理”。")

    def _describe(self, mode, slots, cell, auto_on, moved, applied: bool) -> str:
        """结果文案（提示浮层用，控制在 2~3 行）。"""
        counts = self.zone_counts(slots)
        if applied:
            head = f"已整理 {len(slots)} 个图标，移动 {moved} 个。"
        else:
            head = f"预览：桌面共 {len(slots)} 个图标。"
        lines = [head]
        stats = [f"{zone} {counts[zone]}" for zone in ZONE_ORDER if counts.get(zone)]
        if stats:
            lines.append("　｜　".join(stats))
        if not applied:
            lines.append(
                f"网格 {cell[0]}×{cell[1]}px　自动排列：{'已关闭' if not auto_on else '已开启'}")
        return "\n".join(lines)
