# -*- coding: utf-8 -*-
"""桌面整理：把真实桌面上的图标按分区摆好，并把位置固定住。

- 一键整理：回到固定布局；没固定过的图标在所属分区里自动补位
- 固定当前位置：把桌面此刻的位置记为固定布局（自己拖完图标后点它）
- 解除固定：清掉位置记录，重新自由摆放
- 重新排布：按分区整体紧凑重排（重算所有位置）
- 撤销上次整理：回到整理之前的位置

只调整桌面图标的位置，不会移动、改名、删除任何文件。

用法：
    python main.py             打开工具窗口
    python main.py --boot      开机自启用：静默归位一次后退出
    python main.py --plan      预览两种整理效果（不做任何改动）
    python main.py --apply     一键整理（回到固定布局）
    python main.py --fix       把桌面当前位置固定下来
    python main.py --unfix     解除固定
    python main.py --compact   重新排布
    python main.py --undo      撤销上次整理
"""
from __future__ import annotations

import json
import os
import sys
import time
import winreg
from pathlib import Path

from classify import SYSTEM_GROUP, ZONE_ORDER, build_matcher, scan_entries
from desktop_api import (
    DesktopError,
    auto_arrange_enabled,
    client_size,
    disable_auto_arrange,
    find_desktop_listview,
    make_dpi_aware,
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


def _app_dir() -> Path:
    """程序目录：打包成 exe 后是 exe 所在目录，源码运行时是脚本目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _data_dir() -> Path:
    """配置文件目录：打包后放 %APPDATA%\\桌面整理，避免在桌面等处生成文件。"""
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent
    base = Path(os.environ.get("APPDATA") or Path.home()) / "桌面整理"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path(sys.executable).resolve().parent
    return base


APP_DIR = _app_dir()
DATA_DIR = _data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
# 图标可能在打包资源目录（_MEIPASS）、exe 同目录或配置目录
_BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
_ICON_CANDIDATES = (
    _BUNDLE_DIR / "app.ico",
    APP_DIR / "app.ico",
    DATA_DIR / "app.ico",
)
ICON_PATH = next((path for path in _ICON_CANDIDATES if path.exists()), _ICON_CANDIDATES[0])

# 开机自启（写注册表 Run 项，开机带 --boot 静默归位一次）
STARTUP_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_REG_NAME = "桌面整理"


def startup_command() -> str:
    """开机自启执行的命令：打包后指向 exe，源码运行时用 pythonw。"""
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}" --boot'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{interpreter}" "{Path(__file__).resolve()}" --boot'


def startup_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_PATH) as key:
            winreg.QueryValueEx(key, STARTUP_REG_NAME)
            return True
    except OSError:
        return False


def set_startup(enabled: bool) -> None:
    """写入或删除开机自启项。"""
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_PATH) as key:
        if enabled:
            winreg.SetValueEx(key, STARTUP_REG_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, STARTUP_REG_NAME)
            except OSError:
                pass

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


# ==================== 图形界面 ====================

# 分区配色（界面上的圆点 / 标签底色）
ZONE_COLORS = {
    "软件": "#3B5BDB",
    "文件夹": "#E8A02E",
    "文档": "#2FB37A",
    "图片": "#8B5CF6",
    "视频": "#EC5F8B",
    "音频": "#12A5A5",
    "压缩包": "#EE7B4E",
    "其他": "#8A93A6",
    "系统图标": "#5B6B8C",
}


def _mix(color: str, target: str, ratio: float) -> str:
    """把 color 按比例混向 target（0~1），用来由分区主色派生出浅底/描边/深字。"""
    def channels(value: str):
        value = value.lstrip("#")
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)

    red, green, blue = channels(color)
    to_red, to_green, to_blue = channels(target)
    return "#%02X%02X%02X" % (
        round(red + (to_red - red) * ratio),
        round(green + (to_green - green) * ratio),
        round(blue + (to_blue - blue) * ratio),
    )


# 设计令牌：颜色 / 圆角 / 字号的唯一来源，统一改这里就能整体换肤
TOKENS = {
    "bg": "#F4F6FB",
    "surface": "#FFFFFF",
    "surface_alt": "#F7F9FD",
    "border": "#E4E9F2",
    "border_strong": "#D2DAE9",
    "divider": "#EEF1F8",
    "text": "#1B2130",
    "text_sub": "#5B6579",
    "text_muted": "#8D96A9",
    "text_faint": "#A6AEC0",
    "brand": "#3B5BDB",
    "brand_hover": "#4A6AE8",
    "brand_press": "#2E48B8",
    "brand_soft": "#F0F4FF",
    "brand_border": "#C6D4FF",
    "brand_tint": "#F7FAFF",
    "neutral": "#566073",
    "neutral_hover": "#F4F6FA",
    "neutral_press": "#E8ECF3",
    # 窗口按钮：常态就有实底，避免在白底标题栏上"看不见"
    "chrome_bg": "#F0F3FC",
    "chrome_border": "#E0E6F4",
    "chrome_glyph": "#3E4A66",
    "chrome_hover": "#E3EAFB",
    "chrome_press": "#D6DFF4",
    "chrome_glyph_hover": "#1F2531",
    "danger": "#E05252",
    "danger_soft": "#FDEDED",
    "success": "#2FB37A",
    "warn": "#D98A1B",
    "radius_lg": "14px",
    "radius_md": "10px",
    "radius_sm": "8px",
}

# 状态浮条：常态 / 成功 / 警告 / 失败 四套配色（背景, 描边, 圆点, 文字）
STATUS_STYLES = {
    "info": ("#F5F8FF", "#DCE6FF", "#3B5BDB", "#44507A"),
    "ok": ("#F1FAF5", "#CDEBDD", "#2FB37A", "#2A6B52"),
    "warn": ("#FFF8EC", "#F2E0BB", "#D98A1B", "#8A5A10"),
    "error": ("#FDF0F0", "#F5D2D2", "#E05252", "#A93B3B"),
}

# 界面骨架尺寸（像素）：窗口高度由这些常量算出来，保证内容不溢出、不留大片空白
# 竖版页面：略窄于横版，信息逐行向下排
HEADER_H = 52                # 顶部标题栏（整条都能按住拖动）
SUMMARY_H = 56               # 概览信息：两列各一张小卡（桌面图标 / 已固定）
ZONE_CARD_H = 132            # 标题行 + 两行分区标签（每行 2 个）
PRIMARY_H = 42               # 主按钮：独占一行
ROW_H = 38                   # 次级按钮与开关所在行
SECONDARY_GAP = 8            # 两列之间的横向 / 纵向间距
ACTION_ROWS = 2
ACTION_GRID_H = ROW_H * ACTION_ROWS + SECONDARY_GAP   # 两列两行：3 个按钮 + 开关
GLYPH_SIZE = 32              # 窗口按钮：正方形，同一套圆角
GLYPH_GAP = 6                # 两个窗口按钮之间的间距
STRIP_H = 66                 # 底部提示条：常态三行
BUTTON_RADIUS = 10           # 所有按钮共用的圆角半径
BODY_SPACING = 12
BODY_MARGIN_TOP = 14
BODY_MARGIN_BOTTOM = 16
CHIP_H = 34
CHIP_COLUMNS = 2             # 每行放两个分区标签
WINDOW_MAX_WIDTH = 460
WINDOW_MIN_WIDTH = 400
ROOT_BORDER = 2               # 窗口外框 1px + 1px
SCROLL_CHROME = 10            # 滚动区自身吃掉的像素，计入窗口高度避免逼出滚动条
WINDOW_MIN_HEIGHT = 440

_QSS_TEMPLATE = """
* {
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
}
#Root { background: %(bg)s; border: 1px solid %(border_strong)s; }
#Content { background: transparent; }
#Header {
    background: %(brand_tint)s;
    border: none; border-bottom: 1px solid %(border)s;
    border-top-left-radius: 13px; border-top-right-radius: 13px;
}
#Header:hover { background: #EDF2FF; }
#Title { font-size: 14px; font-weight: 600; color: %(text)s; }

