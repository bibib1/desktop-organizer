# -*- coding: utf-8 -*-
"""开机自启：读写 HKCU 的 Run 项。

开启后每次开机带 --boot 参数静默归位一次，随即退出。
"""
from __future__ import annotations

import sys
import winreg
from pathlib import Path


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
