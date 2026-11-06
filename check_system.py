"""只读的系统检查小工具。

默认只读取信息并打印，不修改任何文件或系统设置。
只有你主动加上 --output 时才会写出一个报告文件。
只使用 Python 标准库，不需要额外安装。

常用例子：
    python check_system.py                       # 基本检查
    python check_system.py --all                 # 打开所有不需要指定目录的检查
    python check_system.py --ports --processes   # 只看端口和占内存的程序
    python check_system.py --folder-sizes .      # 看当前目录下哪个子文件夹最占空间
    python check_system.py --duplicates D:\\下载  # 找出重复的文件
    python check_system.py --json --output a.txt # 存成 JSON 报告
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import getpass
import hashlib
import io
import json
import os
import platform
import re
import shutil
import socket
import string
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

VERSION = "0.3.0"
GIB = 1024 ** 3

# 扫描目录时的上限，避免在超大目录上卡住
MAX_SCAN_FILES = 200_000
# 算重复文件时，单个文件最多读这么多字节
MAX_HASH_BYTES = 64 * 1024 * 1024

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

# 需要提醒用户的事情，最后统一显示
warnings: list[str] = []
# 没能完成的检查（权限不足、命令缺失等），单独列出来，不当成"没问题"
skipped: list[str] = []

IP_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b[0-9a-fA-F:]{6,}\b")


# ================================================================ 小工具

def human_size(num_bytes: float) -> str:
    """把字节数变成人看得懂的大小。"""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def human_duration(seconds: float) -> str:
    """把秒数变成「x 天 x 小时 x 分钟」。"""
    delta = timedelta(seconds=int(seconds))
    hours, minutes = delta.seconds // 3600, (delta.seconds % 3600) // 60
    parts = []
    if delta.days:
        parts.append(f"{delta.days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    parts.append(f"{minutes} 分钟")
    return " ".join(parts)


def run_readonly(command: list[str], timeout: int = 15) -> str | None:
    """运行一个只查询、不改动系统的命令。失败返回 None，不抛错。"""
    try:
        result = subprocess.run(
            command, capture_output=True, text=True,
            timeout=timeout, errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 and not result.stdout.strip():
        return None
    return result.stdout


def walk_files(root: Path, follow_links: bool = False):
    """遍历目录下的文件，自动跳过常见的无关目录，并限制总数。"""
    ignore = {".git", "node_modules", "__pycache__", ".venv", "venv",
              "$RECYCLE.BIN", "System Volume Information", ".cache"}
    count = unreadable = 0
    for current, dirnames, filenames in os.walk(root, onerror=lambda _: None,
                                                followlinks=follow_links):
        dirnames[:] = [d for d in dirnames if d not in ignore]
        for name in filenames:
            path = Path(current) / name
            try:
                if path.is_symlink():
                    continue
                stat = path.stat()
            except OSError:
                unreadable += 1
                continue
            count += 1
            if count > MAX_SCAN_FILES:
                warnings.append(
                    f"目录 {root} 文件太多，只检查了前 {MAX_SCAN_FILES} 个文件。")
                if unreadable:
                    skipped.append(f"{unreadable} 个文件读不到信息（多为权限限制），已跳过。")
                return
            yield path, stat
    if unreadable:
        skipped.append(f"{unreadable} 个文件读不到信息（多为权限限制），已跳过。")


# ================================================================ 系统概况

def read_uptime_seconds() -> float | None:
    """读取开机时长。"""
    if IS_LINUX:
        try:
            with open("/proc/uptime", encoding="ascii") as handle:
                return float(handle.read().split()[0])
        except (OSError, ValueError, IndexError):
            return None
    if IS_WINDOWS:
        try:
            return ctypes.windll.kernel32.GetTickCount64() / 1000
        except (AttributeError, OSError):
            return None
    return None


def collect_system() -> dict:
    """操作系统、主机、用户、Python 和时区。"""
    info: dict[str, object] = {
        "操作系统": f"{platform.system()} {platform.release()}",
        "系统版本号": platform.version(),
        "处理器架构": platform.machine(),
        "电脑名称": platform.node(),
        "当前用户": getpass.getuser(),
        "管理员权限": "是" if is_admin() else "否",
        "Python 版本": sys.version.split()[0],
        "Python 位置": sys.executable,
        "检查时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "时区": f"{time.tzname[0]}（UTC{time.strftime('%z')}）",
    }
    uptime = read_uptime_seconds()
    if uptime is not None:
        info["已开机"] = human_duration(uptime)
        if uptime > 30 * 86400:
            warnings.append(f"已连续开机 {human_duration(uptime)}，长期不重启可能让补丁无法生效。")
    return info


def is_admin() -> bool:
    """当前是不是管理员/root。只用于说明有些检查为什么读不到。"""
    if IS_WINDOWS:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def collect_cpu() -> dict:
    """CPU 核心数、型号和平均负载。"""
    info: dict[str, object] = {"逻辑核心数": os.cpu_count() or "未知"}
    if processor := platform.processor():
        info["型号"] = processor
    if hasattr(os, "getloadavg"):
        try:
            one, five, fifteen = os.getloadavg()
            info["平均负载"] = f"{one:.2f} / {five:.2f} / {fifteen:.2f}（1/5/15 分钟）"
            cores = os.cpu_count() or 1
            if one > cores:
                warnings.append(f"CPU 1 分钟平均负载 {one:.2f} 超过核心数 {cores}，系统偏忙。")
        except OSError:
            pass
    return info


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def collect_memory() -> dict:
    """内存和交换空间的使用情况。"""
    total = available = swap_total = swap_free = None

    if IS_WINDOWS:
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                total, available = status.ullTotalPhys, status.ullAvailPhys
                # Windows 的"页面文件"总量含物理内存，减掉才是虚拟部分
                swap_total = max(0, status.ullTotalPageFile - status.ullTotalPhys)
                swap_free = max(0, status.ullAvailPageFile - status.ullAvailPhys)
        except (AttributeError, OSError):
            pass
    elif IS_LINUX:
        values: dict[str, int] = {}
        try:
            with open("/proc/meminfo", encoding="ascii") as handle:
                for line in handle:
                    key, _, rest = line.partition(":")
                    number = rest.strip().split(" ")[0]
                    if number.isdigit():
                        values[key] = int(number) * 1024  # 文件里单位是 kB
        except OSError:
            pass
        total = values.get("MemTotal")
        available = values.get("MemAvailable", values.get("MemFree"))
        swap_total = values.get("SwapTotal")
        swap_free = values.get("SwapFree")

    if total is None or available is None:
        skipped.append("这个系统上读不到内存信息。")
        return {"状态": "读不到"}

    used = total - available
    percent = used / total * 100
    if percent >= 90:
        warnings.append(f"内存已使用 {percent:.0f}%，可用空间不多。")
    info = {
        "总量": human_size(total),
        "已使用": f"{human_size(used)}（{percent:.0f}%）",
        "可用": human_size(available),
    }
    if swap_total:
        swap_used = swap_total - (swap_free or 0)
        info["交换空间"] = f"{human_size(swap_used)} / {human_size(swap_total)}"
    elif swap_total == 0:
        info["交换空间"] = "未启用"
    return info


# ================================================================ 磁盘

def list_disk_targets() -> list[Path]:
    """列出要检查的磁盘位置。"""
    if IS_WINDOWS:
        drives = [Path(f"{letter}:\\") for letter in string.ascii_uppercase
                  if Path(f"{letter}:\\").exists()]
        return drives or [Path.cwd()]

    real_types = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "apfs",
                  "hfs", "vfat", "ntfs", "f2fs"}
    targets = [Path("/")]
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) >= 3 and fields[2] in real_types:
                    point = Path(fields[1])
                    if point not in targets:
                        targets.append(point)
    except OSError:
        pass
    return targets


def collect_disks() -> list[dict]:
    """每个磁盘的容量、已用和剩余。"""
    rows = []
    for target in list_disk_targets():
        try:
            total, used, free = shutil.disk_usage(target)
        except OSError:
            continue  # 空光驱、未插卡的读卡器会读不到
        percent = used / total * 100 if total else 0
        if percent >= 90:
            warnings.append(f"磁盘 {target} 已使用 {percent:.0f}%，剩余不足 10%。")
        rows.append({
            "位置": str(target), "总容量": human_size(total),
            "已使用": f"{human_size(used)}（{percent:.0f}%）",
            "剩余": human_size(free),
        })
    return rows


# ================================================================ 网络

def collect_network() -> dict:
    """主机名和本机 IP。不会连接外网。"""
    hostname = socket.gethostname()
    addresses: list[str] = []
    try:
        for family, _, _, _, sockaddr in socket.getaddrinfo(hostname, None):
            address = sockaddr[0]
            if family in (socket.AF_INET, socket.AF_INET6) and address not in addresses:
                addresses.append(address)
    except socket.gaierror:
        skipped.append("主机名解析失败，读不到本机 IP。")
    return {"主机名": hostname, "本机地址": "、".join(addresses) or "未取到"}


def collect_dns() -> dict:
    """正在使用的 DNS 服务器。只读配置，不发查询。"""
    servers: list[str] = []
    if IS_WINDOWS:
        output = run_readonly(["ipconfig", "/all"])
        if output is None:
            skipped.append("读不到 DNS 配置（ipconfig 不可用）。")
            return {"状态": "读不到"}
        collecting = False
        for line in output.splitlines():
            if "DNS" in line and ":" in line:
                collecting = True
                candidate = line.split(":", 1)[1].strip()
            elif collecting and line.startswith((" " * 20, "\t")) and ":" not in line:
                candidate = line.strip()
            else:
                collecting = False
                continue
            if IP_PATTERN.fullmatch(candidate) and candidate not in servers:
                servers.append(candidate)
    else:
        try:
            with open("/etc/resolv.conf", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.startswith("nameserver"):
                        parts = line.split()
                        if len(parts) >= 2 and parts[1] not in servers:
                            servers.append(parts[1])
        except OSError:
            skipped.append("读不到 /etc/resolv.conf。")
            return {"状态": "读不到"}
    if not servers:
        skipped.append("没有解析出 DNS 服务器地址。")
    return {"DNS 服务器": "、".join(servers) or "未取到"}


def collect_ports() -> list[dict]:
    """正在监听的端口。只查询，不改防火墙或服务。"""
    if IS_WINDOWS:
        output = run_readonly(["netstat", "-ano"])
        if output is None:
            skipped.append("读不到端口信息（netstat 不可用）。")
            return []
        rows = parse_netstat_windows(output, listening=True)
        if any(row.get("进程", "").startswith("PID ") for row in rows):
            skipped.append("部分端口只显示 PID：程序名属于其它账户，需要管理员权限才能看到。")
        return rows

    for command in (["ss", "-tulnp"], ["ss", "-tuln"], ["netstat", "-tuln"]):
        if output := run_readonly(command):
            return parse_netstat_unix(output)
    skipped.append("读不到端口信息（ss 和 netstat 都不可用）。")
    return []


def collect_connections(top: int = 20) -> list[dict]:
    """已经建立的外部连接。帮你看出有哪些程序在联网。"""
    if IS_WINDOWS:
        output = run_readonly(["netstat", "-ano"])
        if output is None:
            skipped.append("读不到连接信息（netstat 不可用）。")
            return []
        rows = parse_netstat_windows(output, listening=False)
    else:
        output = run_readonly(["ss", "-tunp"]) or run_readonly(["netstat", "-tun"])
        if output is None:
            skipped.append("读不到连接信息。")
            return []
        rows = [row for row in parse_netstat_unix(output)
                if row.get("状态") == "ESTAB" or "ESTABLISHED" in row.get("状态", "")]
    return rows[:top]


def parse_netstat_windows(output: str, listening: bool) -> list[dict]:
    """解析 Windows netstat -ano 的输出。"""
    names = windows_pid_names()
    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[0] not in ("TCP", "UDP"):
            continue
        protocol, local, remote = fields[0], fields[1], fields[2]
        state = fields[3] if protocol == "TCP" else ""
        pid = fields[-1]
        if listening:
            if protocol == "TCP" and state != "LISTENING":
                continue
            key = (protocol, local, "")
            row = {"协议": protocol, "监听地址": local}
        else:
            if protocol != "TCP" or state != "ESTABLISHED":
                continue
            key = (protocol, local, remote)
            row = {"协议": protocol, "本地": local, "对方": remote, "状态": state}
        if key in seen:
            continue
        seen.add(key)
        # 普通用户看不到其它账户的进程名，这时只显示 PID
        row["进程"] = f"{names[pid]}（PID {pid}）" if pid in names else f"PID {pid}"
        rows.append(row)
    return sorted(rows, key=lambda item: item.get("监听地址") or item.get("对方", ""))


def parse_netstat_unix(output: str) -> list[dict]:
    """解析 Linux/macOS 上 ss 或 netstat 的输出。"""
    rows: list[dict] = []
    seen: set[str] = set()
    for line in output.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 5 or not fields[0].upper().startswith(("TCP", "UDP")):
            continue
        protocol = fields[0].upper()
        state = fields[1] if fields[1].isupper() else ""
        local, remote = fields[4], fields[5] if len(fields) > 5 else ""
        key = f"{protocol}|{local}|{remote}"
        if key in seen:
            continue
        seen.add(key)
        row = {"协议": protocol, "监听地址": local, "状态": state}
        if remote and remote not in ("*:*", "0.0.0.0:*", "[::]:*"):
            row["对方"] = remote
        if process := re.search(r'users:\(\("([^"]+)".*?pid=(\d+)', line):
            row["进程"] = f"{process.group(1)}（PID {process.group(2)}）"
        rows.append(row)
    return sorted(rows, key=lambda item: item["监听地址"])


def windows_pid_names() -> dict[str, str]:
    """PID 到程序名的对照表，用来让端口列表更好读。"""
    rows = windows_processes()
    if not rows:
        return {}
    return {pid: name for _, name, pid in rows}


# ================================================================ 进程

def windows_processes() -> list[tuple[int, str, str]] | None:
    """Windows 上读进程列表。tasklist 被限制时改用 Get-Process。"""
    if output := run_readonly(["tasklist", "/fo", "csv", "/nh"]):
        rows = []
        for fields in csv.reader(io.StringIO(output)):
            if len(fields) < 5:
                continue
            digits = re.sub(r"[^\d]", "", fields[4])  # 形如 "123,456 K"
            if digits:
                rows.append((int(digits) * 1024, fields[0], fields[1]))
        if rows:
            return rows

    # tasklist 在部分环境下要管理员，Get-Process 普通用户就能用
    output = run_readonly([
        "powershell", "-NoProfile", "-Command",
        "Get-Process | Select-Object Name,Id,WorkingSet64 | ConvertTo-Csv -NoTypeInformation",
    ], timeout=30)
    if not output:
        return None
    rows = []
    for fields in csv.reader(io.StringIO(output)):
        if len(fields) >= 3 and fields[2].isdigit():
            rows.append((int(fields[2]), fields[0], fields[1]))
    return rows or None


def collect_processes(top: int = 10) -> list[dict]:
    """占内存最多的几个程序。"""
    rows: list[tuple[int, str, str]] = []

    if IS_WINDOWS:
        found = windows_processes()
        if found is None:
            skipped.append("读不到进程列表（tasklist 和 Get-Process 都不可用或被权限限制）。")
            return []
        rows = found
    elif IS_LINUX:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                text = (entry / "status").read_text(encoding="ascii", errors="replace")
            except OSError:
                continue  # 进程可能刚好退出了
            name = rss = None
            for line in text.splitlines():
                if line.startswith("Name:"):
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("VmRSS:"):
                    digits = re.sub(r"[^\d]", "", line)
                    rss = int(digits) * 1024 if digits else None
            if name and rss:
                rows.append((rss, name, entry.name))
    else:
        skipped.append("这个系统上没有实现进程检查。")
        return []

    rows.sort(key=lambda item: item[0], reverse=True)
    return [{"内存": human_size(size), "程序": name, "PID": pid}
            for size, name, pid in rows[:top]]


# ================================================================ 安全相关

def collect_firewall() -> dict:
    """防火墙是否开着。只读状态，不做任何改动。"""
    if IS_WINDOWS:
        output = run_readonly(["netsh", "advfirewall", "show", "allprofiles", "state"])
        if output is None or "elevation" in output.lower() or "管理员" in output:
            skipped.append("读不到防火墙状态：这项检查需要管理员权限，请用管理员身份再运行。")
            return {"状态": "需要管理员权限"}
        profiles: dict[str, str] = {}
        current = None
        for line in output.splitlines():
            text = line.strip()
            if not text:
                continue
            if ("配置文件" in text or "Profile" in text) and "设置" not in text:
                current = text.replace("设置", "").strip()
            elif current and ("状态" in text or "State" in text):
                value = text.split()[-1]
                profiles[current] = value
                if value.upper() in ("OFF", "关闭"):
                    warnings.append(f"防火墙配置「{current}」处于关闭状态。")
                current = None
        if not profiles:
            skipped.append("防火墙状态没能解析出来。")
            return {"状态": "没解析出结果"}
        return profiles

    if output := run_readonly(["ufw", "status"]):
        active = "active" in output.lower() and "inactive" not in output.lower()
        if not active:
            warnings.append("UFW 防火墙未启用。")
        return {"UFW": "启用" if active else "未启用"}
    if output := run_readonly(["systemctl", "is-active", "firewalld"]):
        state = output.strip()
        if state != "active":
            warnings.append(f"firewalld 状态为 {state}。")
        return {"firewalld": state}
    skipped.append("读不到防火墙状态（ufw / firewalld 都查不到，可能需要 root）。")
    return {"状态": "读不到"}


def collect_pending_reboot() -> dict:
    """系统是不是在等着重启才能完成更新。"""
    if IS_WINDOWS:
        keys = {
            "有待安装的更新": r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
            "有待重命名的文件": r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager",
        }
        pending = []
        for label, key in keys.items():
            if run_readonly(["reg", "query", key]) is not None and "Component" in key:
                pending.append(label)
        if pending:
            warnings.append("系统有待完成的更新，建议找合适的时间重启。")
        return {"需要重启": "是（" + "、".join(pending) + "）" if pending else "否"}

    marker = Path("/var/run/reboot-required")
    if marker.exists():
        warnings.append("系统提示需要重启才能完成更新。")
        return {"需要重启": "是"}
    return {"需要重启": "否"}


def collect_startup(top: int = 20) -> list[dict]:
    """开机自动启动的项目。只读注册表和启动文件夹。"""
    rows: list[dict] = []
    if IS_WINDOWS:
        for scope, key in (
            ("所有用户", r"HKLM\Software\Microsoft\Windows\CurrentVersion\Run"),
            ("当前用户", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"),
        ):
            output = run_readonly(["reg", "query", key])
            if output is None:
                continue
            for line in output.splitlines():
                fields = line.strip().split(None, 2)
                if len(fields) == 3 and fields[1].startswith("REG_"):
                    rows.append({"来源": scope, "名称": fields[0], "命令": fields[2]})
        appdata = os.environ.get("APPDATA")
        if appdata:
            folder = Path(appdata) / r"Microsoft\Windows\Start Menu\Programs\Startup"
            if folder.is_dir():
                for item in folder.iterdir():
                    rows.append({"来源": "启动文件夹", "名称": item.name, "命令": str(item)})
        if not rows:
            skipped.append("读不到开机启动项（reg 不可用）。")
    else:
        output = run_readonly(["systemctl", "list-unit-files", "--state=enabled", "--no-pager"])
        if output is None:
            skipped.append("读不到开机服务（systemctl 不可用）。")
            return []
        for line in output.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0].endswith(".service"):
                rows.append({"来源": "systemd", "名称": fields[0], "命令": fields[1]})
    return rows[:top]


def collect_users(top: int = 20) -> list[dict]:
    """系统上能登录的账户。只读，不改密码或权限。"""
    rows: list[dict] = []
    if IS_WINDOWS:
        output = run_readonly(["net", "user"])
        if output is None:
            skipped.append("读不到用户列表（net 不可用）。")
            return []
        started = False
        for line in output.splitlines():
            if line.startswith("---"):
                started = True
                continue
            if not started or line.startswith("命令") or "successfully" in line:
                continue
            for name in line.split():
                rows.append({"用户名": name})
    else:
        try:
            with open("/etc/passwd", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    fields = line.strip().split(":")
                    if len(fields) >= 7 and not fields[6].endswith(("nologin", "false")):
                        rows.append({"用户名": fields[0], "UID": fields[2], "登录 shell": fields[6]})
        except OSError:
            skipped.append("读不到 /etc/passwd。")
    return rows[:top]


# ================================================================ 文件分析

def collect_large_files(root: Path, top: int = 10) -> list[dict]:
    """最大的几个文件。只读大小，不打开内容。"""
    found = [(stat.st_size, path) for path, stat in walk_files(root)]
    found.sort(key=lambda item: item[0], reverse=True)
    return [{"大小": human_size(size), "文件": str(path)} for size, path in found[:top]]


def collect_recent_files(root: Path, top: int = 10) -> list[dict]:
    """最近改动过的文件。排查"刚才动了什么"时有用。"""
    found = [(stat.st_mtime, stat.st_size, path) for path, stat in walk_files(root)]
    found.sort(key=lambda item: item[0], reverse=True)
    return [{
        "修改时间": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
        "大小": human_size(size), "文件": str(path),
    } for mtime, size, path in found[:top]]


def collect_folder_sizes(root: Path, top: int = 10) -> list[dict]:
    """每个子文件夹各占多少空间。找占空间的大户很方便。"""
    totals: dict[str, int] = {}
    root = root.resolve()
    for path, stat in walk_files(root):
        try:
            relative = path.resolve().relative_to(root)
        except (ValueError, OSError):
            continue
        top_level = relative.parts[0] if len(relative.parts) > 1 else "（直接放在这一层的文件）"
        totals[top_level] = totals.get(top_level, 0) + stat.st_size
    ordered = sorted(totals.items(), key=lambda item: item[1], reverse=True)
    return [{"大小": human_size(size), "名称": name} for name, size in ordered[:top]]


def file_digest(path: Path, limit: int = MAX_HASH_BYTES) -> str | None:
    """算文件内容的指纹，用来判断是否真的重复。"""
    digest = hashlib.sha256()
    read = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                read += len(chunk)
                if read >= limit:
                    break
    except OSError:
        return None
    return digest.hexdigest()


def collect_duplicates(root: Path, top: int = 10) -> list[dict]:
    """找出内容相同的文件。只读取内容算指纹，不会删除任何东西。"""
    by_size: dict[int, list[Path]] = {}
    for path, stat in walk_files(root):
        if stat.st_size > 0:
            by_size.setdefault(stat.st_size, []).append(path)

    groups: list[dict] = []
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue  # 大小都不一样，肯定不重复
        by_hash: dict[str, list[Path]] = {}
        for path in paths:
            if digest := file_digest(path):
                by_hash.setdefault(digest, []).append(path)
        for duplicates in by_hash.values():
            if len(duplicates) > 1:
                groups.append({
                    "单份大小": human_size(size),
                    "可省空间": human_size(size * (len(duplicates) - 1)),
                    "份数": len(duplicates),
                    "文件": [str(item) for item in duplicates],
                })
    groups.sort(key=lambda item: item["份数"], reverse=True)
    if groups:
        warnings.append(f"在 {root} 下发现 {len(groups)} 组重复文件，可以手动清理。")
    return groups[:top]


def collect_temp() -> dict:
    """临时文件夹占了多少空间。只统计，不删除。"""
    temp_dir = Path(tempfile.gettempdir())
    total = count = 0
    for _, stat in walk_files(temp_dir):
        total += stat.st_size
        count += 1
    if total > 5 * GIB:
        warnings.append(f"临时文件夹已占 {human_size(total)}，可以考虑清理。")
    return {"位置": str(temp_dir), "文件数": count, "占用空间": human_size(total)}


# ================================================================ 排版输出

SIMPLE_SECTIONS = [
    ("系统信息", "system"), ("CPU", "cpu"), ("内存", "memory"),
    ("网络", "network"), ("DNS", "dns"), ("防火墙", "firewall"),
    ("重启状态", "reboot"), ("临时文件", "temp"),
]

TABLE_SECTIONS = [
    ("磁盘空间", "disks"), ("占内存最多的程序", "processes"),
    ("正在监听的端口", "ports"), ("已建立的连接", "connections"),
    ("开机启动项", "startup"), ("系统账户", "users"),
    ("最大的文件", "large_files"), ("最近改动的文件", "recent_files"),
    ("子文件夹占用", "folder_sizes"),
]


def render(report: dict) -> str:
    """把结果排成好读的文字。"""
    lines = [f"系统检查报告 v{VERSION}", ""]

    for title, key in SIMPLE_SECTIONS:
        if key not in report:
            continue
        lines.append(f"== {title} ==")
        lines.extend(f"{name}：{value}" for name, value in report[key].items())
        lines.append("")

    for title, key in TABLE_SECTIONS:
        if key not in report:
            continue
        lines.append(f"== {title} ==")
        rows = report[key]
        if not rows:
            lines.append("没有读到内容。")
        for row in rows:
            lines.append("  ".join(f"{value}" for value in row.values()))
        lines.append("")

    if "duplicates" in report:
        lines.append("== 重复文件 ==")
        if not report["duplicates"]:
            lines.append("没有发现重复文件。")
        for group in report["duplicates"]:
            lines.append(f"{group['份数']} 份 × {group['单份大小']}，可省 {group['可省空间']}")
            lines.extend(f"    {item}" for item in group["文件"])
        lines.append("")

    if report.get("warnings"):
        lines.append("== 需要注意 ==")
        lines.extend(f"- {item}" for item in report["warnings"])
        lines.append("")
    else:
        lines.extend(["没有发现需要注意的问题。", ""])

    if report.get("skipped"):
        lines.append("== 没能完成的检查 ==")
        lines.extend(f"- {item}" for item in report["skipped"])
        lines.append("")

    return "\n".join(lines)


# ================================================================ 入口

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读的系统检查工具：读取并显示系统状态，默认不修改任何设置。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"check_system {VERSION}")

    group = parser.add_argument_group("系统类检查（不需要指定目录）")
    group.add_argument("--ports", action="store_true", help="正在监听的端口")
    group.add_argument("--connections", action="store_true", help="已经建立的外部连接")
    group.add_argument("--processes", action="store_true", help="占内存最多的程序")
    group.add_argument("--startup", action="store_true", help="开机自动启动的项目")
    group.add_argument("--firewall", action="store_true", help="防火墙是否开启")
    group.add_argument("--reboot", action="store_true", help="是否有待完成的更新需要重启")
    group.add_argument("--dns", action="store_true", help="正在使用的 DNS 服务器")
    group.add_argument("--users", action="store_true", help="系统上能登录的账户")
    group.add_argument("--temp", action="store_true", help="临时文件夹占用的空间")
    group.add_argument("--all", action="store_true", help="打开上面所有检查")

    group = parser.add_argument_group("目录类检查（需要你指定一个目录）")
    group.add_argument("--large-files", metavar="目录", help="该目录下最大的文件")
    group.add_argument("--recent-files", metavar="目录", help="该目录下最近改动的文件")
    group.add_argument("--folder-sizes", metavar="目录", help="该目录下各子文件夹的占用")
    group.add_argument("--duplicates", metavar="目录", help="该目录下内容重复的文件")

    group = parser.add_argument_group("输出方式")
    group.add_argument("--top", type=int, default=10, metavar="数量",
                       help="各个排行榜显示前几名，默认 10")
    group.add_argument("--json", action="store_true", help="输出 JSON，方便别的程序处理")
    group.add_argument("--quiet", action="store_true", help="只显示需要注意的问题")
    group.add_argument("--output", metavar="文件",
                       help="把报告另外写入这个文件（唯一会写文件的选项）")
    return parser


def resolve_directory(value: str) -> Path | None:
    """确认用户给的目录真实存在。"""
    path = Path(value).expanduser()
    if not path.is_dir():
        print(f"找不到目录：{path}", file=sys.stderr)
        return None
    return path


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # 让中文在 Windows 的终端里正常显示
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    top = max(1, args.top)
    report: dict = {
        "version": VERSION,
        "system": collect_system(),
        "cpu": collect_cpu(),
        "memory": collect_memory(),
        "disks": collect_disks(),
        "network": collect_network(),
    }

    # 系统类检查
    if args.all or args.dns:
        report["dns"] = collect_dns()
    if args.all or args.firewall:
        report["firewall"] = collect_firewall()
    if args.all or args.reboot:
        report["reboot"] = collect_pending_reboot()
    if args.all or args.temp:
        report["temp"] = collect_temp()
    if args.all or args.processes:
        report["processes"] = collect_processes(top)
    if args.all or args.ports:
        report["ports"] = collect_ports()
    if args.all or args.connections:
        report["connections"] = collect_connections(top)
    if args.all or args.startup:
        report["startup"] = collect_startup(top)
    if args.all or args.users:
        report["users"] = collect_users(top)

    # 目录类检查
    for option, key, collector in (
        (args.large_files, "large_files", collect_large_files),
        (args.recent_files, "recent_files", collect_recent_files),
        (args.folder_sizes, "folder_sizes", collect_folder_sizes),
        (args.duplicates, "duplicates", collect_duplicates),
    ):
        if not option:
            continue
        directory = resolve_directory(option)
        if directory is None:
            return 2
        report[key] = collector(directory, top)

    report["warnings"] = warnings
    report["skipped"] = sorted(set(skipped))

    if args.json:
        text = json.dumps(report, ensure_ascii=False, indent=2)
    elif args.quiet:
        parts = [f"- {item}" for item in warnings] or ["没有发现需要注意的问题。"]
        text = "\n".join(parts)
    else:
        text = render(report)
    print(text)

    if args.output:
        destination = Path(args.output).expanduser()
        try:
            destination.write_text(text + "\n", encoding="utf-8")
        except OSError as error:
            print(f"报告没能写入 {destination}：{error}", file=sys.stderr)
            return 1
        print(f"报告已保存到：{destination}")

    # 有需要注意的问题时返回 1，方便接到别的脚本里
    return 1 if warnings else 0


if __name__ == "__main__":
    sys.exit(main())
