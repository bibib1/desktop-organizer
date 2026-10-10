# -*- coding: utf-8 -*-
"""路径定位：程序目录、数据目录，以及配置文件和图标的位置。

打包成 exe 后配置写在 %APPDATA%\桌面整理 下，
避免在桌面等用户目录里生成文件。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


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
