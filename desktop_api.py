# -*- coding: utf-8 -*-
"""Windows 真实桌面图标接口（直接操作桌面 ListView）。

原理：桌面图标由 explorer.exe 里的 SysListView32 控件承载，
通过向它发送 LVM_* 消息可以读取图标名称/位置、设置图标位置；
跨进程读写需要用到 explorer 进程内的远程内存。

只改图标位置，不移动、不改名、不删除任何文件。
"""
from __future__ import annotations

import ctypes
import winreg
from ctypes import wintypes
from dataclasses import dataclass

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# --- ListView 消息 ---
LVM_FIRST = 0x1000
LVM_GETITEMCOUNT = LVM_FIRST + 4
LVM_SETITEMPOSITION = LVM_FIRST + 15
LVM_GETITEMPOSITION = LVM_FIRST + 16
LVM_GETITEMSPACING = LVM_FIRST + 51
LVM_GETITEMTEXTW = LVM_FIRST + 115

LVIF_TEXT = 0x0001
GWL_STYLE = -16
LVS_AUTOARRANGE = 0x0100
LVS_ALIGNLEFT = 0x0800

# --- 进程内存 ---
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04

# 桌面视图设置的注册表位置（让“自动排列图标”的开关能持久保存）
BAGS_KEY = r"Software\Microsoft\Windows\Shell\Bags\1\Desktop"
FFLAGS_AUTOARRANGE = 0x00000001

_TEXT_CHARS = 512


class DesktopError(RuntimeError):
    """桌面 ListView 不可用。"""


class _LVITEMW(ctypes.Structure):
    _fields_ = [
        ("mask", ctypes.c_uint),
        ("iItem", ctypes.c_int),
        ("iSubItem", ctypes.c_int),
        ("state", ctypes.c_uint),
        ("stateMask", ctypes.c_uint),
        ("pszText", ctypes.c_void_p),
        ("cchTextMax", ctypes.c_int),
        ("iImage", ctypes.c_int),
        ("lParam", ctypes.c_void_p),
        ("iIndent", ctypes.c_int),
        ("iGroupId", ctypes.c_int),
        ("cColumns", ctypes.c_uint),
        ("puColumns", ctypes.c_void_p),
        ("piColFmt", ctypes.c_void_p),
        ("iGroup", ctypes.c_int),
    ]


@dataclass
class DesktopIcon:
    index: int
    name: str
    x: int
    y: int


def _prepare() -> None:
    user32.FindWindowW.restype = wintypes.HWND
    user32.FindWindowExW.restype = wintypes.HWND
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.VirtualAllocEx.restype = ctypes.c_void_p


_prepare()


# ==================== 定位 ====================

def find_desktop_listview() -> int:
    """找到承载桌面图标的 SysListView32 窗口句柄。"""
    parents = []
    progman = user32.FindWindowW("Progman", None)
    if progman:
        parents.append(progman)

    compare = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        buffer = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, buffer, 64)
        if buffer.value == "WorkerW":
            parents.append(hwnd)
        return True

    user32.EnumWindows(compare(callback), 0)
    for parent in parents:
        view = user32.FindWindowExW(parent, None, "SHELLDLL_DefView", None)
        if view:
            listview = user32.FindWindowExW(view, None, "SysListView32", None)
            if listview:
                return listview
    raise DesktopError("没有找到桌面图标控件（SysListView32）")


def client_size(listview: int) -> tuple[int, int]:
    rect = wintypes.RECT()
    user32.GetClientRect(listview, ctypes.byref(rect))
    return rect.right - rect.left, rect.bottom - rect.top


def item_spacing(listview: int) -> tuple[int, int]:
    """返回图标单元格尺寸 (宽, 高)，用于计算网格。"""
    value = user32.SendMessageW(listview, LVM_GETITEMSPACING, 1, 0)
    width = value & 0xFFFF
    height = (value >> 16) & 0xFFFF
    if width <= 0 or height <= 0:
        width, height = 76, 100
    return width, height


# ==================== 读取 / 写入 ====================