#Card { background: %(surface)s; border: 1px solid %(border)s; border-radius: %(radius_lg)s; }
#Caption { font-size: 11px; font-weight: 600; color: %(text_muted)s; }
#CaptionHint { font-size: 11px; color: %(text_faint)s; }
#SwitchField { background: %(surface)s; border: 1px solid %(border_strong)s; border-radius: 10px; }
#SwitchLabel { font-size: 12px; color: %(neutral)s; }

#Chip { background: %(surface_alt)s; border: 1px solid %(border)s; border-radius: %(radius_md)s; }
#Chip:hover { background: %(brand_soft)s; border-color: %(brand_border)s; }
#ChipName { font-size: 12px; color: %(text_sub)s; }
#ChipCount { font-size: 13px; font-weight: 700; color: %(text)s; }

#Primary {
    background: %(brand)s; color: #FFFFFF; border: none; border-radius: 10px;
    font-size: 13px; font-weight: 600;
}
#Primary:hover { background: %(brand_hover)s; }
#Primary:pressed { background: %(brand_press)s; }
#Primary:disabled { background: #C8D0E4; color: #F6F8FF; }

#Secondary {
    background: %(surface)s; border: 1px solid %(border_strong)s; border-radius: 10px;
    color: %(neutral)s; font-size: 13px;
}
#Secondary:hover { background: %(brand_soft)s; border-color: %(brand_border)s; color: %(brand)s; }
#Secondary:pressed { background: #E4EBFF; color: %(brand_press)s; }
#Secondary:disabled { background: #F5F7FA; color: %(text_faint)s; border-color: %(divider)s; }
#Secondary:focus { border-color: %(brand)s; }

#Status { border-radius: %(radius_md)s; }
#Switch { font-size: 12px; }
#Note { font-size: 11px; color: %(text_faint)s; }

QScrollArea#Scroll { background: transparent; border: none; }
QScrollArea#Scroll > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px 2px 2px 0; }
QScrollBar::handle:vertical { background: %(border_strong)s; border-radius: 3px; min-height: 32px; }
QScrollBar::handle:vertical:hover { background: #B9C4DA; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; background: transparent; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }

QToolTip { background: %(text)s; color: #FFFFFF; border: none; padding: 5px 9px; border-radius: 6px; }
"""

_GUI_QSS = _QSS_TEMPLATE % TOKENS


def run_gui(organizer: Organizer) -> int:
    from PySide6.QtCore import (
        Property,
        QEasingCurve,
        QEvent,
        QObject,
        QPointF,
        QPropertyAnimation,
        QRectF,
        QSize,
        Qt,
        QTimer,
    )
    from PySide6.QtGui import QColor, QIcon, QPainter, QPen
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QToolButton,
        QVBoxLayout,
        QWidget,
    )

    # 高 DPI：125% / 150% 缩放下按真实比例渲染，避免模糊和取整错位
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    app = QApplication(sys.argv)
    app.setApplicationName("桌面整理")
    app.setStyleSheet(_GUI_QSS)

    class _DragBar(QWidget):
        """标题栏：按住可以拖动无边框窗口。"""

        def __init__(self, target, parent=None):
            super().__init__(parent)
            self._target = target
            self._offset = None

        def mousePressEvent(self, event):
            if event.button() == Qt.MouseButton.LeftButton:
                self._offset = (
                    event.globalPosition().toPoint()
                    - self._target.frameGeometry().topLeft()
                )

        def mouseMoveEvent(self, event):
            if self._offset is not None:
                self._target.move(event.globalPosition().toPoint() - self._offset)

        def mouseReleaseEvent(self, event):
            self._offset = None

    class _GlyphButton(QToolButton):
        """窗口按钮：与次级按钮同一套圆角与配色，常态就有实底，存在感明确。"""

        def __init__(self, kind: str, tip: str, parent=None):
            super().__init__(parent)
            self._kind = kind
            self._hover = False
            self._down = False
            self.setToolTip(tip)
            self.setAccessibleName(tip)
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setFixedSize(GLYPH_SIZE, GLYPH_SIZE)
            self.setFocusPolicy(Qt.FocusPolicy.TabFocus)

        def enterEvent(self, event):
            self._hover = True
            self.update()
            super().enterEvent(event)

        def leaveEvent(self, event):
            self._hover = False
            self._down = False
            self.update()
            super().leaveEvent(event)

        def mousePressEvent(self, event):
            self._down = True
            self.update()
            super().mousePressEvent(event)

        def mouseReleaseEvent(self, event):
            self._down = False
            self.update()
            super().mouseReleaseEvent(event)

        def paintEvent(self, event):
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            close = self._kind == "close"
            box = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

            if close and self._hover:
                background = QColor("#C84444" if self._down else TOKENS["danger"])
                border = QColor("#C84444" if self._down else TOKENS["danger"])
                stroke = QColor("#FFFFFF")
            elif self._hover:
                background = QColor(
                    TOKENS["chrome_press"] if self._down else TOKENS["chrome_hover"])
                border = QColor(TOKENS["brand_border"])
                stroke = QColor(TOKENS["chrome_glyph_hover"])
            else:
                background = QColor(TOKENS["chrome_bg"])
                border = QColor(TOKENS["chrome_border"])
                stroke = QColor(TOKENS["chrome_glyph"])

            painter.setPen(QPen(border, 1))
            painter.setBrush(background)
            painter.drawRoundedRect(box, BUTTON_RADIUS, BUTTON_RADIUS)
            if self.hasFocus():
                painter.setPen(QPen(QColor(TOKENS["text_sub"]), 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(box.adjusted(1.5, 1.5, -1.5, -1.5),
                                        BUTTON_RADIUS - 2, BUTTON_RADIUS - 2)

            pen = QPen(stroke)
            pen.setWidthF(1.5)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)

            cx = self.width() / 2.0
            cy = self.height() / 2.0
            if close:
                offset = 4.6
                painter.drawLine(QPointF(cx - offset, cy - offset), QPointF(cx + offset, cy + offset))
                painter.drawLine(QPointF(cx + offset, cy - offset), QPointF(cx - offset, cy + offset))
            else:
                half = 5.5
                painter.drawLine(QPointF(cx - half, cy), QPointF(cx + half, cy))

    class _Switch(QCheckBox):
        """自绘横版开关：右=开、左=关；只有点开关本体才切换，点文字不切换。"""

        TRACK_W = 48
        TRACK_H = 28
        KNOB = 24
        INSET = 2.0
        PAD = 2              # 轨道四周留白，给键盘焦点描边留位置，不压在轨道边缘上

        def __init__(self, text: str, parent=None):
            super().__init__(text, parent)
            self.setObjectName("Switch")
            self.setMouseTracking(True)
            self.setCursor(Qt.CursorShape.ArrowCursor)
            # TabFocus：鼠标点击不夺取焦点，点完不会留下一圈焦点描边（键盘 Tab 仍可用）
            self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            self._over_track = False
            self._pos = 1.0 if self.isChecked() else 0.0
            self._anim = QPropertyAnimation(self, b"knob", self)
            self._anim.setDuration(160)
            self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self.toggled.connect(self._animate)

        def sizeWithPad(self) -> QSize:
            """含描边留白的控件尺寸。"""
            return QSize(self.TRACK_W + self.PAD * 2, self.TRACK_H + self.PAD * 2)

        def _get_knob(self) -> float:
            return self._pos

        def _set_knob(self, value: float) -> None:
            self._pos = float(value)
            self.update()

        knob = Property(float, _get_knob, _set_knob)

        def _track_rect(self) -> QRectF:
            return QRectF(
                self.width() - self.TRACK_W - self.PAD,
                (self.height() - self.TRACK_H) / 2.0,
                self.TRACK_W, self.TRACK_H,
            )

        def hitButton(self, pos) -> bool:
            # 命中区只覆盖开关本体（外扩 2px 好点），点文字不触发切换
            return self._track_rect().adjusted(-2, -2, 2, 2).contains(QPointF(pos))

        def _animate(self, checked: bool) -> None:
            self._anim.stop()
            self._anim.setStartValue(self._pos)
            self._anim.setEndValue(1.0 if checked else 0.0)
            self._anim.start()

        def mouseMoveEvent(self, event):
            over = self.hitButton(event.position().toPoint())
            if over != self._over_track:
                self._over_track = over
                self.setCursor(Qt.CursorShape.PointingHandCursor if over
                               else Qt.CursorShape.ArrowCursor)
                self.update()
            super().mouseMoveEvent(event)

        def leaveEvent(self, event):
            self._over_track = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.update()
            super().leaveEvent(event)

        def sizeHint(self) -> QSize:
            metrics = self.fontMetrics()
            width = metrics.horizontalAdvance(self.text()) + self.TRACK_W + 14
            return QSize(width, max(self.TRACK_H, metrics.height()))

        def paintEvent(self, event):
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            metrics = self.fontMetrics()

            # 文字：只有鼠标停在开关上时才加深，提示"这里才可点"
            text_width = max(0, self.width() - self.TRACK_W - 14)
            if self.isEnabled():
                color = QColor(TOKENS["brand"] if self._over_track else TOKENS["text_sub"])
            else:
                color = QColor(TOKENS["text_faint"])
            painter.setPen(color)
            painter.drawText(
                0, 0, text_width, self.height(),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                metrics.elidedText(self.text(), Qt.TextElideMode.ElideRight, text_width),
            )

            # 轨道
            track = self._track_rect()
            off = QColor("#D5DCE9") if self.isEnabled() else QColor("#E7EBF3")
            on = QColor(TOKENS["brand"]) if self.isEnabled() else QColor("#BCC7E6")
            ratio = self._pos
            track_color = QColor(
                int(off.red() + (on.red() - off.red()) * ratio),
                int(off.green() + (on.green() - off.green()) * ratio),
                int(off.blue() + (on.blue() - off.blue()) * ratio),
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(track_color)
            painter.drawRoundedRect(track, self.TRACK_H / 2.0, self.TRACK_H / 2.0)
            if self.hasFocus():
                # 只在键盘 Tab 聚焦时出现，且画在轨道外侧的留白里，不覆盖轨道本身的颜色
                ring = QColor("#FFFFFF") if self._pos > 0.5 else QColor(TOKENS["brand"])
                painter.setPen(QPen(ring, 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(track.adjusted(-1, -1, 1, 1),
                                        self.TRACK_H / 2.0 + 1, self.TRACK_H / 2.0 + 1)

            # 圆钮：横向滑动，右=开、左=关
            travel = self.TRACK_W - self.KNOB - self.INSET * 2
            knob = QRectF(
                track.x() + self.INSET + travel * ratio,
                track.y() + (self.TRACK_H - self.KNOB) / 2.0,
                self.KNOB, self.KNOB,
            )
            painter.setBrush(QColor("#FFFFFF"))
            painter.drawEllipse(knob)

    # ---------- 窗口骨架 ----------
    window = QWidget()
    window.setWindowTitle("桌面整理")
    window.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
    icon = QIcon(str(ICON_PATH)) if ICON_PATH.exists() else QIcon()
    if not icon.isNull():
        window.setWindowIcon(icon)
        app.setWindowIcon(icon)

    shell = QVBoxLayout(window)
    shell.setContentsMargins(0, 0, 0, 0)
    root = QFrame(window)
    root.setObjectName("Root")
    shell.addWidget(root)

    # 内容放进滚动区：屏幕放不下时能滚动，屏幕足够时不出现滚动条
    scroll = QScrollArea(root)
    scroll.setObjectName("Scroll")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    root_layout = QVBoxLayout(root)
    root_layout.setContentsMargins(0, 0, 0, 0)
    root_layout.addWidget(scroll)

    body = QWidget()
    body.setObjectName("Content")
    scroll.setWidget(body)
    layout = QVBoxLayout(body)
    layout.setContentsMargins(20, BODY_MARGIN_TOP, 20, BODY_MARGIN_BOTTOM)
    layout.setSpacing(BODY_SPACING)

    # ---------- 标题栏（整条都是拖动区） ----------
    titlebar = _DragBar(window, root)
    titlebar.setObjectName("Header")
    titlebar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    titlebar.setFixedHeight(HEADER_H)
    titlebar.setCursor(Qt.CursorShape.SizeAllCursor)
    bar = QHBoxLayout(titlebar)
    bar.setContentsMargins(16, 0, 12, 0)
    bar.setSpacing(9)
    icon_label = QLabel(titlebar)
    if not icon.isNull():
        icon_label.setPixmap(icon.pixmap(22, 22))
    title = QLabel("桌面整理", titlebar)
    title.setObjectName("Title")
    # 文字不接收鼠标事件，按在标题上也能拖动窗口
    for widget in (icon_label, title):
        widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    minimize_button = _GlyphButton("min", "最小化", titlebar)
    minimize_button.clicked.connect(window.showMinimized)
    close_button = _GlyphButton("close", "关闭", titlebar)
    close_button.clicked.connect(window.close)

    win_box = QHBoxLayout()
    win_box.setContentsMargins(0, 0, 0, 0)
    win_box.setSpacing(GLYPH_GAP)
    win_box.addWidget(minimize_button)
    win_box.addWidget(close_button)

    bar.addWidget(icon_label)
    bar.addWidget(title)
    bar.addStretch(1)
    bar.addLayout(win_box)
    # 标题栏放在滚动区外面：内容滚动时它始终固定在顶部，随时可以拖动
    root_layout.insertWidget(0, titlebar)

    # ---------- 概览信息（两列，各自一张小卡，界限分明） ----------
    summary = QWidget(body)
    summary.setFixedHeight(SUMMARY_H)
    summary_layout = QHBoxLayout(summary)
    summary_layout.setContentsMargins(0, 0, 0, 0)
    summary_layout.setSpacing(SECONDARY_GAP)

    def summary_item(caption_text: str, tip: str, base_color: str):
        box = QFrame(summary)
        box.setObjectName("SummaryCard")
        box.setFixedHeight(SUMMARY_H)
        box.setToolTip(tip)
        box.setStyleSheet(
            f"#SummaryCard {{ background: {_mix(base_color, '#FFFFFF', 0.92)};"
            f" border: 1px solid {_mix(base_color, '#FFFFFF', 0.76)};"
            f" border-radius: 12px; }}")
        column = QVBoxLayout(box)
        column.setContentsMargins(12, 8, 12, 8)
        column.setSpacing(2)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(6)
        dot = QLabel(box)
        dot.setFixedSize(7, 7)
        dot.setStyleSheet(f"background: {base_color}; border-radius: 3px;")
        caption = QLabel(caption_text, box)
        caption.setStyleSheet(
            f"font-size: 11px; color: {_mix(base_color, '#101828', 0.55)};")
        head.addWidget(dot)
        head.addWidget(caption)
        head.addStretch(1)
        value = QLabel("—", box)
        value.setStyleSheet(
            f"font-size: 17px; font-weight: 700; color: {_mix(base_color, '#101828', 0.25)};")
        column.addLayout(head)
        column.addWidget(value)
        return box, value

    icons_box, icons_value = summary_item("桌面图标", "桌面上当前的图标总数", TOKENS["brand"])
    fixed_box, fixed_value = summary_item("已固定", "已记录固定位置的图标数量", TOKENS["success"])
    for box in (icons_box, fixed_box):
        summary_layout.addWidget(box, 1)
    layout.addWidget(summary)

    # ---------- 分区卡片 ----------
    zone_card = QFrame(root)
    zone_card.setObjectName("Card")
    zone_card.setMinimumHeight(ZONE_CARD_H)
    zone_layout = QVBoxLayout(zone_card)
    zone_layout.setContentsMargins(18, 14, 18, 16)
    zone_layout.setSpacing(10)

    caption_row = QHBoxLayout()
    caption_row.setContentsMargins(0, 0, 0, 0)
    caption_row.setSpacing(6)
    accent = QFrame(zone_card)
    accent.setFixedSize(3, 12)
    accent.setStyleSheet(f"background: {TOKENS['brand']}; border-radius: 2px;")
    zone_caption = QLabel("分区概览", zone_card)
    zone_caption.setObjectName("Caption")
    zone_hint = QLabel("", zone_card)
    zone_hint.setObjectName("CaptionHint")
    caption_row.addWidget(accent)
    caption_row.addWidget(zone_caption)
    caption_row.addStretch(1)
    caption_row.addWidget(zone_hint)
    zone_layout.addLayout(caption_row)

    chip_grid = QGridLayout()
    chip_grid.setContentsMargins(0, 0, 0, 0)
    chip_grid.setHorizontalSpacing(8)
    chip_grid.setVerticalSpacing(8)
    for column in range(CHIP_COLUMNS):
        chip_grid.setColumnStretch(column, 1)
    zone_layout.addLayout(chip_grid)

    # 首次加载提示（读完后被分区标签替换）
    loading_label = QLabel("正在读取桌面", zone_card)
    loading_label.setObjectName("Note")
    chip_grid.addWidget(loading_label, 0, 0, 1, CHIP_COLUMNS)
    loading_timer = QTimer(root)
    loading_timer.setInterval(400)
    dots = {"n": 0}

    def tick_loading() -> None:
        dots["n"] = (dots["n"] + 1) % 4
        loading_label.setText("正在读取桌面" + "·" * dots["n"])

    loading_timer.timeout.connect(tick_loading)
    loading_timer.start()
    layout.addWidget(zone_card)

    # ---------- 操作区 ----------
    primary_button = QPushButton("一键整理　·　回到固定布局", root)
    primary_button.setObjectName("Primary")
    primary_button.setFixedHeight(PRIMARY_H)
    primary_button.setCursor(Qt.CursorShape.PointingHandCursor)
    primary_button.setAccessibleName("一键整理，回到固定布局")
    primary_button.setToolTip("把图标按固定布局归位，没固定过的自动补位")
    # 点击不夺取焦点，键盘 Tab 仍可聚焦
    primary_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    # 大按钮独占一行；其余三个按钮 + 开机自启开关排成两列
    def secondary_button(text: str, tooltip: str, slot) -> QPushButton:
        button = QPushButton(text, root)
        button.setObjectName("Secondary")
        button.setFixedHeight(ROW_H)
        button.setToolTip(tooltip)
        button.setAccessibleName(f"{text}：{tooltip}")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        # 点击不夺取焦点，避免点完后一直挂着蓝色 focus 描边
        button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        button.clicked.connect(slot)
        return button

    fix_button = secondary_button("固定当前位置", "把桌面现在的位置记为固定布局", lambda: run_action(organizer.fix_current))
    undo_button = secondary_button("撤销上次整理", "回到上次整理之前的位置", lambda: run_action(organizer.undo))
    unfix_button = secondary_button("解除固定", "清除位置记录，重新自由摆放", lambda: run_action(organizer.unfix))

    # 开机自启：标签和开关包在同一个带边框的控件里，占满一格，与三个按钮齐平
    startup_box = QFrame(root)
    startup_box.setObjectName("SwitchField")
    startup_box.setFixedHeight(ROW_H)
    startup_box.setToolTip("开启后每次开机自动把图标归位一次（点右侧开关切换）")
    startup_layout = QHBoxLayout(startup_box)
    startup_layout.setContentsMargins(12, 0, 8, 0)
    startup_layout.setSpacing(8)
    startup_label = QLabel("开机自动启动", startup_box)
    startup_label.setObjectName("SwitchLabel")
    startup_pill = _Switch("", startup_box)
    startup_pill.setFixedSize(startup_pill.sizeWithPad())
    startup_pill.setChecked(startup_enabled())
    startup_layout.addWidget(startup_label)
    startup_layout.addStretch(1)
    startup_layout.addWidget(startup_pill)

    action_grid = QGridLayout()
    action_grid.setContentsMargins(0, 0, 0, 0)
    action_grid.setHorizontalSpacing(SECONDARY_GAP)
    action_grid.setVerticalSpacing(SECONDARY_GAP)
    action_grid.addWidget(fix_button, 0, 0)
    action_grid.addWidget(undo_button, 0, 1)
    action_grid.addWidget(unfix_button, 1, 0)
    action_grid.addWidget(startup_box, 1, 1)

    layout.addWidget(primary_button)
    layout.addLayout(action_grid)

    # ---------- 底部提示条 ----------
    status = QFrame(root)
    status.setObjectName("Status")
    status.setMinimumHeight(STRIP_H)
    status_layout = QHBoxLayout(status)
    status_layout.setContentsMargins(12, 9, 12, 9)
    status_layout.setSpacing(8)
    status_dot = QLabel(status)
    status_dot.setFixedSize(7, 7)
    status_text = QLabel(status)
    status_text.setObjectName("Note")
    status_text.setWordWrap(True)
    status_text.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    dot_box = QVBoxLayout()
    dot_box.setContentsMargins(0, 4, 0, 0)
    dot_box.addWidget(status_dot)
    dot_box.addStretch(1)
    status_layout.addLayout(dot_box)
    status_layout.addWidget(status_text, 1)
    layout.addWidget(status)
    layout.addStretch(1)

    GUIDANCE = ("用法：先把图标拖到喜欢的位置 → 点“固定当前位置”；之后被拖乱，点“一键整理”即可回到布局。\n"
                "只调整图标位置，不会移动、改名或删除任何文件。")

    all_buttons = [primary_button, fix_button, undo_button, unfix_button]

    def set_status(message: str, kind: str = "info") -> None:
        background, border, dot, text = STATUS_STYLES.get(kind, STATUS_STYLES["info"])
        status.setStyleSheet(
            f"#Status {{ background: {background}; border: 1px solid {border};"
            f" border-radius: {TOKENS['radius_md']}; }}")
        status_dot.setStyleSheet(f"background: {dot}; border-radius: 3px;")
        status_text.setStyleSheet(f"color: {text}; font-size: 11px;")
        status_text.setText(message)

    # ---------- 交互 ----------
    def rebuild_chips(counts: dict) -> None:
        loading_timer.stop()
        while chip_grid.count():
            item = chip_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)  # 立刻从布局移除，避免影响高度计算
                widget.deleteLater()
        visible = [(zone, counts[zone]) for zone in ZONE_ORDER if counts.get(zone)]
        zone_hint.setText(f"共 {len(visible)} 类" if visible else "")
        if not visible:
            hint = QLabel("桌面暂时没有图标", zone_card)
            hint.setObjectName("Note")
            chip_grid.addWidget(hint, 0, 0, 1, CHIP_COLUMNS)
            return
        for index, (zone, count) in enumerate(visible):
            base = ZONE_COLORS.get(zone, "#8A93A6")
            # 标签用分区主色派生出同色系的浅底 / 细描边 / 深字，形成一套完整的色彩层次
            chip = QFrame(zone_card)
            chip.setObjectName("Chip")
            chip.setFixedHeight(CHIP_H)
            chip.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            chip.setToolTip(f"{zone}：{count} 个")
            chip.setStyleSheet(
                f"#Chip {{ background: {_mix(base, '#FFFFFF', 0.90)};"
                f" border: 1px solid {_mix(base, '#FFFFFF', 0.70)};"
                f" border-radius: {TOKENS['radius_md']}; }}"
                f"#Chip:hover {{ background: {_mix(base, '#FFFFFF', 0.82)};"
                f" border-color: {_mix(base, '#FFFFFF', 0.52)}; }}")
            chip_layout = QHBoxLayout(chip)
            chip_layout.setContentsMargins(10, 7, 10, 7)
            chip_layout.setSpacing(6)
            dot = QLabel(chip)
            dot.setFixedSize(8, 8)
            dot.setStyleSheet(f"background: {base}; border-radius: 4px;")
            name = QLabel(zone, chip)
            name.setStyleSheet(f"font-size: 12px; color: {_mix(base, '#101828', 0.55)};")
            value = QLabel(str(count), chip)
            value.setStyleSheet(
                f"font-size: 13px; font-weight: 700; color: {_mix(base, '#101828', 0.30)};")
            chip_layout.addWidget(dot)
            chip_layout.addWidget(name)
            chip_layout.addStretch(1)
            chip_layout.addWidget(value)
            chip_grid.addWidget(chip, index // CHIP_COLUMNS, index % CHIP_COLUMNS)

    def refresh(show_loading: bool = False) -> None:
        if show_loading:
            loading_timer.start()
        try:
            _listview, slots, _size, _auto_on = organizer.snapshot()
        except DesktopError as error:
            icons_value.setText("—")
            fixed_value.setText("—")
            loading_timer.stop()
            rebuild_chips({})
            set_status(f"读取桌面失败：{error}", "error")
            return
        counts = organizer.zone_counts(slots)
        fixed = sum(1 for slot in slots if slot.key in organizer.cfg.positions)
        icons_value.setText(str(len(slots)))
        fixed_value.setText(str(fixed))
        loaded["done"] = True
        if status_kind["current"] == "info":
            set_status(GUIDANCE)
        rebuild_chips(counts)

    status_kind = {"current": "info"}

    def run_action(action) -> None:
        for button in all_buttons:
            button.setEnabled(False)
        set_status("正在处理，请稍候……", "info")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            message = action()
        except DesktopError as error:
            refresh()
            status_kind["current"] = "error"
            set_status(f"操作失败：{error}", "error")
            return
        except Exception as error:  # 兜底：不让异常把窗口留在禁用状态
            refresh()
            status_kind["current"] = "error"
            set_status(f"操作失败：{error}", "error")
            return
        finally:
            QApplication.restoreOverrideCursor()
            for button in all_buttons:
                button.setEnabled(True)
        refresh()
        status_kind["current"] = "ok"
        set_status(message, "ok")

    loaded = {"done": False}

    primary_button.clicked.connect(lambda: run_action(lambda: organizer.apply("keep")))
    primary_button.setDefault(True)

    def on_startup_toggled(state: int) -> None:
        enabled = state == Qt.CheckState.Checked.value
        try:
            set_startup(enabled)
        except OSError as error:
            startup_pill.blockSignals(True)
            startup_pill.setChecked(not enabled)
            startup_pill.blockSignals(False)
            set_status(f"设置开机自启失败：{error}", "error")
            return
        set_status("已开启开机自启：开机后会静默把图标归位一次。"
                   if enabled else "已关闭开机自启。", "ok" if enabled else "info")

    startup_pill.stateChanged.connect(on_startup_toggled)

    class _ActivationWatcher(QObject):
        """窗口重新获得焦点时自动刷新（代替原来的“刷新”按钮）。"""

        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.ActivationChange and window.isActiveWindow():
                if loaded["done"]:
                    # 重新回到窗口时，上一次的操作结果让位给使用说明
                    status_kind["current"] = "info"
                    refresh()
            return False

    activation_watcher = _ActivationWatcher(window)
    window.installEventFilter(activation_watcher)

    # ---------- 尺寸：按屏幕可用区域自适应 ----------
    geometry = app.primaryScreen().availableGeometry()
    width = max(WINDOW_MIN_WIDTH, min(WINDOW_MAX_WIDTH, geometry.width() - 120))
    natural_height = (
        HEADER_H + SUMMARY_H + ZONE_CARD_H + PRIMARY_H + ACTION_GRID_H + STRIP_H
        + BODY_SPACING * 4 + BODY_MARGIN_TOP + BODY_MARGIN_BOTTOM
        + ROOT_BORDER + SCROLL_CHROME
    )
    height = max(WINDOW_MIN_HEIGHT, min(natural_height, geometry.height() - 100))
    window.setFixedSize(width, height)
    window.move(geometry.center() - window.rect().center())

    window.show()
    set_status(GUIDANCE)
    # 先让窗口画出来，再读取桌面（读取期间显示“正在读取桌面…”）
    QTimer.singleShot(60, lambda: refresh(show_loading=True))
    return app.exec()


# ==================== 命令行 ====================

def main() -> int:
    args = sys.argv[1:]
    config = Config()
    organizer = Organizer(config)

    if not args:
        return run_gui(organizer)

    make_dpi_aware()  # 命令行模式没有 Qt，需要自己声明 DPI 感知，坐标才准
    try:
        if "--plan" in args:
            print(organizer.apply("keep", dry_run=True))
            print()
            print(organizer.apply("compact", dry_run=True))
            return 0
        if "--apply" in args:
            print(organizer.apply("keep"))
            return 0
        if "--boot" in args:
            # 开机自启：静默归位一次（无窗口、无提示）
            organizer.apply("keep")
            return 0
        if "--fix" in args:
            print(organizer.fix_current())
            return 0
        if "--unfix" in args:
            print(organizer.unfix())
            return 0
        if "--compact" in args:
            print(organizer.apply("compact"))
            return 0
        if "--undo" in args:
            print(organizer.undo())
            return 0
    except DesktopError as error:
        print(f"失败：{error}")
        return 1
    print("未知参数。可用：--plan / --apply / --fix / --unfix / --compact / --undo")
    return 1


if __name__ == "__main__":
    sys.exit(main())