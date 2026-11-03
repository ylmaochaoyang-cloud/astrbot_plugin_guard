"""插件门卫 —— 只做事后补救。

不预检、不拦人、不管市场，只干三件事：
    拍快照  把当前能跑通的插件整棵树压成一份
    列快照  看看手里有哪些退路
    回滚    出事时把某个插件退回上一份

为什么只做这些：AstrBot 的安装动作在内核手里，第三方插件没有「装前」的钩子。
拦不住，就只能救。

命令：
    /pguard snap [插件名]     拍快照，不写名字就是全部
    /pguard list              列出所有快照
    /pguard rollback 名字     退回该插件最近一份快照
    /pguard rollback 名字 时间戳   退回指定那一份
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import zipfile

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

BASE = os.path.dirname(os.path.abspath(__file__))
SNAP_DIR = os.path.join(BASE, "snapshots")
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "snapshots"}

LOG_PATH = os.path.join(os.environ.get("USERPROFILE", ""), ".astrbot", "logs", "backend.log")
STARTUP_WAIT = 30          # 等日志写齐再自检
# 只认 star_manager 自己打的那几行——日志里混着工具参数的长行，宽松匹配会被它们骗
RE_LOADING = re.compile(r"\[star\.star_manager:\d+\]:\s+Loading plugin (\S+) \.\.\.")
RE_LOADED = re.compile(r"\[star\.star_manager:\d+\]:\s+Plugin (\S+) \([^)]*\) by")
RE_ERR = re.compile(r"\[star\.star_manager:\d+\]:.*?(?:failed to load|加载失败)\D{0,40}?(\S+)")
RE_STARTED = "[core.core_lifecycle:"


def _plugin_root() -> str:
    """插件根目录 —— 这个文件本身就住在它下面。"""
    return os.path.dirname(BASE)


def _list_plugins() -> list[str]:
    root = _plugin_root()
    try:
        names = os.listdir(root)
    except OSError:
        return []
    return sorted(n for n in names
                  if os.path.isdir(os.path.join(root, n)) and not n.startswith("."))


def _scan_startup() -> tuple[list[str], list[str]]:
    """从本次启动的位置往后读日志，返回（加载成功的，加载失败或没下文的）。"""
    if not os.path.exists(LOG_PATH):
        return [], []
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return [], []

    # 插件加载写在 "AstrBot started" 之前，所以区间要取「上一次启动」到「本次启动」
    starts = [i for i, line in enumerate(lines)
              if "AstrBot started" in line and RE_STARTED in line]
    if not starts:
        return [], []
    end = starts[-1]
    begin = starts[-2] + 1 if len(starts) > 1 else max(0, end - 4000)
    tail = lines[begin:end + 1]

    loading: dict[str, None] = {}
    ok: set[str] = set()
    bad: set[str] = set()
    for line in tail:
        m = RE_LOADING.search(line)
        if m:
            loading[m.group(1)] = None
            continue
        m = RE_LOADED.search(line)
        if m:
            ok.add(m.group(1))
            continue
        m = RE_ERR.search(line)
        if m:
            bad.add(m.group(1))

    # 开始了、没成功、也没明确报错的 —— 算「没下文」，按可疑处理
    missing = [n for n in loading if n not in ok]
    return sorted(ok), sorted(set(missing) | (bad & set(loading)))


def _snap(name: str) -> str:
    """把一个插件目录压成 zip，返回快照路径。"""
    src = os.path.join(_plugin_root(), name)
    if not os.path.isdir(src):
        raise FileNotFoundError(f"没有这个插件：{name}")

    os.makedirs(os.path.join(SNAP_DIR, name), exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(SNAP_DIR, name, f"{stamp}.zip")

    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for cur, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                full = os.path.join(cur, f)
                rel = os.path.relpath(full, src)
                zf.write(full, rel)
                count += 1

    meta = {"name": name, "stamp": stamp, "files": count,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"), "size": os.path.getsize(out)}
    with open(out + ".json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=1)
    logger.info("[pguard] 快照 %s -> %s（%d 个文件）", name, os.path.basename(out), count)
    return out


def _list_snaps(name: str | None = None) -> list[dict]:
    rows = []
    if not os.path.isdir(SNAP_DIR):
        return rows
    targets = [name] if name else sorted(os.listdir(SNAP_DIR))
    for plugin in targets:
        folder = os.path.join(SNAP_DIR, plugin)
        if not os.path.isdir(folder):
            continue
        for f in sorted(os.listdir(folder)):
            if not f.endswith(".zip"):
                continue
            meta_path = os.path.join(folder, f + ".json")
            meta = {"name": plugin, "stamp": f[:-4]}
            if os.path.exists(meta_path):
                try:
                    with open(meta_path, encoding="utf-8") as fh:
                        meta.update(json.load(fh))
                except Exception:
                    pass
            meta["path"] = os.path.join(folder, f)
            rows.append(meta)
    return rows


def _restore(name: str, stamp: str | None = None) -> str:
    """把插件目录换回某一份快照。原目录先挪开，不直接删。"""
    rows = [r for r in _list_snaps(name)]
    if not rows:
        raise FileNotFoundError(f"{name} 没有任何快照")
    target = rows[-1]
    if stamp:
        picked = [r for r in rows if r.get("stamp") == stamp]
        if not picked:
            raise FileNotFoundError(f"{name} 没有 {stamp} 这份快照")
        target = picked[0]

    root = _plugin_root()
    dst = os.path.join(root, name)
    if os.path.isdir(dst):
        aside = dst + ".before-rollback-" + time.strftime("%Y%m%d-%H%M%S")
        shutil.move(dst, aside)
        logger.warning("[pguard] 原目录挪到 %s", os.path.basename(aside))

    os.makedirs(dst, exist_ok=True)
    with zipfile.ZipFile(target["path"]) as zf:
        zf.extractall(dst)
    logger.warning("[pguard] 已回滚 %s 到 %s", name, target.get("stamp"))
    return target.get("stamp", "")


@register("astrbot_plugin_guard", "插件门卫",
          "只做事后补救：给插件留快照，崩了能一键退回上一份", "0.1.0")
class PluginGuard(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.auto_rollback = bool(self.config.get("auto_rollback", True))
        os.makedirs(SNAP_DIR, exist_ok=True)
        logger.info("[pguard] 门卫就绪，快照放在 %s", SNAP_DIR)

    async def initialize(self) -> None:
        asyncio.create_task(self._startup_check())

    async def _startup_check(self) -> None:
        """开机自检：等插件都加载完，看谁没能起来。"""
        await asyncio.sleep(STARTUP_WAIT)
        try:
            ok, bad = await asyncio.to_thread(_scan_startup)
        except Exception as exc:
            logger.warning("[pguard] 自检读日志出错：%s: %s", type(exc).__name__, exc)
            return

        logger.info("[pguard] 自检：成功 %d 个，可疑 %d 个", len(ok), len(bad))
        if not bad:
            return

        for name in bad:
            snaps = _list_snaps(name)
            if not snaps:
                logger.warning("[pguard] %s 没起来，手里又没它的快照，只能记一笔", name)
                continue
            if not self.auto_rollback:
                logger.warning("[pguard] %s 没起来，已有快照 %s，等你发话回滚",
                               name, snaps[-1].get("stamp"))
                continue
            try:
                used = await asyncio.to_thread(_restore, name)
                logger.warning("[pguard] %s 没起来，已自动回滚到 %s", name, used)
            except Exception as exc:
                logger.error("[pguard] %s 回滚失败：%s: %s", name, type(exc).__name__, exc)

    async def terminate(self) -> None:
        logger.info("[pguard] 已停止")

    @filter.command("pguard", alias={"门卫"})
    async def pguard(self, event: AstrMessageEvent):
        """插件门卫：snap / list / rollback"""
        raw = (event.message_str or "").strip()
        parts = raw.split()[1:]          # 去掉指令本身
        sub = parts[0].lower() if parts else "list"

        if sub == "snap":
            names = parts[1:] or _list_plugins()
            done, failed = [], []
            for n in names:
                try:
                    _snap(n)
                    done.append(n)
                except Exception as exc:
                    failed.append(f"{n}（{type(exc).__name__}: {exc}）")
            msg = f"拍了 {len(done)} 份快照：" + "、".join(done)
            if failed:
                msg += "\n没拍成的：" + "；".join(failed)
            yield event.plain_result(msg)
            return

        if sub == "list":
            rows = _list_snaps()
            if not rows:
                yield event.plain_result("手里还没有任何快照。用 /pguard snap 拍一份。")
                return
            lines = [f"共 {len(rows)} 份快照："]
            for r in rows[-20:]:
                lines.append(f"  {r['name']}  {r.get('stamp')}  {r.get('files', '?')} 个文件  {r.get('at', '')}")
            yield event.plain_result("\n".join(lines))
            return

        if sub == "rollback":
            if len(parts) < 2:
                yield event.plain_result("要回滚哪个？用法：/pguard rollback 插件名 [时间戳]")
                return
            name = parts[1]
            stamp = parts[2] if len(parts) > 2 else None
            try:
                used = _restore(name, stamp)
                yield event.plain_result(f"已把 {name} 退回快照 {used}。原目录留了备份，没删。")
            except Exception as exc:
                yield event.plain_result(f"回滚失败：{type(exc).__name__}: {exc}")
            return

        yield event.plain_result("用法：\n/pguard snap [插件名]\n/pguard list\n/pguard rollback 插件名 [时间戳]")
