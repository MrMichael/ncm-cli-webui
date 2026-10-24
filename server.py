#!/usr/bin/env python3
"""ncm-webctl — 局域网遥控 ncm-cli / mpv 播放。

只用标准库。控制面分两路：
  - mpv JSON IPC (unix socket)：播放/暂停/进度/播放器音量/单曲循环，毫秒级
  - ncm-cli 子进程：曲名、队列、换曲、歌单与搜索数据

播放模式在应用层实现（daemon 没有可用的模式接口）：
  sequential  顺序播放，队列空了就停
  loop-list   歌单循环，检测到停止后重放上一个歌单
  loop-one    单曲循环，交给 mpv 的 loop-file
"""

import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOME = os.path.expanduser("~")
MPV_SOCK = os.path.join(HOME, ".config", "ncm-cli", "mpv.sock")
DATA_DIR = os.path.join(HOME, ".config", "ncm-webctl")
TOKEN_FILE = os.path.join(DATA_DIR, "token")
LAST_FILE = os.path.join(DATA_DIR, "last.json")
NCM = shutil.which("ncm-cli") or os.path.join(HOME, ".local", "bin", "ncm-cli")
PORT = int(os.environ.get("NCM_WEBCTL_PORT", "8787"))
COOKIE = "ncm_token"

QUEUE_LOCK = threading.RLock()
_cache = {}

# 运行时状态（内存，重启即失）
RT = {
    "mode": "sequential",
    "deadline": 0.0,        # 限时播放截止时间戳
    "expired": False,       # 定时器触发过，抑制歌单循环重放
    "was_playing": False,
    "last_replay": 0.0,
    "gen": 0,               # 后台追加的世代号
    "last": None,           # {"enc","orig","name"}
    "stopped_ticks": 0,
    "replay_guard": 0.0,
    "reloading": False,
}


# ── mpv IPC ────────────────────────────────────────────────────────────────


def mpv(*commands, timeout=2.0):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(MPV_SOCK)
        f = s.makefile("rwb")
        for cmd in commands:
            f.write(json.dumps({"command": list(cmd)}).encode() + b"\n")
        f.flush()
        out = []
        for _ in commands:
            line = f.readline()
            if not line:
                break
            out.append(json.loads(line))
        s.close()
        return out
    except Exception:
        return None


def mpv_props(*names):
    resp = mpv(*[("get_property", n) for n in names])
    if not resp:
        return [None] * len(names)
    return [r.get("data") if r.get("error") == "success" else None for r in resp]


# ── ncm-cli ────────────────────────────────────────────────────────────────


def ncm_run(*args, timeout=20):
    try:
        subprocess.run([NCM, *args], capture_output=True, text=True, timeout=timeout)
        _cache.pop("ncm", None)
        return True
    except Exception:
        return False


def ncm_json(*args, timeout=20):
    try:
        r = subprocess.run([NCM, *args], capture_output=True, text=True, timeout=timeout)
        return json.loads(r.stdout or "{}")
    except Exception:
        return {}


def records_of(payload):
    d = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        return d.get("records") or []
    return []


def cached(key, ttl, factory):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = factory()
    _cache[key] = (time.time(), val)
    return val


def ncm_state():
    return (ncm_json("state").get("state") or {})


