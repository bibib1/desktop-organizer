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

import sys

from desktop_api import DesktopError, make_dpi_aware
from organizer import Config, Organizer
from ui import run_gui


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
