# -*- coding: utf-8 -*-
"""ffmpeg 首次自动安装 —— 发布包不再内置 392MB 二进制

发布包剔除 tools/ffmpeg 后整包从 647MB 降到约 110MB。首次启动检测不到
完整的 ffmpeg + ffprobe 时，后台下载官方 Windows 构建，只解出两个 exe 到：

    <ComfyUI>/models/banzhang_tools/ffmpeg/bin

这个位置在插件目录之外 —— 以后更新插件只覆盖 ComfyUI-Banzhang-All，
这里原样保留，老用户永远不会重复下载同一份 ffmpeg。

关掉自动下载：环境变量 BANZHANG_NO_FFMPEG_DL=1
手动指定目录：环境变量 BANZHANG_FFMPEG_DIR（含 ffmpeg.exe 的目录）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import urllib.request
import zipfile
from pathlib import Path

from .banzhang_ffmpeg import find_binary

# 发布者可把自己网盘的直链填在第一位：国内比 GitHub 快，且不受上游改版影响。
# 留空则跳过，不会影响后面的官方源。
MIRROR_URL = ""

SOURCES = [
    ("gyan.dev 官方构建", "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"),
    ("GitHub 加速镜像", "https://gh-proxy.com/https://github.com/BtbN/FFmpeg-Builds/"
                       "releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"),
    ("GitHub BtbN 构建", "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/"
                        "ffmpeg-master-latest-win64-gpl.zip"),
]

# 只取这两个：ffplay（128MB）本插件从不调用，doc（9.6MB）纯文档
WANTED = ("ffmpeg.exe", "ffprobe.exe")

_LOCK = threading.Lock()
_RUNNING = False


def target_bin() -> Path:
    """安装落点：<ComfyUI>/models/banzhang_tools/ffmpeg/bin"""
    return Path(__file__).resolve().parents[2] / "models" / "banzhang_tools" / "ffmpeg" / "bin"


def ready() -> bool:
    """ffmpeg 和 ffprobe 都在才算齐：缺 ffprobe 探不出视频时长。"""
    return bool(find_binary("ffmpeg")) and bool(find_binary("ffprobe"))


def _verify(bin_dir: Path) -> bool:
    """跑一次 -version 确认能执行，避免把半截文件当成装好了。"""
    for name in ("ffmpeg", "ffprobe"):
        exe = bin_dir / (name + ".exe")
        if not exe.is_file():
            return False
        try:
            r = subprocess.run([str(exe), "-version"], capture_output=True, timeout=25)
        except Exception:
            return False
        if r.returncode != 0:
            return False
    return True


def _fetch(url: str, dst: Path, report, timeout: int = 30) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "Banzhang-All/2.1.1"})
    tmp = dst.parent / (dst.name + ".part")
    with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done, last_pct = 0, -5
        while True:
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total and report:
                pct = done * 100 // total
                if pct >= last_pct + 5:
                    last_pct = pct
                    report(pct, done, total)
    os.replace(tmp, dst)


def _extract(zip_path: Path, bin_dir: Path) -> None:
    """只解 bin 下的两个 exe + 根目录 LICENSE，不落 ffplay 和 doc。

    用 basename 白名单而非整包解压，顺带挡住 zip 里带 ../ 的路径穿越。
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    got = {}
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            base = name.rsplit("/", 1)[-1].lower()
            if base in WANTED and "/bin/" in "/" + name.lower():
                got[base] = info
        missing = [w for w in WANTED if w not in got]
        if missing:
            raise OSError("压缩包里没有 " + "、".join(missing) + "（下载源可能给的是源码包）")
        for base, info in got.items():
            with zf.open(info) as src, open(bin_dir / base, "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 512)
        for info in zf.infolist():
            if info.filename.replace("\\", "/").lower().rsplit("/", 1)[-1] == "license":
                with zf.open(info) as src, open(bin_dir.parent / "LICENSE.txt", "wb") as out:
                    shutil.copyfileobj(src, out)
                break


def _manual_hint() -> str:
    dest = target_bin()
    return ("[班长全家桶] ffmpeg 没能自动装上，视频类节点会缺音轨或不可用。\n"
            "  手动办法（任选一个，弄好不用重启也能被认出来）：\n"
            "  1) 装好 ffmpeg 后加进系统 PATH，或设环境变量 BANZHANG_FFMPEG_DIR 指向它的目录；\n"
            "  2) 自己下个 Windows 版 ffmpeg，把 ffmpeg.exe 和 ffprobe.exe 放到：\n"
            "     " + str(dest) + "\n"
            "  3) 也可以在插件的 banzhang_ffmpeg_boot.py 里把 MIRROR_URL 填成自己的网盘直链。")


def ensure_ffmpeg(log=print) -> bool:
    """确保 ffmpeg + ffprobe 可用。本地已有就直接返回，绝不联网。"""
    global _RUNNING
    with _LOCK:
        _RUNNING = True
        try:
            return _ensure_locked(log)
        finally:
            _RUNNING = False


def _ensure_locked(log) -> bool:
    if ready():
        return True

    flag = str(os.environ.get("BANZHANG_NO_FFMPEG_DL", "")).strip().lower()
    if flag in ("1", "true", "yes", "on"):
        log(_manual_hint())
        return False

    dest = target_bin()
    if _verify(dest):
        log("[班长全家桶] ffmpeg 已在缓存目录，直接复用：" + str(dest))
        return True

    urls = ([("班长网盘", MIRROR_URL)] if MIRROR_URL.strip() else []) + SOURCES
    zip_path = dest.parent / "ffmpeg-download.zip"
    dest.parent.mkdir(parents=True, exist_ok=True)

    for label, url in urls:
        try:
            log("[班长全家桶] 首次使用，正在从" + label + "下载 ffmpeg（约 90-190MB，只需一次）...")

            def report(pct, done, total, _label=label):
                log("[班长全家桶]   " + _label + " " + str(pct) + "%  "
                    + str(done // 1048576) + "/" + str(total // 1048576) + " MB")

            _fetch(url, zip_path, report)
            log("[班长全家桶] 下载完成，正在解出 ffmpeg.exe / ffprobe.exe ...")
            _extract(zip_path, dest)
            if _verify(dest):
                log("[班长全家桶] ffmpeg 安装完成：" + str(dest))
                return True
            raise OSError("解出来的程序无法执行（可能被杀毒软件拦截）")
        except Exception as exc:
            log("[班长全家桶] " + label + " 失败：" + str(exc))
        finally:
            try:
                zip_path.unlink()
            except OSError:
                pass

    log(_manual_hint())
    return False


def start_background_install(log=print) -> None:
    """后台装，不阻塞 ComfyUI 启动。缺 ffmpeg 时视频节点仍会注册，装好即生效。"""
    if _RUNNING or _LOCK.locked():
        return
    t = threading.Thread(target=ensure_ffmpeg, kwargs={"log": log},
                         daemon=True, name="banzhang-ffmpeg-install")
    t.start()