def system_volume():
    try:
        r = subprocess.run(
            ["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
            capture_output=True, text=True, timeout=5,
        )
        m = re.search(r"(\d+)%", r.stdout)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def wait_until(pred, timeout, step=0.25):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def raw_playback():
    paused, pos, dur, vol, idle = mpv_props(
        "pause", "time-pos", "duration", "volume", "idle-active"
    )
    return {
        "paused": paused, "pos": pos, "dur": dur, "vol": vol,
        "idle": idle, "reachable": paused is not None,
    }


def is_stopped():
    p = raw_playback()
    if not p["reachable"]:
        return True
    return bool(p["idle"]) or p["paused"] is None


def build_state():
    p = raw_playback()
    st = cached("ncm", 2.0, ncm_state)
    sysvol = cached("sysvol", 2.0, system_volume)

    if not p["reachable"]:
        # mpv 的 IPC socket 丢了（例如端口竞态）但音频可能还在放，回退用 daemon 的状态
        status = "playing" if st.get("status") == "playing" else "stopped"
    elif p["idle"] or p["paused"] is None:
        status = "stopped"
    else:
        status = "paused" if p["paused"] else "playing"

    remaining = 0
    if RT["deadline"]:
        remaining = max(0, int(round(RT["deadline"] - time.time())))

    return {
        "status": status,
        "title": st.get("title") or "",
        "position": round(p["pos"] or 0, 2),
        "duration": round(p["dur"] or 0, 2),
        "playerVolume": int(round(p["vol"] or 0)),
        "systemVolume": sysvol,
        "queueLength": st.get("queueLength") or 0,
        "currentIndex": (st.get("currentIndex") or 0) + 1,
        "reachable": p["reachable"],
        "mode": RT["mode"],
        "timerRemaining": remaining,
        "lastName": (RT["last"] or {}).get("name") or "",
        "reloading": RT["reloading"],
    }


# ── 持久化"上一个歌单" ──────────────────────────────────────────────────────


def save_last(enc, orig, name):
    RT["last"] = {"enc": enc, "orig": orig, "name": name}
    try:
        os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
        with open(LAST_FILE, "w") as fh:
            json.dump({"enc": enc, "orig": orig, "name": name}, fh, ensure_ascii=False)
    except Exception:
        pass


def load_last():
    """优先用自己记的；没有就回退到 ncm-cli 自己的 play-session.json。"""
    try:
        with open(LAST_FILE) as fh:
            d = json.load(fh)
        if d.get("enc"):
            RT["last"] = d
            return
    except Exception:
        pass

    try:
        with open(os.path.join(HOME, ".config", "ncm-cli", "play-session.json")) as fh:
            sess = json.load(fh)
    except Exception:
        return
    if sess.get("sourceType") != "list" or not sess.get("sourceId"):
        return

    enc = sess["sourceId"]
    orig, name = None, "上次的歌单"
    try:
        info = ncm_json("playlist", "get", "--playlistId", enc,
                        "--userInput", "恢复上次播放的歌单")
        d = info.get("data") or {}
        orig = d.get("originalId")
        name = d.get("name") or name
    except Exception:
        pass
    RT["last"] = {"enc": enc, "orig": orig, "name": name}


# ── 播放动作 ───────────────────────────────────────────────────────────────


def apply_mode():
    """把当前模式落到 mpv（只有单曲循环需要动 mpv）。"""
    if RT["mode"] == "loop-one":
        mpv(("set_property", "loop-file", "inf"))
    else:
        mpv(("set_property", "loop-file", "no"))


def clear_and_wait():
    ncm_run("queue", "clear")
    wait_until(is_stopped, 3.0)


def start_playlist(enc, orig, name, skip=0):
    """整单播放。skip>0 时用 next 跳过前 skip 首（仅用于"从第 N 首开始"）。"""
    with QUEUE_LOCK:
        RT["gen"] += 1
        RT["expired"] = False
        print(f"[play] 加载歌单 {name}…", flush=True)
        try:
            clear_and_wait()
            ok = ncm_run("play", "--playlist", "--encrypted-id", enc,
                         "--original-id", str(orig), timeout=30)
            started = wait_until(
                lambda: build_state()["status"] != "stopped" or _queue_len() > 0, 10.0
            )
            for _ in range(skip):
                ncm_run("next")
                time.sleep(0.15)
            save_last(enc, orig, name)
            apply_mode()
            RT["was_playing"] = started
            print(f"[play] 开始播放={started}", flush=True)
            return ok and started
        finally:
            pass  # start_playlist 不再需要冷却：歌单循环改用"提前补货"，不重放


def start_track(song_enc, song_orig, playlist=None, index=None):
    """立刻播一首；给了歌单信息就在后台把后续曲目追加进队列。"""
    with QUEUE_LOCK:
        RT["gen"] += 1
        gen = RT["gen"]
        RT["expired"] = False
        clear_and_wait()
        ok = ncm_run("play", "--song", "--encrypted-id", song_enc,
                     "--original-id", str(song_orig), timeout=20)
        wait_until(lambda: not is_stopped(), 6.0)
        if playlist:
            save_last(playlist[0], playlist[1], playlist[2])
        apply_mode()
        RT["was_playing"] = True

    if playlist and index is not None:
        threading.Thread(
            target=append_rest, args=(gen, playlist[0], index + 1), daemon=True
        ).start()
    return ok


def append_rest(gen, enc, start_index):
    """后台把歌单剩下部分追加到队列；世代号变了就退出。"""
    tracks = playlist_tracks(enc)
    for t in tracks[start_index:]:
        if gen != RT["gen"]:
            return
        ncm_run("queue", "add", "--encrypted-id", t["enc"],
                "--original-id", str(t["orig"]))
        time.sleep(0.05)


def _queue_len():
    try:
        st = build_state()
        return st["queueLength"]
    except Exception:
        return 0


# ── 歌单 / 曲目数据 ────────────────────────────────────────────────────────


def my_playlists(kind):
    def fetch():
        cmd = "collected" if kind == "collected" else "created"
        data = ncm_json("playlist", cmd, "--limit", "60",
                        "--userInput", "浏览我的收藏歌单")
        out = []
        for r in records_of(data):
            if not r.get("id"):
                continue
            out.append({
                "enc": r["id"],
                "orig": r.get("originalId"),
                "name": r.get("name") or "",
                "count": r.get("trackCount") or 0,
                "play": r.get("playCount") or 0,
            })
        return out
    return cached(("mypl", kind), 300, fetch)


def playlist_tracks(enc):
    def fetch():
        data = ncm_json("playlist", "tracks", "--playlistId", enc, "--limit", "200",
                        "--userInput", "浏览歌单曲目")
        out = []
        for r in records_of(data):
            if not (r.get("visible") and r.get("playFlag")):
                continue
            out.append({
                "enc": r["id"],
                "orig": r.get("originalId"),
                "name": r.get("name") or "",
                "artist": (r.get("artists") or [{}])[0].get("name") or "",
                "ms": r.get("duration") or 0,
            })
        return out
    return cached(("tracks", enc), 300, fetch)


# ── 看门狗：限时播放 + 歌单循环 ────────────────────────────────────────────


def tick():
    st = build_state()

    if st["status"] != RT.get("prev_status"):
        RT["prev_status"] = st["status"]
        print(f"[状态] {st['status']} mode={RT['mode']} was_playing={RT['was_playing']} "
              f"expired={RT['expired']} q={st['queueLength']} last={(RT['last'] or {}).get('name')}",
              flush=True)

    if st["status"] == "playing":
        RT["was_playing"] = True

    if RT["deadline"] and time.time() >= RT["deadline"]:
        RT["deadline"] = 0
        RT["expired"] = True
        RT["was_playing"] = False
        ncm_run("stop")
        return

    # 歌单循环：确认停了（连续两次 tick）再重放，45 秒冷却防止反复触发
    if st["status"] == "stopped":
        RT["stopped_ticks"] += 1
    else:
        RT["stopped_ticks"] = 0

    if (
        RT["mode"] == "loop-list"
        and not RT["expired"]
        and RT["was_playing"]
        and st["status"] == "stopped"
        and RT["stopped_ticks"] >= 2
        and RT["last"]
        and time.time() > RT["replay_guard"]
    ):
        RT["replay_guard"] = time.time() + 45
        RT["stopped_ticks"] = 0
        RT["was_playing"] = False
        last = dict(RT["last"])
        print(f"[replay] 歌单循环重放: {last.get('name')}", flush=True)
        threading.Thread(target=replay_worker, args=(last,), daemon=True).start()


def replay_worker(last):
    RT["reloading"] = True
    try:
        start_playlist(last["enc"], last.get("orig"), last.get("name") or "")
    except Exception:
        import traceback
        print("[replay] 失败: " + traceback.format_exc(), flush=True)
    finally:
        RT["reloading"] = False


def watchdog():
    while True:
        time.sleep(1.5)
        try:
            tick()
        except Exception as exc:
            import traceback
            print("[watchdog] " + traceback.format_exc(), flush=True)


# ── 鉴权 ───────────────────────────────────────────────────────────────────


def load_token():
    if os.path.exists(TOKEN_FILE):
        tok = open(TOKEN_FILE).read().strip()
        if tok:
            return tok
    os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
    tok = secrets.token_urlsafe(18)
    with open(TOKEN_FILE, "w") as fh:
        fh.write(tok)
    os.chmod(TOKEN_FILE, stat.S_IRUSR | stat.S_IWUSR)
    return tok


TOKEN = load_token()


def lan_addresses():
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return []
    ips = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[1].startswith(("docker", "br-", "veth")):
            continue
        ips.append(parts[3].split("/")[0])
    return ips


# ── 页面 ───────────────────────────────────────────────────────────────────

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#12161f">
<title>儿歌遥控</title>
<style>
  :root{
    --base:#12161f; --surface:#1b2130; --surface-2:#232b3d; --line:#2b3448;
    --text:#edeff5; --muted:#8a93a8; --faint:#5f6779;
    --amber:#f0a65a; --amber-soft:#4a3a26; --ring:rgba(240,166,90,.35);
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  html,body{height:100%}
  body{
    margin:0; background:var(--base); color:var(--text);
    font-family:ui-rounded,"SF Pro Rounded","Segoe UI",system-ui,-apple-system,sans-serif;
    display:flex; align-items:center; justify-content:center;
    padding:20px 20px calc(20px + env(safe-area-inset-bottom));
    background-image:radial-gradient(120% 60% at 50% 0%,#1a2030 0%,var(--base) 60%);
  }
  main{width:100%; max-width:380px; display:flex; flex-direction:column; gap:18px}

  header{display:flex; align-items:center; justify-content:space-between; gap:10px}
  .badge{font-size:12px; color:var(--muted); display:inline-flex; align-items:center; gap:6px}
  .dot{width:7px;height:7px;border-radius:50%;background:var(--muted)}
  .dot.playing{background:var(--amber); box-shadow:0 0 10px var(--ring)}
  .dot.paused{background:#c8b06a}
  .head-right{display:flex; align-items:center; gap:8px}
  .chip{
    font-size:12px; padding:5px 10px; border-radius:99px; background:var(--surface);
    color:var(--amber); border:1px solid var(--line); font-variant-numeric:tabular-nums;
  }
  .chip.hidden{display:none}
  .iconbtn{
    font-size:12px; padding:5px 11px; border-radius:99px; background:var(--surface);
    border:1px solid var(--line); color:var(--text);
  }

  .title{font-size:21px; line-height:1.3; font-weight:600; letter-spacing:-.01em; margin:0}
  .title .sub{display:block; color:var(--muted); font-size:14px; font-weight:400; margin-top:4px}

  .rail{height:34px; margin:0 -10px; padding:14px 10px; cursor:pointer; touch-action:none;
        display:flex; align-items:center}
  .track{position:relative; flex:1; height:5px; border-radius:99px; background:var(--surface-2)}
  .fill{position:absolute; inset:0 auto 0 0; width:0%; border-radius:99px; background:var(--amber)}
  .knob{position:absolute; top:50%; left:0%; width:13px; height:13px; margin:-6.5px 0 0 -6.5px;
        border-radius:50%; background:var(--amber); box-shadow:0 0 0 4px var(--ring);
        opacity:0; pointer-events:none; transition:opacity .15s}
  body.seeking .knob{opacity:1}
  .times{display:flex; justify-content:space-between; font-size:12px; color:var(--muted);
         font-variant-numeric:tabular-nums; margin-top:-8px}

  .controls{display:flex; align-items:center; justify-content:center; gap:26px; margin:2px 0}
  button{font:inherit; color:inherit; background:none; border:0; padding:0; cursor:pointer}
  .step{width:54px;height:54px;border-radius:50%;background:var(--surface);
        display:grid;place-items:center;transition:transform .12s,background .15s}
  .step:active{transform:scale(.92); background:var(--surface-2)}
  .step svg{width:21px;height:21px;fill:var(--text)}
  .play{width:84px;height:84px;border-radius:50%;background:var(--amber);
        display:grid;place-items:center;box-shadow:0 6px 26px rgba(240,166,90,.22);
        transition:transform .12s}
  .play:active{transform:scale(.94)}
  .play svg{width:29px;height:29px;fill:#1a1409}

  .modes{display:flex; gap:8px; justify-content:center}
  .resume{
    display:block; width:100%; padding:12px 14px; border-radius:12px; font-size:14px;
    background:var(--amber-soft); border:1px solid #6b5230; color:var(--amber);
    margin-top:-6px;
  }
  .resume.hidden{display:none}
  .resume span{display:block; font-size:11px; color:#b98c54; margin-top:3px;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
  .mode{
    flex:1; padding:9px 0; font-size:13px; border-radius:11px; background:var(--surface);
    border:1px solid var(--line); color:var(--muted);
  }
  .mode.on{background:var(--amber-soft); border-color:#6b5230; color:var(--amber)}

  .volumes{display:flex; flex-direction:column; gap:12px}
  .vrow{display:grid; grid-template-columns:74px 1fr 44px; align-items:center; gap:12px}
  .vrow label{font-size:13px; color:var(--muted)}
  .vrow output{font-size:13px; text-align:right; font-variant-numeric:tabular-nums}
  input[type=range]{-webkit-appearance:none; appearance:none; width:100%; height:26px; background:transparent}
  input[type=range]::-webkit-slider-runnable-track{height:5px;border-radius:99px;background:var(--surface-2)}
  input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:21px;height:21px;border-radius:50%;
    background:var(--text);margin-top:-8px}
  input[type=range]::-moz-range-track{height:5px;border-radius:99px;background:var(--surface-2)}
  input[type=range]::-moz-range-thumb{width:21px;height:21px;border:0;border-radius:50%;background:var(--text)}

  /* 底部抽屉 */
  .scrim{position:fixed; inset:0; background:rgba(6,8,13,.6); opacity:0; pointer-events:none;
         transition:opacity .2s; z-index:9}
  .scrim.open{opacity:1; pointer-events:auto}
  .sheet{
    position:fixed; left:0; right:0; bottom:0; z-index:10; display:flex; flex-direction:column;
    background:var(--surface); border-radius:20px 20px 0 0; border-top:1px solid var(--line);
    max-height:82vh; transform:translateY(102%); transition:transform .26s cubic-bezier(.2,.8,.3,1);
    padding-bottom:env(safe-area-inset-bottom);
  }
  .sheet.open{transform:none}
  .sheet-head{display:flex; align-items:center; gap:10px; padding:16px 18px 10px}
  .sheet-head h2{font-size:15px; font-weight:600; margin:0; flex:1; text-align:center}
  .ghost{font-size:14px; color:var(--muted); padding:4px 2px; min-width:44px}
  .ghost.left{text-align:left}
  .tabs{display:flex; gap:8px; padding:0 18px 10px}
  .tab{flex:1; padding:8px 0; border-radius:10px; font-size:13px; color:var(--muted);
       background:var(--surface-2); border:1px solid transparent}
  .tab.on{color:var(--text); border-color:var(--line); background:#28324a}
  .list{overflow-y:auto; -webkit-overflow-scrolling:touch; padding:0 8px 14px}
  .row{display:flex; align-items:center; gap:12px; padding:11px 10px; border-radius:12px}
  .row:active{background:var(--surface-2)}
  .row .info{flex:1; min-width:0}
  .row .nm{font-size:15px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
  .row .meta{font-size:12px; color:var(--muted); margin-top:2px}
  .row .go{font-size:12px; color:var(--amber); padding:7px 10px; border-radius:8px;
           background:var(--amber-soft); white-space:nowrap}
  .empty{padding:26px 16px; text-align:center; color:var(--faint); font-size:13px; line-height:1.6}
  .spin{display:inline-block; animation:sp 1s linear infinite}
  @keyframes sp{to{transform:rotate(360deg)}}

  .timer-grid{display:grid; grid-template-columns:repeat(2,1fr); gap:10px; padding:4px 18px 20px}
  .timer-grid button{padding:14px 0; border-radius:12px; background:var(--surface-2);
    border:1px solid var(--line); font-size:14px}
  .timer-grid button.cancel{color:var(--muted); grid-column:1 / -1}

  .hint{font-size:12px; color:var(--faint); text-align:center; line-height:1.6; margin:0}
  .offline{position:fixed; inset:auto 0 0 0; padding:10px; text-align:center; font-size:13px;
    background:#3a1f1f; color:#ffd7d7; transform:translateY(100%); transition:transform .25s; z-index:20}
  .offline.show{transform:none}
  @media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style>
</head>
<body>
<main>
  <header>
    <span class="badge"><span class="dot" id="dot"></span><span id="statusText">连接中</span></span>
    <span class="head-right">
      <span class="chip hidden" id="timerChip">⏱ <span id="timerText"></span></span>
      <button class="iconbtn" id="openSheet">选歌</button>
    </span>
  </header>

  <section>
    <h1 class="title"><span id="title">—</span><span class="sub" id="artist"></span></h1>
    <div class="rail" id="rail">
      <div class="track"><div class="fill" id="fill"></div><div class="knob" id="knob"></div></div>
    </div>
    <div class="times"><span id="cur">0:00</span><span id="dur">0:00</span></div>
  </section>

  <section class="controls">
    <button class="step" id="prev" aria-label="上一首">
      <svg viewBox="0 0 24 24"><path d="M6 5h2.5v14H6zM20 5v14L9.5 12z"/></svg>
    </button>
    <button class="play" id="toggle" aria-label="播放或暂停">
      <svg id="icon" viewBox="0 0 24 24"><path d="M7 4.5v15l13-7.5z"/></svg>
    </button>
    <button class="step" id="next" aria-label="下一首">
      <svg viewBox="0 0 24 24"><path d="M15.5 5H18v14h-2.5zM4 5v14l10.5-7z"/></svg>
    </button>
  </section>

  <button id="resume" class="resume hidden">继续上次<span id="resumeName"></span></button>

  <section class="modes">
    <button class="mode" data-mode="sequential">顺序播放</button>
    <button class="mode" data-mode="loop-list">歌单循环</button>
    <button class="mode" data-mode="loop-one">单曲循环</button>
  </section>

  <section class="volumes">
    <div class="vrow">
      <label for="pv">播放音量</label>
      <input id="pv" type="range" min="0" max="100" step="1" value="100">
      <output id="pvo">100%</output>
    </div>
    <div class="vrow">
      <label for="sv">系统音量</label>
      <input id="sv" type="range" min="0" max="100" step="1" value="60">
      <output id="svo">60%</output>
    </div>
  </section>

  <p class="hint"><button id="openTimer" style="color:var(--muted);text-decoration:underline">限时播放</button>　·　空格 播放暂停　←/→ 换曲</p>
</main>

<div class="scrim" id="scrim"></div>

<section class="sheet" id="sheet">
  <div class="sheet-head">
    <button class="ghost left" id="back" style="visibility:hidden">‹ 返回</button>
    <h2 id="sheetTitle">我的歌单</h2>
    <button class="ghost" id="closeSheet">关闭</button>
  </div>
  <div class="tabs" id="tabs">
    <button class="tab on" data-kind="collected">收藏</button>
    <button class="tab" data-kind="created">我创建</button>
  </div>
  <div class="list" id="list"></div>
</section>

<section class="sheet" id="timerSheet">
  <div class="sheet-head">
    <button class="ghost left" style="visibility:hidden">占位</button>
    <h2>限时播放</h2>
    <button class="ghost" id="closeTimer">关闭</button>
  </div>
  <div class="timer-grid" id="timerGrid">
    <button data-min="15">15 分钟</button>
    <button data-min="30">30 分钟</button>
    <button data-min="60">1 小时</button>
    <button data-min="90">1.5 小时</button>
    <button data-min="180">3 小时</button>
    <button data-min="0" class="cancel">取消限时</button>
  </div>
</section>

<div class="offline" id="offline">连不上 ncm-cli 播放服务</div>

<script>
const $ = (id) => document.getElementById(id);
const fmt = (s) => {
  s = Math.max(0, Math.floor(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  const mm = String(m).padStart(h ? 2 : 1, "0"), sec = String(ss).padStart(2, "0");
  return h ? h + ":" + mm + ":" + sec : mm + ":" + sec;
};
const ICONS = { play: "M7 4.5v15l13-7.5z", pause: "M7 4.5h4v15H7zM13 4.5h4v15h-4z" };

let state = null, seeking = false, dragging = { pv: false, sv: false };
let view = { level: "lists", kind: "collected", playlist: null };

async function api(path, body) {
  const opt = body === undefined
    ? { method: "POST" }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  return r.json();
}

async function refresh() {
  try {
    const r = await fetch("/api/state", { cache: "no-store" });
    if (r.status === 401) { location.href = "/"; return; }
    state = await r.json();
    $("offline").classList.toggle("show", !state.reachable);
  } catch (e) {
    $("offline").classList.add("show");
    return;
  }
  render();
}

function render() {
  const s = state;
  if (!s) return;
  const label = { playing: "播放中", paused: "已暂停", stopped: "已停止" }[s.status] || s.status;
  $("statusText").textContent = label;
  $("dot").className = "dot " + s.status;

  const [song, ...rest] = (s.title || "").split(" - ");
  $("title").textContent = s.title ? song : (s.status === "stopped" ? "没有在播放" : "—");
  $("artist").textContent = rest.join(" - ");
  $("icon").firstChild.setAttribute("d", s.status === "playing" ? ICONS.pause : ICONS.play);

  const pct = s.duration ? Math.min(100, (s.position / s.duration) * 100) : 0;
  if (!seeking) {
    $("fill").style.width = pct + "%";
    $("knob").style.left = pct + "%";
    $("cur").textContent = fmt(s.position);
  }
  $("dur").textContent = fmt(s.duration);

  document.querySelectorAll(".mode").forEach((b) => b.classList.toggle("on", b.dataset.mode === s.mode));

  const tc = $("timerChip");
  if (s.timerRemaining > 0) { tc.classList.remove("hidden"); $("timerText").textContent = fmt(s.timerRemaining); }
  else tc.classList.add("hidden");

  if (!dragging.pv) { $("pv").value = s.playerVolume; $("pvo").value = s.playerVolume + "%"; }
  if (!dragging.sv && s.systemVolume !== null) { $("sv").value = s.systemVolume; $("svo").value = s.systemVolume + "%"; }

  const showResume = s.status === "stopped" && s.lastName;
  $("resume").classList.toggle("hidden", !showResume);
  if (showResume) $("resumeName").textContent = s.lastName;
}

$("toggle").onclick = () => api("/api/toggle").then(() => setTimeout(refresh, 150));
$("resume").onclick = () => api("/api/resume-last").then(() => setTimeout(refresh, 600));
$("next").onclick = () => api("/api/next").then(() => setTimeout(refresh, 300));
$("prev").onclick = () => api("/api/prev").then(() => setTimeout(refresh, 300));

document.querySelectorAll(".mode").forEach((b) => {
  b.onclick = () => api("/api/mode", { mode: b.dataset.mode }).then(() => setTimeout(refresh, 150));
});

/* 进度条 */
const seekTo = (clientX) => {
  const r = $("rail").getBoundingClientRect();
  const ratio = Math.min(1, Math.max(0, (clientX - r.left) / r.width));
  const t = ratio * (state?.duration || 0);
  $("fill").style.width = ratio * 100 + "%";
  $("knob").style.left = ratio * 100 + "%";
  $("cur").textContent = fmt(t);
  return t;
};
$("rail").addEventListener("mousedown", (e) => { seeking = true; document.body.classList.add("seeking"); seekTo(e.clientX); });
$("rail").addEventListener("touchstart", (e) => { seeking = true; document.body.classList.add("seeking"); seekTo(e.touches[0].clientX); e.preventDefault(); }, { passive: false });
window.addEventListener("mousemove", (e) => { if (seeking) seekTo(e.clientX); });
window.addEventListener("touchmove", (e) => { if (seeking) { seekTo(e.touches[0].clientX); e.preventDefault(); } }, { passive: false });
const endSeek = (x) => {
  if (!seeking) return;
  const t = seekTo(x);
  seeking = false; document.body.classList.remove("seeking");
  api("/api/seek", { seconds: t }).then(() => setTimeout(refresh, 200));
};
window.addEventListener("mouseup", (e) => endSeek(e.clientX));
window.addEventListener("touchend", (e) => endSeek(e.changedTouches[0].clientX));

/* 音量 */
for (const [id, key] of [["pv", "player"], ["sv", "system"]]) {
  const el = $(id);
  el.addEventListener("pointerdown", () => { dragging[id] = true; });
  el.addEventListener("input", () => { $(id + "o").value = el.value + "%"; });
  el.addEventListener("change", () => {
    dragging[id] = false;
    api("/api/volume", { [key]: Number(el.value) }).then(() => setTimeout(refresh, 200));
  });
}

/* 抽屉 */
const openSheet = (el) => { el.classList.add("open"); $("scrim").classList.add("open"); };
const closeSheets = () => {
  document.querySelectorAll(".sheet").forEach((s) => s.classList.remove("open"));
  $("scrim").classList.remove("open");
};
$("scrim").onclick = closeSheets;
$("closeSheet").onclick = closeSheets;
$("closeTimer").onclick = closeSheets;
$("openSheet").onclick = () => { view.level = "lists"; $("back").style.visibility = "hidden"; loadLists(); openSheet($("sheet")); };
$("openTimer").onclick = () => openSheet($("timerSheet"));

document.querySelectorAll("#timerGrid button").forEach((b) => {
  b.onclick = () => api("/api/timer", { minutes: Number(b.dataset.min) })
    .then(() => { closeSheets(); refresh(); });
});

$("tabs").querySelectorAll(".tab").forEach((t) => {
  t.onclick = () => {
    $("tabs").querySelectorAll(".tab").forEach((x) => x.classList.toggle("on", x === t));
    view.kind = t.dataset.kind;
    view.level = "lists";
    $("back").style.visibility = "hidden";
    loadLists();
  };
});
$("back").onclick = () => { view.level = "lists"; $("back").style.visibility = "hidden"; showTabs(true); loadLists(); };

function showTabs(on) { $("tabs").style.display = on ? "flex" : "none"; }

const esc = (s) => (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const rate = (p) => {
  if (!p) return "";
  return p >= 1e8 ? (p / 1e8).toFixed(1) + " 亿播放" : (p / 1e4).toFixed(0) + " 万播放";
};

async function loadLists() {
  showTabs(true);
  $("sheetTitle").textContent = "我的歌单";
  $("list").innerHTML = '<div class="empty"><span class="spin">◐</span> 加载中</div>';
  const r = await fetch("/api/my/playlists?kind=" + view.kind, { cache: "no-store" });
  const data = await r.json();
  const items = data.items || [];
  if (!items.length) {
    $("list").innerHTML = '<div class="empty">这里还空着<br>在网易云 App 里收藏歌单后回来刷新</div>';
    return;
  }
  $("list").innerHTML = items.map((p, i) => `
    <div class="row" data-i="${i}">
      <div class="info">
        <div class="nm">${esc(p.name)}</div>
        <div class="meta">${p.count} 首${p.play ? " · " + rate(p.play) : ""}</div>
      </div>
      <button class="go" data-play="${i}">整单播放</button>
    </div>`).join("");

  $("list").querySelectorAll(".row").forEach((row) => {
    const p = items[Number(row.dataset.i)];
    row.querySelector(".go").onclick = (e) => { e.stopPropagation(); playPlaylist(p); };
    row.onclick = () => loadTracks(p);
  });
}

async function loadTracks(p) {
  view.level = "tracks"; view.playlist = p;
  showTabs(false);
  $("back").style.visibility = "visible";
  $("sheetTitle").textContent = p.name;
  $("list").innerHTML = '<div class="empty"><span class="spin">◐</span> 读取曲目</div>';
  const r = await fetch("/api/playlist/tracks?enc=" + encodeURIComponent(p.enc), { cache: "no-store" });
  const data = await r.json();
  const tracks = data.items || [];
  if (!tracks.length) {
    $("list").innerHTML = '<div class="empty">这个歌单没有可播放的曲目</div>';
    return;
  }
  $("list").innerHTML = tracks.map((t, i) => `
    <div class="row" data-i="${i}">
      <div class="info">
        <div class="nm">${esc(t.name)}</div>
        <div class="meta">${esc(t.artist)} · ${fmt(t.ms / 1000)}</div>
      </div>
    </div>`).join("");
  $("list").querySelectorAll(".row").forEach((row) => {
    row.onclick = () => playTrack(tracks[Number(row.dataset.i)], Number(row.dataset.i));
  });
}

async function playPlaylist(p) {
  $("list").innerHTML = '<div class="empty"><span class="spin">◐</span> 正在加载歌单…</div>';
  const r = await api("/api/play/playlist", { enc: p.enc, orig: p.orig, name: p.name });
  if (!r.ok) { $("list").innerHTML = '<div class="empty">歌单加载失败，可能被限流<br>稍后重试</div>'; return; }
  closeSheets(); refresh();
}

async function playTrack(t, index) {
  closeSheets(); refresh();
  await api("/api/play/track", {
    songEnc: t.enc, songOrig: t.orig, index,
    playlistEnc: view.playlist.enc, playlistOrig: view.playlist.orig, playlistName: view.playlist.name,
  });
  setTimeout(refresh, 400);
}

document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT") return;
  if (e.code === "Escape") { closeSheets(); return; }
  if (e.code === "Space") { e.preventDefault(); $("toggle").click(); }
  else if (e.code === "ArrowRight") { e.preventDefault(); $("next").click(); }
  else if (e.code === "ArrowLeft") { e.preventDefault(); $("prev").click(); }
});

refresh();
setInterval(refresh, 1200);

// 深链接：/ #sheet 直接拉起歌单抽屉
if (location.hash === "#sheet") { $("openSheet").click(); }
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "ncm-webctl"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _token_ok(self):
        q = parse_qs(urlparse(self.path).query)
        if q.get("t", [""])[0] == TOKEN:
            return True
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and v == TOKEN:
                return True
        return False

    def _send(self, body, ctype, code=200, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code=200):
        self._send(json.dumps(payload, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8", code)

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)

        if path == "/" and q.get("t", [""])[0] == TOKEN:
            self.send_response(302)
            self.send_header(
                "Set-Cookie",
                f"{COOKIE}={TOKEN}; Path=/; Max-Age=31536000; SameSite=Lax; HttpOnly",
            )
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if not self._token_ok():
            if path == "/":
                body = ("<!doctype html><meta charset=utf-8><body style='font:15px system-ui;"
                        "background:#12161f;color:#edeff5;display:grid;place-items:center;"
                        "height:100vh;margin:0;text-align:center'>"
                        "<div><p>需要访问令牌</p><p style='color:#8a93a8;font-size:13px'>"
                        "用启动时打印的 ?t=… 链接打开</p></div>").encode()
                self._send(body, "text/html; charset=utf-8", 401)
            else:
                self._json({"error": "unauthorized"}, 401)
            return

        if path == "/":
            self._send(PAGE.encode(), "text/html; charset=utf-8")
            return

        if path == "/api/state":
            self._json(build_state())
            return

        if path == "/api/my/playlists":
            kind = q.get("kind", ["collected"])[0]
            if kind not in ("collected", "created"):
                kind = "collected"
            self._json({"items": my_playlists(kind)})
            return

        if path == "/api/playlist/tracks":
            enc = q.get("enc", [""])[0]
            self._json({"items": playlist_tracks(enc) if enc else []})
            return

        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._token_ok():
            self._json({"error": "unauthorized"}, 401)
            return

        path = urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}

        if path == "/api/toggle":
            st = build_state()
            if st["status"] == "stopped":
                ncm_run("resume")
                time.sleep(0.2)
                if not is_stopped():
                    RT["was_playing"] = True
                    RT["expired"] = False
                    apply_mode()
                else:
                    self._json({"ok": True, "hint": "no-queue"})
                    return
            else:
                mpv(("cycle", "pause"))
            self._json({"ok": True})
            return

        if path == "/api/next":
            self._json({"ok": ncm_run("next")})
            return

        if path == "/api/prev":
            self._json({"ok": ncm_run("prev")})
            return

        if path == "/api/seek":
            secs = float(body.get("seconds") or 0)
            mpv(("seek", secs, "absolute"))
            self._json({"ok": True, "seconds": secs})
            return

        if path == "/api/volume":
            done = {}
            if "player" in body:
                v = max(0, min(100, int(body["player"])))
                mpv(("set_property", "volume", v))
                done["player"] = v
            if "system" in body:
                v = max(0, min(100, int(body["system"])))
                subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{v}%"],
                               capture_output=True, timeout=5)
                _cache.pop("sysvol", None)
                done["system"] = v
            self._json({"ok": True, **done})
            return

        if path == "/api/mode":
            m = body.get("mode")
            if m not in ("sequential", "loop-list", "loop-one"):
                self._json({"ok": False, "error": "bad mode"}, 400)
                return
            RT["mode"] = m
            RT["expired"] = False
            apply_mode()
            self._json({"ok": True, "mode": m})
            return

        if path == "/api/timer":
            minutes = int(body.get("minutes") or 0)
            if minutes <= 0:
                RT["deadline"] = 0
            else:
                RT["deadline"] = time.time() + minutes * 60
            RT["expired"] = False
            self._json({"ok": True, "remaining": build_state()["timerRemaining"]})
            return

        if path == "/api/play/playlist":
            enc, orig = body.get("enc"), body.get("orig")
            name = body.get("name") or ""
            if not enc:
                self._json({"ok": False, "error": "missing enc"}, 400)
                return
            ok = start_playlist(enc, orig, name)
            self._json({"ok": bool(ok), "state": build_state()})
            return

        if path == "/api/play/track":
            song_enc, song_orig = body.get("songEnc"), body.get("songOrig")
            if not song_enc:
                self._json({"ok": False, "error": "missing song"}, 400)
                return
            playlist = None
            if body.get("playlistEnc"):
                playlist = (body["playlistEnc"], body.get("playlistOrig"), body.get("playlistName") or "")
            ok = start_track(song_enc, song_orig, playlist, body.get("index"))
            self._json({"ok": bool(ok)})
            return

        if path == "/api/resume-last":
            self._json({"ok": do_resume_last(), "state": build_state()})
            return

        self._json({"error": "not found"}, 404)


def do_resume_last():
    """三级降级：daemon 还在 → 原地续播；否则重放上一个歌单。"""
    ncm_run("resume")
    if wait_until(lambda: not is_stopped(), 3.0):
        RT["was_playing"] = True
        RT["expired"] = False
        apply_mode()
        return True
    last = RT["last"]
    if last:
        return start_playlist(last["enc"], last["orig"], last["name"])
    return False


def main():
    global PORT
    if len(sys.argv) > 1:
        PORT = int(sys.argv[1])

    load_last()
    threading.Thread(target=watchdog, daemon=True).start()

    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    ips = lan_addresses() or ["127.0.0.1"]
    print(f"ncm-webctl 已启动，端口 {PORT}")
    print(f"  本机:   http://127.0.0.1:{PORT}/?t={TOKEN}")
    for ip in ips:
        print(f"  手机:   http://{ip}:{PORT}/?t={TOKEN}")
    if RT["last"]:
        print(f"  上次歌单: {RT['last'].get('name')}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
