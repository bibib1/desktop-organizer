# -*- coding: utf-8 -*-
"""桌面条目分类：把桌面上的真实文件按类型归入分区。

只读扫描桌面目录，不会移动任何文件；
系统图标（此电脑、回收站等，不属于任何真实文件）单独归入“系统图标”分区。

注意：桌面可能被重定向（例如本机是 D:\\Desktop），所以这里通过
SHGetKnownFolderPath 向系统要真实路径，而不是拼 %USERPROFILE%\\Desktop。
"""
from __future__ import annotations

import ctypes
import os
import winreg
from dataclasses import dataclass
from pathlib import Path

# 视为“软件 / 快捷方式”的扩展名
APP_EXTS = {".lnk", ".url", ".exe", ".msi", ".bat", ".cmd", ".ps1", ".appref-ms"}

# 文件类型分区（顺序即桌面上的排列顺序）
TYPE_GROUPS = [
    ("文档", {".doc", ".docx", ".pdf", ".txt", ".md", ".rtf", ".wps", ".odt",
              ".xls", ".xlsx", ".et", ".csv", ".ods",
              ".ppt", ".pptx", ".dps", ".odp", ".one", ".vsdx"}),
    ("图片", {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico",
              ".tif", ".tiff", ".heic", ".psd", ".ai"}),
    ("视频", {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
              ".rmvb", ".ts"}),
    ("音频", {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".wma", ".ape"}),
    ("压缩包", {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz",
                ".iso", ".cab"}),
]
OTHER_GROUP = "其他"
SYSTEM_GROUP = "系统图标"

# 分区从左到右的顺序
ZONE_ORDER = ["软件", "文件夹"] + [name for name, _ in TYPE_GROUPS] + [OTHER_GROUP, SYSTEM_GROUP]

SKIP_NAMES = {"desktop.ini", "thumbs.db"}


@dataclass
class DeskEntry:
    name: str          # 桌面图标显示名（快捷方式去掉 .lnk 等后缀）
    path: str | None   # 真实路径；系统图标为 None
    zone: str          # 所属分区
    is_dir: bool


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


_FOLDERID_DESKTOP = _GUID(
    0xB4BFCC3A, 0xDB2C, 0x424C,
    (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41),
)


def _known_folder(folder_id: _GUID) -> str | None:
    """用 SHGetKnownFolderPath 取系统已知文件夹的真实路径。"""
    try:
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        shell32.SHGetKnownFolderPath.argtypes = [
            ctypes.POINTER(_GUID), ctypes.c_uint32,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ]
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        pointer = ctypes.c_void_p()
        hr = shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(pointer))
        if hr != 0 or not pointer.value:
            return None
        path = ctypes.wstring_at(pointer)
        ole32.CoTaskMemFree(pointer)
        return path
    except Exception:
        return None


def _registry_folder(hive, value_name: str) -> str | None:
    """从壳文件夹注册表读取目录（会自动展开 %PUBLIC% 这类变量）。"""
    try:
        with winreg.OpenKey(
            hive,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            value, _ = winreg.QueryValueEx(key, value_name)
        return os.path.expandvars(value)
    except OSError:
        return None


def desktop_dirs() -> list[Path]:
    """返回桌面真实目录（用户桌面 + 公共桌面），兼容桌面被重定向的情况。"""
    dirs: list[Path] = []
    candidates = [
        _known_folder(_FOLDERID_DESKTOP),
        _registry_folder(winreg.HKEY_CURRENT_USER, "Desktop"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_dir():
            dirs.append(Path(candidate))
            break

    public_candidates = [
        _registry_folder(winreg.HKEY_LOCAL_MACHINE, "Common Desktop"),
        _registry_folder(winreg.HKEY_CURRENT_USER, "Common Desktop"),
        str(Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop"),
    ]
    for candidate in public_candidates:
        if candidate and Path(candidate).is_dir():
            dirs.append(Path(candidate))
            break
    return dirs


def _display_name(path: Path) -> str:
    name = path.name
    for ext in (".lnk", ".url", ".appref-ms"):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return name


def zone_of(path: Path) -> str:
    if path.is_dir():
        return "文件夹"
    ext = path.suffix.lower()
    if ext in APP_EXTS:
        return "软件"
    for name, exts in TYPE_GROUPS:
        if ext in exts:
            return name
    return OTHER_GROUP


def scan_entries() -> list[DeskEntry]:
    """扫描桌面目录（按目录顺序），返回条目列表。

    不排除隐藏文件：Explorer 在开启“显示隐藏项目”时会显示它们，
    而这些也要能和桌面图标对上号。
    """
    entries: list[DeskEntry] = []
    seen: set[str] = set()
    for folder in desktop_dirs():
        try:
            children = sorted(folder.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for path in children:
            if path.name.lower() in SKIP_NAMES or path.name.startswith("."):
                continue
            key = os.path.normcase(str(path))
            if key in seen:
                continue
            seen.add(key)
            entries.append(DeskEntry(
                name=_display_name(path),
                path=str(path),
                zone=zone_of(path),
                is_dir=path.is_dir(),
            ))
    return entries


def build_matcher(entries: list[DeskEntry]) -> dict[str, DeskEntry]:
    """建立“桌面图标名 -> 条目”的映射，兼容系统隐藏扩展名的情况。"""
    matcher: dict[str, DeskEntry] = {}
    stems: dict[str, list[DeskEntry]] = {}
    for entry in entries:
        matcher[entry.name.lower()] = entry
        if entry.path:
            stem = Path(entry.path).stem.lower()
            stems.setdefault(stem, []).append(entry)
    for stem, group in stems.items():
        if len(group) == 1 and stem not in matcher:
            matcher[stem] = group[0]
    return matcher