def read_items(listview: int) -> list[DesktopIcon]:
    """读取桌面上所有图标的名称与位置。"""
    count = user32.SendMessageW(listview, LVM_GETITEMCOUNT, 0, 0)
    if count <= 0:
        return []

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(listview, ctypes.byref(pid))
    process = kernel32.OpenProcess(
        PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_QUERY_INFORMATION,
        False, pid.value,
    )
    if not process:
        raise DesktopError("无法读取桌面进程内存（请不要用管理员身份运行本工具）")

    item_size = ctypes.sizeof(_LVITEMW)
    text_size = _TEXT_CHARS * ctypes.sizeof(ctypes.c_wchar)
    point_size = ctypes.sizeof(wintypes.POINT)
    remote_item = kernel32.VirtualAllocEx(
        process, None, item_size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
    remote_text = kernel32.VirtualAllocEx(
        process, None, text_size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
    remote_point = kernel32.VirtualAllocEx(
        process, None, point_size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)

    icons: list[DesktopIcon] = []
    try:
        if not (remote_item and remote_text and remote_point):
            raise DesktopError("无法在桌面进程中分配内存")
        for index in range(count):
            entry = _LVITEMW(
                mask=LVIF_TEXT, iItem=index, iSubItem=0,
                pszText=remote_text, cchTextMax=_TEXT_CHARS,
            )
            kernel32.WriteProcessMemory(
                process, remote_item, ctypes.byref(entry), item_size, None)
            user32.SendMessageW(listview, LVM_GETITEMTEXTW, index, remote_item)
            buffer = ctypes.create_unicode_buffer(_TEXT_CHARS)
            kernel32.ReadProcessMemory(process, remote_text, buffer, text_size, None)

            user32.SendMessageW(listview, LVM_GETITEMPOSITION, index, remote_point)
            point = wintypes.POINT()
            kernel32.ReadProcessMemory(
                process, remote_point, ctypes.byref(point), point_size, None)
            icons.append(DesktopIcon(index, buffer.value, point.x, point.y))
    finally:
        for pointer in (remote_item, remote_text, remote_point):
            if pointer:
                kernel32.VirtualFreeEx(process, pointer, 0, MEM_RELEASE)
        kernel32.CloseHandle(process)
    return icons


def set_position(listview: int, index: int, x: int, y: int) -> None:
    """把第 index 个图标移动到 (x, y)。"""
    x = max(0, min(0x7FFF, int(x)))
    y = max(0, min(0x7FFF, int(y)))
    user32.SendMessageW(listview, LVM_SETITEMPOSITION, index, (y << 16) | x)


def set_positions(listview: int, positions: dict[int, tuple[int, int]]) -> None:
    for index, (x, y) in positions.items():
        set_position(listview, index, x, y)


# ==================== 自动排列开关 ====================

def _get_style(listview: int) -> int:
    return user32.GetWindowLongPtrW(listview, GWL_STYLE)


def _set_style(listview: int, style: int) -> None:
    user32.SetWindowLongPtrW(listview, GWL_STYLE, style)


def auto_arrange_enabled(listview: int) -> bool:
    """桌面是否开启了“自动排列图标”。"""
    return bool(_get_style(listview) & LVS_AUTOARRANGE)


def disable_auto_arrange(listview: int, persist: bool = True) -> bool:
    """关闭“自动排列图标”（否则设好的位置会被系统打乱）。返回是否有改动。"""
    changed = False
    style = _get_style(listview)
    if style & LVS_AUTOARRANGE:
        _set_style(listview, style & ~LVS_AUTOARRANGE)
        user32.InvalidateRect(listview, None, True)
        changed = True
    if persist:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, BAGS_KEY, 0,
                winreg.KEY_READ | winreg.KEY_WRITE,
            ) as key:
                flags, _ = winreg.QueryValueEx(key, "FFlags")
                value = int(flags) & ~FFLAGS_AUTOARRANGE
                if value != flags:
                    winreg.SetValueEx(key, "FFlags", 0, winreg.REG_DWORD, value)
                    changed = True
        except OSError:
            pass
    return changed


def registry_flags() -> tuple[int | None, int | None]:
    """读取桌面视图的 FFlags / Sort（诊断用）。"""
    flags = sort = None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, BAGS_KEY) as key:
            try:
                flags, _ = winreg.QueryValueEx(key, "FFlags")
            except OSError:
                pass
            try:
                sort, _ = winreg.QueryValueEx(key, "Sort")
            except OSError:
                pass
    except OSError:
        pass
    return flags, sort


def make_dpi_aware() -> None:
    """让本进程按物理像素与桌面控件对齐（避免高分屏下坐标偏移）。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass