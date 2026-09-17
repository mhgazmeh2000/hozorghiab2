# -*- coding: utf-8 -*-
"""
استعلام آنلاین و مدیریت دستگاه‌های حضور و غیاب (ZKTeco / Timmy)
================================================================
Single-file Flask app.

مدل‌های هدف : UF100, MB20, F70, WL50, Timmy AI09F  (همه با پروتکل ZK)
ارتباط      : TCP port 4370 (ZK protocol)  →  کتابخانه pyzk
نحوه اجرا   :  python app.py        →   http://<server-ip>:5000

وابستگی‌ها  :  pip install flask pyzk openpyxl
"""

import atexit
import csv
import io
import json
import logging
import os
import queue
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import calendar
from flask import Flask, Response, jsonify, request, send_from_directory
from struct import pack, unpack, unpack_from

try:
    from zk import ZK, const
except ImportError:
    raise SystemExit("pyzk نصب نیست:  pip install pyzk")

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DEVICES_FILE = DATA_DIR / "devices.json"

HOST = "0.0.0.0"
PORT_WEB = 5000

DEFAULT_SUBNETS = [
    "172.16.0.0/24", "172.16.8.0/24", "172.16.10.0/24", "172.16.15.0/24",
    "172.16.25.0/24", "172.16.27.0/24", "172.16.31.0/24", "172.16.32.0/24",
    "172.16.50.0/24", "172.16.61.0/24",
]

SEED_DEVICES = [
    {"ip": "172.16.50.30", "port": 4370, "password": 0, "use_udp": False,
     "label": "", "location": "", "enabled": True},
    {"ip": "172.16.8.20", "port": 5005, "password": 0, "use_udp": False,
     "label": "AI09F", "location": "", "enabled": True},
    {"ip": "172.16.0.20", "port": 4370, "password": 0, "use_udp": False,
     "label": "", "location": "", "enabled": True},
    {"ip": "172.16.32.21", "port": 4370, "password": 0, "use_udp": False,
     "label": "", "location": "", "enabled": True},
]

PUNCH_LABELS = {
    0: "ورود", 1: "خروج", 2: "شروع استراحت",
    3: "پایان استراحت", 4: "ورود اضافه‌کاری", 5: "خروج اضافه‌کاری",
}
PUNCH_UNKNOWN = "نامشخص"   # بعضی فریم‌ورها 255 ذخیره می‌کنند (بدون تفکیک ورود/خروج)

# ----------------------------------------------------------------------------
# SQLite archive — one local store fed both by SDK pulls and ADMS push
# ----------------------------------------------------------------------------
DB_FILE = DATA_DIR / "attendance.db"
_sqlite_lock = threading.Lock()


def _db():
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def db_init():
    DB_DIR_EXIST = DATA_DIR.exists()
    if not DB_DIR_EXIST:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS attendance(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          device TEXT NOT NULL, user_id TEXT NOT NULL,
          ts TEXT NOT NULL, punch INTEGER, status INTEGER,
          source TEXT NOT NULL DEFAULT 'pull',
          UNIQUE(device,user_id,ts,punch));
        CREATE INDEX IF NOT EXISTS i_att_ts   ON attendance(ts);
        CREATE INDEX IF NOT EXISTS i_att_user ON attendance(user_id);
        CREATE INDEX IF NOT EXISTS i_att_dev  ON attendance(device);
        CREATE TABLE IF NOT EXISTS employees(
          device TEXT NOT NULL, user_id TEXT NOT NULL,
          name TEXT, privilege INTEGER, card TEXT, updated TEXT,
          UNIQUE(device,user_id));
        CREATE TABLE IF NOT EXISTS sync_state(
          device TEXT PRIMARY KEY, last_sync TEXT,
          last_count INTEGER, last_error TEXT);
        """)


def db_save_attendance(device_ip, recs, source="pull"):
    if not recs:
        return 0
    with _sqlite_lock, _db() as con:
        cur = con.executemany(
            "INSERT OR IGNORE INTO attendance(device,user_id,ts,punch,status,source)"
            " VALUES(?,?,?,?,?,?)",
            [(r["device"], r["user_id"], r["timestamp"], r.get("punch"),
              r.get("status"), source) for r in recs])
        if cur.rowcount:
            cache_invalidate("archive")
        return cur.rowcount


def db_save_users(device_ip, users):
    if not users:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    with _sqlite_lock, _db() as con:
        cur = con.executemany(
            "INSERT INTO employees(device,user_id,name,privilege,card,updated)"
            " VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(device,user_id) DO UPDATE SET name=excluded.name,"
            " privilege=excluded.privilege, card=excluded.card,"
            " updated=excluded.updated",
            [(device_ip, str(u.get("user_id") or u.get("uid") or ""),
              u.get("name", ""), int(u.get("privilege") or 0),
              str(u.get("card") or "0"), now) for u in users])
        if cur.rowcount:
            cache_invalidate("archive")
        return cur.rowcount


def db_set_sync_state(device_ip, count, error):
    with _sqlite_lock, _db() as con:
        con.execute(
            "INSERT INTO sync_state(device,last_sync,last_count,last_error)"
            " VALUES(?,?,?,?) ON CONFLICT(device) DO UPDATE SET"
            " last_sync=excluded.last_sync, last_count=excluded.last_count,"
            " last_error=excluded.last_error",
            (device_ip, datetime.now().isoformat(timespec="seconds"),
             count, error))

# ----------------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------------
_dev_lock = threading.RLock()
_locks: dict = {}           # per-device locks (ZK = one session at a time)
_users_cache: dict = {}     # ip -> {user_id: name}


def _load() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if DEVICES_FILE.exists():
        try:
            return json.loads(DEVICES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "devices": [dict(d, info={}, last_state="unknown", last_check=None,
                         last_log_ts=None) for d in SEED_DEVICES],
        "subnets": DEFAULT_SUBNETS,
    }


def _save(db: dict):
    DEVICES_FILE.write_text(
        json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")


DB = _load()


def _dev_lock_for(ip: str) -> threading.Lock:
    with _dev_lock:
        if ip not in _locks:
            _locks[ip] = threading.Lock()
        return _locks[ip]


def _find_dev(ip: str):
    for d in DB["devices"]:
        if d["ip"] == ip:
            return d
    return None


# ----------------------------------------------------------------------------
# ZK helpers
# ----------------------------------------------------------------------------
def model_guess(name: str = "", platform: str = "") -> str:
    n = f"{name} {platform}".lower()
    checks = [
        ("mb20", "MB20"), ("uf100", "UF100"), ("f70", "F70"),
        ("wl50", "WL50"), ("wl-", "WL50"),
        ("timmy", "Timmy AI09F"), ("ai09", "Timmy AI09F"),
        ("zmm220", "ZMM220 (UF100-class)"), ("ak3750", "AK3750 WiFi"),
        ("zlm60", "ZLM60 (MB20-class)"),
    ]
    for key, label in checks:
        if key in n:
            return label
    return "نامشخص"


def tcp_check(ip: str, port: int = 4370, timeout: float = 2.0):
    """Fast reachability check + latency in ms. Returns (ok, ms|None)."""
    t0 = time.perf_counter()
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True, round((time.perf_counter() - t0) * 1000)
    except OSError:
        return False, None


def icmp_check(ip: str, timeout: float = 2.0):
    """Check host reachability when the device service port is filtered."""
    wait_ms = max(1000, int(timeout * 1000))
    try:
        result = subprocess.run(
            ["ping", "-n", "1", "-w", str(wait_ms), ip],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=timeout + 1)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _zk(dev: dict) -> ZK:
    return ZK(dev["ip"], port=int(dev.get("port", 4370)),
              timeout=int(dev.get("timeout", 20)),
              password=int(dev.get("password", 0)),
              force_udp=bool(dev.get("use_udp", False)),
              ommit_ping=True)          # ICMP often blocked in tunnels


def zk_identify(dev: dict) -> dict:
    """Open a session, read device identity, persist it. Raises on failure."""
    with _dev_lock_for(dev["ip"]):
        conn = _zk(dev).connect()
        try:
            info = {}
            for attr, key in (("get_device_name", "device_name"),
                              ("get_serialnumber", "serial"),
                              ("get_platform", "platform"),
                              ("get_firmware_version", "firmware"),
                              ("get_mac", "mac")):
                try:
                    info[key] = str(getattr(conn, attr)()).strip()
                except Exception:
                    info[key] = ""
            # A hand-entered serial (from the device label) wins when the
            # device won't tell us, and is never silently lost.
            if not info.get("serial"):
                info["serial"] = ((dev.get("info") or {}).get("serial") or "")
            info["model"] = model_guess(info.get("device_name", ""),
                                        info.get("platform", ""))
            # firmware strings on the WiFi units don't reveal the marketing
            # model (WL50/Timmy) — trust the user's label when set
            if (dev.get("label") or "").strip():
                info["model"] = dev["label"].strip()
            dev["info"] = info
            dev["last_state"] = "online"
            dev["last_check"] = datetime.now().isoformat(timespec="seconds")
            _save(DB)
            return info
        finally:
            try:
                conn.disconnect()
            except Exception:
                pass


# ----------------------------------------------------------------------------
# "Green label" WiFi units (AK3750WIFI_TFT platform — WL50 / Timmy AI09F)
# These firmwares return an empty CMD_GET_FREE_SIZES packet, so pyzk's
# get_attendance()/get_users() short-circuit to [] — the data IS there and
# must be pulled with the raw buffered flow (CMD 1503/1504).
# ----------------------------------------------------------------------------
GL_ATT_CACHE = {}          # ip -> (monotonic_ts, attlog_blob)  TTL below
GL_ATT_TTL = 300           # seconds — a full pull takes minutes on 4KB frames


def _is_green_label(conn) -> bool:
    try:
        if "AK3750" in (conn.get_platform() or "").upper():
            return True
    except Exception:
        pass
    try:
        if (conn.get_serialnumber() or "").strip().upper().startswith("AW"):
            return True
    except Exception:
        pass
    return False


def _gl_recv_until_quiet(sock, first_timeout=8.0, quiet=2.0, cap=2_000_000):
    sock.settimeout(first_timeout)
    buf = b""
    try:
        while len(buf) < cap:
            ch = sock.recv(65536)
            if not ch:
                break
            buf += ch
            sock.settimeout(quiet)
    except socket.timeout:
        pass
    return buf


def _gl_walk_frames(buf):
    """Yield (cmd, payload) for every complete TCP frame in buf."""
    out, pos = [], 0
    while len(buf) - pos >= 16:
        m1, m2, flen = unpack("<HHI", buf[pos:pos + 8])
        if m1 != 0x5050 or m2 != 0x7473 or flen < 8:
            break
        if len(buf) - pos < 8 + flen:
            break
        inner = buf[pos + 8: pos + 8 + flen]
        out.append((unpack("<H", inner[:2])[0], inner[8:]))
        pos += 8 + flen
    return out, buf[pos:]


def _gl_make_ack(c):
    """Protocol-valid CMD_ACK_OK TCP frame (mirrors pyzk internals)."""
    head = pack("<4H", const.CMD_ACK_OK, 0, c._ZK__session_id,
                const.USHRT_MAX - 1)
    chk, n, p = 0, len(head), head
    while n > 1:
        chk += unpack("H", pack("BB", p[0], p[1]))[0]
        p = p[2:]
        if chk > const.USHRT_MAX:
            chk -= const.USHRT_MAX
        n -= 2
    while chk > const.USHRT_MAX:
        chk -= const.USHRT_MAX
    chk = ~chk
    while chk < 0:
        chk += const.USHRT_MAX
    return pack("<HHI", 0x5050, 0x7473, 8) + pack(
        "<4H", const.CMD_ACK_OK, chk, c._ZK__session_id,
        const.USHRT_MAX - 1)


def _gl_collect(c, want_bytes):
    """Pull the stream with 1504 rounds. Each answer is a run of 4096-byte
    CMD_DATA frames coalescing on the socket; pyzk's __send_command only
    consumes the head of the first frame, so complete it inline, ACK it,
    then drain the rest."""
    sock = c._ZK__sock
    stream = b""
    idle = 0
    while len(stream) < want_bytes and idle < 12:
        off = len(stream)
        want = min(0xFFc0, want_bytes - off)
        try:
            r = c._ZK__send_command(1504, pack("<ii", off, want), 1024)
        except Exception:
            idle += 1
            time.sleep(2)
            continue
        if not r.get("status"):
            idle += 1
            time.sleep(1.5)
            continue
        got = 0
        if r.get("code") == const.CMD_DATA:
            have = len(c._ZK__data_recv)
            target = c._ZK__tcp_length
            if have < target:
                c._ZK__data_recv += c._ZK__recieve_raw_data(target - have)
            stream += c._ZK__data_recv[8:target][:want_bytes - len(stream)]
            got += len(c._ZK__data_recv[8:target])
            try:
                sock.send(_gl_make_ack(c))
            except Exception:
                pass
        buf = _gl_recv_until_quiet(sock, first_timeout=6.0, quiet=2.5)
        frames, _rest = _gl_walk_frames(buf)
        for cmd, payload in frames:
            if cmd == const.CMD_DATA and payload:
                if len(stream) < want_bytes:
                    stream += payload[:want_bytes - len(stream)]
                got += len(payload)
                try:
                    sock.send(_gl_make_ack(c))
                except Exception:
                    pass
        if got:
            idle = 0
        else:
            idle += 1
    return stream


def _gl_pull(c, cmd, fct=0):
    """One full buffered read. ALWAYS frees the session (a dangling 1503
    blocks all further reads until the device reboots)."""
    c.free_data()
    time.sleep(1.0)
    c.read_sizes()
    time.sleep(1.0)
    resp = c._ZK__send_command(1503, pack("<bhii", 1, cmd, fct, 0), 1024)
    if not resp.get("status"):
        try:
            c._ZK__send_command(const.CMD_FREE_DATA, b"", 1024)
        except Exception:
            pass
        raise RuntimeError(f"1503 refused (code {resp.get('code')}) — "
                           "دستگاه احتمالاً نیاز به ری‌استارت دارد")
    total = None
    payload_ack = c._ZK__data_recv[8:c._ZK__tcp_length]
    if resp.get("code") in (const.CMD_PREPARE_DATA, const.CMD_ACK_OK) \
            and len(payload_ack) >= 5:
        total = unpack("<I", payload_ack[1:5])[0]
    stream = b""
    buf = _gl_recv_until_quiet(c._ZK__sock, first_timeout=1.2, quiet=0.8)
    frames, _rest = _gl_walk_frames(buf)
    for fcmd, payload in frames:
        if fcmd == const.CMD_DATA and payload:
            stream += payload
            if total is None and len(payload) >= 4:
                total = unpack("<I", payload[:4])[0]
    if not total:
        try:
            c._ZK__send_command(const.CMD_FREE_DATA, b"", 1024)
        except Exception:
            pass
        raise RuntimeError("حجم داده از دستگاه خوانده نشد")
    # WiFi units sometimes stall mid-stream — retry the whole pull on a
    # fresh 1503 session until complete (max 3 rounds).
    for round_no in range(3):
        if round_no > 0:
            stream = b""          # random-access 1504: restart from offset 0
        stream += _gl_collect(c, total + 4 - len(stream))
        if len(stream) >= total + 4:
            break
        print(f"[green-label] short read "
              f"{len(stream)}/{total + 4}, retrying (round {round_no + 2})")
        time.sleep(2)
    try:
        c._ZK__send_command(const.CMD_FREE_DATA, b"", 1024)
    except Exception:
        pass
    return stream[:total + 4]


def _gl_decode_time(t):
    """ZK packed time; old green-label clocks overflow day/month fields
    (e.g. 'Sep 31') — clamp to the last valid day of the month."""
    s = t % 60
    t //= 60
    mi = t % 60
    t //= 60
    h = t % 24
    t //= 24
    d = t % 32
    t //= 32
    mo = t % 13
    y = 2000 + t // 13
    mo = min(max(mo + 1, 1), 12)
    d = min(max(d + 1, 1), calendar.monthrange(y, mo)[1])
    return datetime(y, mo, d, min(h, 23), min(mi, 59), min(s, 59))


def _gl_parse_attlog(blob):
    """22-byte records: [uid:u16][user_id str:4][magic:7][ts u32@13][rsv:5]."""
    recs = []
    body = blob[4:]
    for off in range(0, len(body) - 21, 22):
        rec = body[off:off + 22]
        uid = unpack_from("<H", rec, 0)[0]
        user_id = rec[2:6].split(b"\0")[0].decode("utf-8", "ignore")
        # Confused-state garbage (e.g. after a clock change) has non-digit
        # user ids and packed-time junk; real ids on these units are numeric.
        if not user_id.isdigit():
            continue
        try:
            ts = _gl_decode_time(unpack_from("<I", rec, 13)[0])
        except Exception:
            continue
        if not (2015 <= ts.year <= 2040):
            continue
        recs.append((uid, user_id, ts))
    return recs


def _gl_parse_users(blob):
    """72-byte user records (same layout pyzk expects for packet size 72)."""
    users = []
    body = blob[4:]
    for off in range(0, len(body) - 71, 72):
        uid, priv, _pwd, name, card, _grp, uid_s = unpack(
            "<HB8s24sIx7sx24s", body[off:off + 72])
        name = name.split(b"\0")[0].decode("utf-8", "ignore").strip()
        uid_s = uid_s.split(b"\0")[0].decode("utf-8", "ignore")
        users.append({"uid": uid, "user_id": uid_s or str(uid),
                      "name": name or f"NN-{uid_s or uid}",
                      "privilege": int(priv), "card": str(card)})
    return users


def _gl_cache_path(ip: str, kind: str):
    return DATA_DIR / f"gl_{kind}_{ip.replace('.', '_')}.bin"


def _gl_pull_with_fallback(conn, ip: str, kind: str, cmd, fct=0):
    """Live pull with a disk-backed fallback: every success is persisted to
    data/gl_<kind>_<ip>.bin and served again when the device is in one of
    its post-session cooldowns (a live pull can then return short/empty —
    the WiFi units refuse 1503 for minutes after an aborted session)."""
    path = _gl_cache_path(ip, kind)
    blob = None
    try:
        blob = _gl_pull(conn, cmd, fct)
        if len(blob) < 100:
            blob = None
    except Exception:
        blob = None
    if blob is not None:
        try:
            path.write_bytes(blob)
        except Exception:
            pass
        return blob
    if path.exists():
        try:
            return path.read_bytes()
        except Exception:
            pass
    raise RuntimeError("زنده خوانده نشد و نسخه ذخیره‌شده‌ای هم نیست")


def _gl_parse_templates(blob):
    """Template table from 1503/FCT_FINGERTMP: records of
    (size:u16, uid:u16, fid:i8, valid:i8, template bytes) after a 4-byte
    total. Empty slots are zero-filled — the walker stops at the first
    impossible record."""
    out = []
    body = blob[4:]
    pos = 0
    while pos + 6 <= len(body):
        rsz, uid, fid, valid = unpack_from("HHbb", body, pos)
        if 6 <= rsz <= 2200 and pos + rsz <= len(body):
            if rsz > 6:
                out.append({"uid": uid, "fid": fid, "valid": valid,
                            "template": body[pos + 6:pos + rsz]})
            pos += rsz
            continue
        break
    return out


def _gl_write_template(conn, uid, user_id, name="", privilege=0,
                       fid=0, template=b""):
    """Write one fingerprint template to a green-label device.

    Packet layout mirrors pyzk's save_user_template() (verified live on
    AK3750WIFI_TFT: CMD 110 answers OK and the re-read template is
    byte-identical): head <III>(user73, table, fpack) + user73 + table +
    fpack, then command 110 with <IHH>(12,0,8) and refresh_data()."""
    name_b = (name or "").encode("utf-8", "ignore")[:24]
    user73 = pack("<BHB8s24sIB7sx24s", 2, int(uid), int(privilege or 0),
                  b"", name_b, 0, 1, b"", str(user_id).encode())
    table = pack("<bHbI", 2, int(uid), 0x10 + int(fid), 0)
    fpack = pack("<H%is" % len(template), len(template), template)
    packet = pack("III", len(user73), len(table), len(fpack)) \
        + user73 + table + fpack
    conn._send_with_buffer(packet)
    resp = conn._ZK__send_command(110, pack("<IHH", 12, 0, 8))
    if not resp.get("status"):
        raise RuntimeError(
            f"دستگاه نوشتن قالب اثر انگشت را نپذیرفت (code {resp.get('code')})")
    try:
        conn.refresh_data()
    except Exception:
        pass
    return True


def zk_fetch_logs_greenlabel(dev: dict, conn, include_users: bool = True,
                             fresh: bool = False):
    """Green-label variant of zk_fetch_logs. conn is already connected.
    Returns (records, users_count, error|None) — never raises."""
    ip = dev["ip"]
    try:
        _fp(ip, "disable", "قفل کردن صفحه دستگاه")
        conn.disable_device()
        try:
            cached = None if fresh else GL_ATT_CACHE.get(ip)
            if cached and (time.time() - cached[0]) < GL_ATT_TTL:
                att = cached[1]
                _fp(ip, "attlog", "خواندن از کش (۵ دقیقه‌ای)")
            else:
                _fp(ip, "attlog", "دریافت کامل تردد — چند دقیقه (فریم ۴KB)")
                t0 = time.time()
                att = _gl_pull_with_fallback(
                    conn, ip, "attlog", const.CMD_ATTLOG_RRQ)
                GL_ATT_CACHE[ip] = (time.time(), att)
                _fp(ip, "attlog",
                    f"{len(att)} بایت در {time.time()-t0:.0f} ثانیه", ok=True)
            if include_users:
                try:
                    _fp(ip, "users", "دریافت جدول کاربران (~۱۱ ثانیه)")
                    ublob = _gl_pull_with_fallback(
                        conn, ip, "users",
                        const.CMD_USERTEMP_RRQ, const.FCT_USER)
                    _users_cache[ip] = {
                        u["user_id"]: u["name"] for u in _gl_parse_users(ublob)}
                    _fp(ip, "users", f"{len(_users_cache[ip])} کاربر", ok=True)
                except Exception as ue:
                    _fp(ip, "users", f"خطا: {ue}", ok=False)
            names = _users_cache.get(ip, {})
            recs = [{
                "device": dev["ip"],
                "label": dev.get("label") or dev["info"].get("model", ""),
                "user_id": user_id or str(uid),
                "name": names.get(user_id or str(uid), ""),
                "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
                "punch": 255,
                "punch_label": PUNCH_UNKNOWN,
                "status": 0,
            } for uid, user_id, ts in _gl_parse_attlog(att)]
            recs.sort(key=lambda r: r["timestamp"], reverse=True)
            dev["last_state"] = "online"
            dev["last_check"] = datetime.now().isoformat(timespec="seconds")
            if recs:
                dev["last_log_ts"] = recs[0]["timestamp"]
            _save(DB)
            _fp(ip, "parse", f"تفسیر {len(recs)} رکورد", ok=True)
            _fp(ip, "done", f"{len(recs)} رکورد از {ip}", ok=True)
            return recs, len(names), None
        finally:
            try:
                conn.enable_device()
            except Exception:
                pass
    except Exception as e:
        dev["last_state"] = "offline"
        dev["last_check"] = datetime.now().isoformat(timespec="seconds")
        _save(DB)
        # capture the failing stage BEFORE _fp overwrites it (st is a live ref)
        failed_stage = (FETCH_PROGRESS.get(ip) or {}).get("stage", "?")
        _fp(ip, "error", f"{type(e).__name__}: {e}", ok=False)
        return [], 0, f"{failed_stage} → GreenLabel {type(e).__name__}: {e}"


# ----------------------------------------------------------------------------
# Per-device fetch progress — each stage of zk_fetch_logs is reported here so
# the UI can show exactly where a slow or failing pull is stuck.
# ----------------------------------------------------------------------------
FETCH_PROGRESS: dict = {}          # ip -> {stage, note, started, updated, ok}
ENROLLMENTS = {}                   # ip -> active enrollment job
ENROLLMENT_LOCK = threading.Lock()


def _fp(ip: str, stage: str, note: str = "", ok=None):
    now = datetime.now().isoformat(timespec="seconds")
    rec = FETCH_PROGRESS.setdefault(ip, {})
    rec.update(stage=stage, note=note, ok=ok, updated=now)
    rec.setdefault("started", now)
    if stage in ("done", "error"):
        rec["finished"] = now
    _connection_log(ip, stage, note, ok=ok, source="fetch")


def _enrollment_status(ip):
    with ENROLLMENT_LOCK:
        job = ENROLLMENTS.get(ip)
        return dict(job) if job else None


# ----------------------------------------------------------------------------
# FK / "B-series" protocol (Faratechno AI09F-class face units, TCP 5005)
#
# The faratecno suite (LatifiWorkingTimeUIWinform + FKAttend.dll / FKViaDev.dll,
# unpacked from faratecno/setup_NewDevice(561).exe) speaks a simple framed
# protocol on the device's TCP 5005 listener — NOT the ZK 4370 protocol.
# Wire format (documented by Nicola Belluti's "Attendance Reader" reverse-
# engineering series and matching FKAttend's FK_ConnectNet device family):
#
#   request : 55 aa <12-byte payload> <u16le seq>          (16 bytes total)
#   response: aa55 <8-byte header> [55 aa <payload>]
#
#   ping            payload 01 80 00*10  -> header-only reply 01 01 00*6
#   record count    payload 01 b4 08 00 00 00 00 00 ff ff 00 00
#                                        -> count in header bytes 4..5 (u16le)
#   start dump      payload 01 a4 00 00 00 00 <count u16le> 00 00 00 04
#   next block      payload 01 a4 00 00 00 00 00 00 <block u16le> 00 04
#                                        -> 12-byte records until FF padding
#   employee name   payload 01 c7 <uid u32le> 00 00 00 00 14 00
#                                        -> payload[0:10] = 10-char name
#   record (12B)    [? ? st ? uid u32le] [packed-time u32le]
#                   st top bits 00/01/10/11 = in1/out1/in2/out2
#                   packed time, big-endian view: yyyymmmmdddhhhhhmmmmmm
#
# dump requests carry two u16le params at offsets 6-8 / 8-10 and the flag
# 00 04 at 10-12; responses have NO length prefix — payload (if any) is
# read until a short quiet period. Blocks end with FF padding.
# ----------------------------------------------------------------------------
FK_REQ, FK_RESP = b"\x55\xaa", b"\xaa\x55"
FK_PING = bytes.fromhex("018000000000000000000000")
FK_COUNT = bytes.fromhex("01b4080000000000ffff0000")
FK_DUMP_START = bytes.fromhex("01a400000000")       # + <count><0><0004>
FK_DUMP_BLOCK = bytes.fromhex("01a4000000000000")   # + <block><0004>


def _fk_packed_time_to_dt(t: int):
    """FK packed date (read big-endian): 12b year, 4b month, 5b day, 5b hour,
    6b minute. Seconds are lost in this field; records land on :00."""
    try:
        year = (t >> 20) & 0xFFF
        month = (t >> 16) & 0xF
        day = (t >> 11) & 0x1F
        hour = (t >> 6) & 0x1F
        minute = t & 0x3F
        if not (2000 <= year <= 2100 and 1 <= month <= 12
                and 1 <= day <= 31 and hour <= 23 and minute <= 59):
            return None
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


def _fk_decode_record(rec: bytes):
    """12-byte record -> (user_id, datetime, status) or None."""
    if len(rec) != 12 or rec == b"\x00" * 12:
        return None
    ts_be = unpack_from(">I", rec, 8)[0]
    dt = _fk_packed_time_to_dt(ts_be)
    if dt is None:
        return None
    uid = str(unpack_from("<I", rec, 4)[0])
    status = (rec[1] >> 6) & 0x03      # 0=in1 1=out1 2=in2 3=out2
    return uid, dt, status


class FKClient:
    """Minimal FK-5005 client: count, attendance dump, employee names."""

    def __init__(self, ip: str, port: int = 5005, timeout: float = 10.0):
        self.ip, self.port, self.timeout = ip, port, timeout
        self.sock = None
        self.seq = 1

    # -- low level ----------------------------------------------------------
    def connect(self):
        self.sock = socket.create_connection((self.ip, self.port),
                                             timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        return self

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            ch = self.sock.recv(n - len(buf))
            if not ch:
                raise ConnectionError("device closed connection")
            buf += ch
        return buf

    def _recv_response(self) -> tuple:
        """aa55 <8B header> [55aa <payload — until quiet>] -> (header, payload)"""
        head = self._recv_exact(2)
        if head != FK_RESP:
            raise ConnectionError(f"bad response magic {head.hex()}")
        header = self._recv_exact(8)
        payload = b""
        if self._peek_pending():
            marker = self._recv_exact(2)
            if marker != FK_REQ:
                raise ConnectionError(
                    f"bad payload marker {marker.hex()}")
            payload = self._recv_until_quiet()
        return header, payload

    def _peek_pending(self) -> bool:
        try:
            self.sock.settimeout(2.5)
            ch = self.sock.recv(2, socket.MSG_PEEK)
            return ch == FK_REQ
        except (socket.timeout, TimeoutError):
            return False
        except OSError:
            return False

    def _recv_until_quiet(self, quiet: float = 1.2,
                          cap: int = 262144) -> bytes:
        buf = b""
        self.sock.settimeout(quiet)
        try:
            while len(buf) < cap:
                ch = self.sock.recv(65536)
                if not ch:
                    break
                buf += ch
        except (socket.timeout, TimeoutError):
            pass
        return buf

    def command(self, payload12: bytes) -> tuple:
        """Send one request, return (header8, payload)."""
        if len(payload12) != 12:
            raise ValueError("FK payload must be 12 bytes")
        pkt = FK_REQ + payload12 + pack("<H", self.seq & 0xFFFF)
        self.seq += 1
        self.sock.sendall(pkt)
        return self._recv_response()

    # -- high level ---------------------------------------------------------
    def ping(self) -> bool:
        header, _ = self.command(FK_PING)
        return header[:2] == b"\x01\x01"

    def get_count(self) -> int:
        header, _ = self.command(FK_COUNT)
        return unpack("<H", header[4:6])[0]

    def get_attendance(self, include_names: bool = True):
        """Dump all records: (records, names) — names is {uid: name}."""
        total = self.get_count()
        records, names = [], {}
        payload = FK_DUMP_START + pack("<HHH", total, 0, 0x0400)
        header, blob = self.command(payload)
        if header[:2] != b"\x01\x01":
            raise ConnectionError("dump refused (bad header)")
        block = 1
        seen_terminator = False
        while not seen_terminator:
            payload = FK_DUMP_BLOCK + pack("<HH", block, 0x0400)
            try:
                header, blob = self.command(payload)
            except (ConnectionError, OSError):
                break
            if header[:2] != b"\x01\x01" or not blob:
                break
            for i in range(0, len(blob) - 11, 12):
                rec = blob[i:i + 12]
                if rec[:2] == b"\xff\xff":
                    seen_terminator = True
                    break
                dec = _fk_decode_record(rec)
                if dec:
                    records.append(dec)
            block += 1
            if block > 65535:
                break
        # employee names — 10 chars each, one command per employee
        if include_names:
            for uid in {r[0] for r in records}:
                try:
                    ui = int(uid)
                    if not (0 < ui < 2**32):
                        continue
                    payload = bytes([0x01, 0xC7]) + pack("<I", ui) + \
                        bytes.fromhex("00000000") + pack("<H", 0x0014)
                    header, blob = self.command(payload)
                    if header[:2] == b"\x01\x01" and len(blob) >= 10:
                        name = blob[:10].rstrip(b"\x00").decode(
                            "utf-8", "ignore").strip()
                        if name:
                            names[uid] = name
                except Exception:
                    continue
        return records, names


def _acquire_device_lock(ip: str, timeout: float):
    """Try to take the per-device lock for up to `timeout` seconds.
    Returns the lock or None — callers fail fast with a clear message
    instead of queueing for a fixed 30 s while a full pull runs."""
    lock = _dev_lock_for(ip)
    deadline = time.monotonic() + timeout
    while True:
        if lock.acquire(blocking=False):
            return lock
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.25)


def fk_fetch_logs(dev: dict, include_users: bool = True):
    """Fetch attendance via the FK-5005 protocol. Same contract as
    zk_fetch_logs: (records, users_count, error|None), never raises."""
    ip = dev["ip"]
    port = int(dev.get("port", 5005) or 5005)
    _fp(ip, "lock-wait", "در انتظار آزاد شدن دستگاه…")
    # FK exchanges are short — a short real wait beats a fixed 30 s stall.
    lock = _acquire_device_lock(
        ip, float(dev.get("lock_timeout", cfg("lock_timeout.fk", 8))))
    if lock is None:
        _fp(ip, "error", "دستگاه مشغول است (قفل آزاد نشد)", ok=False)
        return [], 0, "device busy (lock timeout)"
    try:
        _fp(ip, "connect", f"اتصال FK5005 به {ip}:{port}")
        with FKClient(ip, port, timeout=int(dev.get("timeout", 10))) as fk:
            _fp(ip, "identify", "دستگاه FK/B-series (پروتکل 5005)")
            if not fk.ping():
                raise ConnectionError("FK ping بدون پاسخ")
            _fp(ip, "attlog", "دریافت رکوردهای تردد (FK)")
            raw_records, names = fk.get_attendance(include_names=include_users)
            if include_users and names:
                _users_cache[ip] = {u: n for u, n in names.items() if n}
            recs = [{
                "device": ip,
                "label": dev.get("label") or "Faratechno AI09F",
                "user_id": uid,
                "name": names.get(uid, _users_cache.get(ip, {}).get(uid, "")),
                "timestamp": dt.strftime("%Y-%m-%d %H:%M:%S"),
                "punch": status,
                "punch_label": PUNCH_LABELS.get(status, PUNCH_UNKNOWN),
                "status": 0,
            } for uid, dt, status in raw_records]
            recs.sort(key=lambda r: r["timestamp"], reverse=True)
            dev["last_state"] = "online"
            dev["last_check"] = datetime.now().isoformat(timespec="seconds")
            if recs:
                dev["last_log_ts"] = recs[0]["timestamp"]
            info = dev.get("info") or {}
            info.setdefault("model", "Faratechno AI09F (FK/B-series)")
            info["protocol"] = "fk5005"
            dev["info"] = info
            _save(DB)
            _fp(ip, "done", f"{len(recs)} رکورد", ok=True)
            return recs, len(names), None
    except Exception as e:
        dev["last_state"] = "offline"
        dev["last_check"] = datetime.now().isoformat(timespec="seconds")
        _save(DB)
        failed_stage = (FETCH_PROGRESS.get(ip) or {}).get("stage", "?")
        _fp(ip, "error", f"{type(e).__name__}: {e}", ok=False)
        return [], 0, f"{failed_stage} → FK5005 {type(e).__name__}: {e}"
    finally:
        lock.release()


def is_fk_device(dev: dict) -> bool:
    """Devices registered with port 5005 use the FK/B-series protocol."""
    return int(dev.get("port", 4370) or 0) == 5005


def fetch_logs_for(dev: dict, include_users: bool = True, fresh: bool = False):
    """Protocol dispatcher: port 5005 -> FK/B-series, else ZK/4370."""
    if is_fk_device(dev):
        return fk_fetch_logs(dev, include_users)
    return zk_fetch_logs(dev, include_users, fresh)


def zk_fetch_logs(dev: dict, include_users: bool = True, fresh: bool = False):
    """Fetch attendance records (optionally user names too).
    Never raises: returns (records, users_count, error|None).
    Progress is reported to FETCH_PROGRESS per stage."""
    ip = dev["ip"]
    lock = _dev_lock_for(ip)
    _fp(ip, "lock-wait", "در انتظار آزاد شدن دستگاه (همگام‌سازی دیگر…)")
    # Green-label full pulls legitimately hold the lock for minutes, so the
    # wait scales with the device: honor per-device `lock_timeout`, else
    # default 45 s (long enough to slot in behind a short poll, short enough
    # to fail fast when a multi-minute pull is running). Callers that really
    # want to queue can set lock_timeout higher in the device registry.
    lock_wait = float(
        dev.get("lock_timeout", cfg("lock_timeout.default", 45)))
    lock = _acquire_device_lock(ip, lock_wait)
    if lock is None:
        _fp(ip, "error", "دستگاه مشغول همگام‌سازی طولانی است (قفل آزاد نشد)",
            ok=False)
        return [], 0, "device busy (lock timeout after %ds)" % int(lock_wait)
    conn = None
    try:
        _fp(ip, "connect", f"اتصال به {ip}:{dev.get('port', 4370)}")
        conn = _zk(dev).connect()
        _fp(ip, "identify", "تشخیص نوع فریم‌ور")
        if _is_green_label(conn):
            return zk_fetch_logs_greenlabel(dev, conn, include_users, fresh)
        _fp(ip, "users", "دریافت لیست کاربران")
        if include_users:
            try:
                users = conn.get_users()
                _users_cache[ip] = {
                    str(u.user_id): (u.name or "").strip() for u in users}
            except Exception:
                pass
        _fp(ip, "attlog", "دریافت رکوردهای تردد")
        recs = [{
            "device": ip,
            "label": dev.get("label") or dev["info"].get("model", ""),
            "user_id": str(a.user_id),
            "name": _users_cache.get(ip, {}).get(str(a.user_id), ""),
            "timestamp": a.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "punch": a.punch,
            "punch_label": PUNCH_LABELS.get(a.punch, PUNCH_UNKNOWN),
            "status": a.status,
        } for a in conn.get_attendance()]
        recs.sort(key=lambda r: r["timestamp"], reverse=True)
        dev["last_state"] = "online"
        dev["last_check"] = datetime.now().isoformat(timespec="seconds")
        if recs:
            dev["last_log_ts"] = recs[0]["timestamp"]
        _save(DB)
        _fp(ip, "done", f"{len(recs)} رکورد", ok=True)
        return recs, len(_users_cache.get(ip, {})), None
    except Exception as e:
        dev["last_state"] = "offline"
        dev["last_check"] = datetime.now().isoformat(timespec="seconds")
        _save(DB)
        # capture the failing stage BEFORE _fp overwrites it (st is a live ref)
        failed_stage = (FETCH_PROGRESS.get(ip) or {}).get("stage", "?")
        _fp(ip, "error", f"{type(e).__name__}: {e}", ok=False)
        return [], 0, f"{failed_stage} → {type(e).__name__}: {e}"
    finally:
        if conn is not None:
            try:
                conn.enable_device()
            except Exception:
                pass
            try:
                conn.disconnect()
            except Exception:
                pass
        lock.release()


def zk_set_time(dev: dict, target=None):
    """Sync the device clock. A set_time is a 0.1s command, but it shares the
    per-device lock with syncs — and a green-label sync holds that lock for
    minutes. Acquire non-blocking with a hard timeout: fail fast with a clear
    message instead of hanging the HTTP request silently."""
    lock = _acquire_device_lock(
        dev["ip"], cfg("lock_timeout.set_time", 10))
    if lock is None:
        raise RuntimeError(
            "دستگاه مشغول همگام‌سازی است؛ پایان عملیات چند دقیقه طول "
            "می‌کشد — چند لحظه بعد دوباره تلاش کنید")
    conn = None
    try:
        conn = _zk(dev).connect()
        conn.set_time(target or datetime.now())
        return True
    finally:
        lock.release()
        if conn is not None:
            try:
                conn.disconnect()
            except Exception:
                pass


# ----------------------------------------------------------------------------
# Auto-sync poller — mirrors every enabled device into SQLite on a timer
# (zkteco_sync-style: pull once, store forever, query instantly)
# ----------------------------------------------------------------------------
SYNC_STATE = {"running": False, "last_run": None, "last_results": {},
              "stop": False, "cancel_requested": False,
              "current_device": None}

CONNECTION_LOG = []
CONNECTION_LOG_LOCK = threading.Lock()
CONNECTION_LOG_LIMIT = 2000


def _connection_log(ip, event, detail="", ok=None, source="sync"):
    entry = {"time": datetime.now().isoformat(timespec="seconds"),
             "device": ip, "event": event, "detail": str(detail),
             "ok": ok, "source": source}
    with CONNECTION_LOG_LOCK:
        CONNECTION_LOG.append(entry)
        del CONNECTION_LOG[:-CONNECTION_LOG_LIMIT]
    cache_invalidate("conn")
    return entry


# ----------------------------------------------------------------------------
# Tiny TTL cache for read-heavy endpoints (no Redis — a dict + lock is enough
# for this single-process app). cache_get(key, ttl, producer) returns the
# cached payload for `ttl` seconds; cache_invalidate(*keys) drops entries
# early (called on writes: sync finished, device registry changed, ADMS push
# stored punches) so the UI never sees stale data after a state change.
# ----------------------------------------------------------------------------
_CACHE: dict = {}                    # key -> (expires_at_monotonic, payload)
_CACHE_LOCK = threading.Lock()


def cache_get(key: str, ttl: float, producer):
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    payload = producer()           # build OUTSIDE the lock (re-entrancy safe)
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic() + ttl, payload)
    return payload


def cache_invalidate(*keys):
    with _CACHE_LOCK:
        if not keys:
            _CACHE.clear()
        else:
            for k in keys:
                _CACHE.pop(k, None)


# ----------------------------------------------------------------------------
# Runtime settings — data/settings.json, editable live from the UI (تنظیمات).
# Precedence everywhere: settings.json > env var > code default. cfg() is the
# single accessor; values take effect immediately (no restart) except where
# a component binds once at boot (waitress threads — noted in the UI).
# ----------------------------------------------------------------------------

_SETTINGS_DEFAULTS = {
    "cache_ttl": {
        "devices": 5,
        "scan": 10,
        "connection_logs": 5,
        "archive": 30,
        "sync": 2,
    },
    "lock_timeout": {
        "default": 45,     # ZK green-label full pulls
        "fk": 8,           # FK-5005 short exchanges
        "set_time": 10,    # time sync / user-management ops
    },
    "sync": {
        "auto_interval": 900,    # seconds between auto-sync passes
        "lock_queue_timeout": 30,
    },
    "backup": {
        "interval_hours": 6,
        "keep": 10,              # keep the N most recent backups
    },
    "log": {
        "max_bytes": 2000000,
        "backups": 3,
    },
    "web": {
        "threads": 16,           # applied at boot (waitress binds once)
    },
    "ui_polling": {              # consumed by the browser, not the server
        "devices_ms": 8000,
        "sync_ms": 5000,
        "scan_ms": 2000,
        "enroll_ms": 1200,
        "progress_active_ms": 1000,
        "progress_idle_ms": 5000,
    },
}

# numeric ranges for POST /api/settings validation: dotted key -> (min, max)
_SETTINGS_RANGE = {
    "cache_ttl.devices": (0, 600),
    "cache_ttl.scan": (0, 600),
    "cache_ttl.connection_logs": (0, 600),
    "cache_ttl.archive": (0, 600),
    "cache_ttl.sync": (0, 600),
    "lock_timeout.default": (3, 600),
    "lock_timeout.fk": (1, 120),
    "lock_timeout.set_time": (1, 120),
    "sync.auto_interval": (60, 86400),
    "sync.lock_queue_timeout": (5, 600),
    "backup.interval_hours": (1, 168),
    "backup.keep": (1, 100),
    "log.max_bytes": (100_000, 100_000_000),
    "log.backups": (0, 20),
    "web.threads": (1, 64),
    "ui_polling.devices_ms": (2000, 600000),
    "ui_polling.sync_ms": (2000, 600000),
    "ui_polling.scan_ms": (1000, 600000),
    "ui_polling.enroll_ms": (500, 60000),
    "ui_polling.progress_active_ms": (500, 60000),
    "ui_polling.progress_idle_ms": (2000, 600000),
}


def _settings_path() -> Path:
    return DATA_DIR / "settings.json"


def _deep_merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def load_settings() -> dict:
    """defaults <- settings.json (missing/invalid file simply keeps defaults)."""
    merged = json.loads(json.dumps(_SETTINGS_DEFAULTS))    # deep copy
    try:
        with open(_settings_path(), "r", encoding="utf-8") as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            _deep_merge(merged, stored)
    except (OSError, ValueError):
        pass
    return merged


def _validate_settings(new_values: dict) -> list:
    """Whitelist + numeric-range check; returns a list of error strings."""
    errs = []
    def walk(sub, prefix):
        for k, v in sub.items():
            dotted = f"{prefix}.{k}" if prefix else k
            if dotted not in _SETTINGS_RANGE:
                if isinstance(v, dict) and any(
                        d.startswith(dotted + ".") for d in _SETTINGS_RANGE):
                    walk(v, dotted)
                else:
                    errs.append(f"کلید ناشناخته: {dotted}")
                continue
            lo, hi = _SETTINGS_RANGE[dotted]
            ok = isinstance(v, (int, float)) and not isinstance(v, bool)                 and lo <= v <= hi
            if not ok:
                errs.append(f"{dotted} باید عددی بین {lo} و {hi} باشد")
            elif isinstance(v, float) and v.is_integer():
                sub[k] = int(v)
    walk(new_values, "")
    return errs


def save_settings(new_values: dict) -> dict:
    """Merge validated keys into settings.json; refresh the live dict."""
    with _SETTINGS_LOCK:
        current = load_settings()
        _deep_merge(current, new_values)
        tmp = _settings_path().with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _settings_path())   # atomic swap, works on all OSes
        _SETTINGS.clear()
        _SETTINGS.update(current)
    return current


_SETTINGS: dict = load_settings()
_SETTINGS_LOCK = threading.Lock()


def _env_num(name: str, default):
    try:
        v = os.environ.get(name)
        return int(v) if v else default
    except (TypeError, ValueError):
        return default


def _lookup(dotted: str, root: dict):
    node = root
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def cfg(dotted: str, default=None):
    """Precedence: settings.json > env var > built-in default > caller default."""
    node = _lookup(dotted, _SETTINGS)
    if node is not None:
        return node
    env = os.environ.get("HOZOR_" + dotted.upper().replace(".", "_"))
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    builtin = _lookup(dotted, _SETTINGS_DEFAULTS)
    if builtin is not None:
        return builtin
    return default


def _sync_one(dev: dict) -> dict:
    """Pull logs+users from one device and archive them. Never raises.

    On green-label devices zk_fetch_logs already pulls the user table inside
    the SAME session (opening a second session afterwards costs ~11s and
    risks the firmware's post-session cooldown) — so the users row-count is
    taken from the in-session fetch instead of reconnecting."""
    ip = dev["ip"]
    SYNC_STATE["current_device"] = ip
    _connection_log(ip, "sync-start", "شروع همگام‌سازی")
    try:
        recs, users_count, err = fetch_logs_for(dev, include_users=True)
        new = db_save_attendance(dev["ip"], recs)
        emp = 0
        if users_count:
            try:
                with _sqlite_lock, _db() as con:
                    emp = con.execute(
                        "SELECT COUNT(*) FROM employees WHERE device=?",
                        (dev["ip"],)).fetchone()[0]
            except Exception:
                emp = users_count
        elif is_fk_device(dev):
            # FK devices carry no user table fetch in this flow; names were
            # already captured by fk_fetch_logs into _users_cache.
            emp = len(_users_cache.get(dev["ip"], {}))
        else:
            try:
                with _dev_lock_for(dev["ip"]):
                    conn = _zk(dev).connect()
                    try:
                        if _is_green_label(conn):
                            conn.disable_device()
                            try:
                                ublob = _gl_pull_with_fallback(
                                    conn, dev["ip"], "users",
                                    const.CMD_USERTEMP_RRQ, const.FCT_USER)
                                emp = db_save_users(dev["ip"],
                                                    _gl_parse_users(ublob))
                            finally:
                                try:
                                    conn.enable_device()
                                except Exception:
                                    pass
                        else:
                            users = [{"uid": u.uid, "user_id": str(u.user_id),
                                      "name": (u.name or "").strip(),
                                      "privilege": int(u.privilege),
                                      "card": str(u.card)}
                                     for u in conn.get_users()]
                            emp = db_save_users(dev["ip"], users)
                    finally:
                        try:
                            conn.disconnect()
                        except Exception:
                            pass
            except Exception:
                emp = 0
        db_set_sync_state(dev["ip"], new, err or "")
        _connection_log(ip, "sync-finished",
                        f"تردد: {len(recs)}، رکورد جدید: {new}، کاربران: {emp}",
                        ok=not bool(err))
        if err:
            _connection_log(ip, "sync-error", err, ok=False)
        return {"fetched": len(recs), "new": new, "employees": emp,
                "error": err}
    except Exception as e:
        db_set_sync_state(dev["ip"], 0, f"{type(e).__name__}: {e}")
        _connection_log(ip, "sync-error", f"{type(e).__name__}: {e}", ok=False)
        return {"fetched": 0, "new": 0, "employees": 0,
                "error": f"{type(e).__name__}: {e}"}
    finally:
        SYNC_STATE["current_device"] = None


def _revival_watch_loop():
    """The green-label WiFi units accept TCP sessions only during short
    windows (after a power cycle, or when their push client is idle).
    Probe offline devices every minute; on the first answer, sync that
    device immediately while the window is still open."""
    db_init()
    while not SYNC_STATE["stop"]:
        time.sleep(60)
        if not DB.get("auto_sync", {}).get("enabled", True):
            continue
        for d in list(DB["devices"]):
            if SYNC_STATE["stop"] or SYNC_STATE["running"]:
                break
            if not d.get("enabled") or d.get("last_state") == "online":
                continue
            ip = d["ip"]
            if is_fk_device(d):
                # FK devices: a successful FK ping both proves reachability
                # and warms up the (stateless) protocol — nothing to re-pin.
                try:
                    with FKClient(ip, int(d.get("port", 5005)),
                                  timeout=3) as fk:
                        if not fk.ping():
                            continue
                except Exception:
                    continue
                if ip in _SYNC_PENDING_IPS:
                    continue
                _connection_log(ip, "revival",
                                "دستگاه FK دوباره پاسخ داد — همگام‌سازی فوری",
                                ok=True)
                _sync_queue_job([d])
                time.sleep(5)
                continue
            try:
                probe = ZK(ip, port=int(d.get("port", 4370)), timeout=2,
                           password=int(d.get("password", 0)),
                           ommit_ping=True).connect()
                probe.disconnect()
            except Exception:
                continue
            if ip in _SYNC_PENDING_IPS:
                continue
            _connection_log(ip, "revival",
                            "دستگاه دوباره پاسخ داد — همگام‌سازی فوری",
                            ok=True)
            # Green-label units push to whatever ADMS server their config
            # holds; another project may have re-pointed it (this is why .30
            # "worked yesterday" elsewhere). While we hold a session,
            # re-pin it to THIS server so its punches land in our archive.
            try:
                import socket as _s
                _sd = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
                try:
                    # The address the OS would use to reach THIS device —
                    # never 0.0.0.0 (gethostbyname(hostname) can resolve
                    # to it on machines with no direct LAN A-record).
                    _sd.connect((ip, int(d.get("port", 4370))))
                    lan_ip = _sd.getsockname()[0]
                finally:
                    _sd.close()
                c2 = ZK(ip, port=int(d.get("port", 4370)), timeout=20,
                        password=int(d.get("password", 0)),
                        ommit_ping=True).connect()
                try:
                    if _is_green_label(c2):
                        r = c2._ZK__send_command(
                            69, ("WebServerIP=%s,WebServerPort=%d" %
                                 (lan_ip, ADMS_PORT)).encode() + b"\x00")
                        if r.get("status"):
                            _connection_log(ip, "adms-repin",
                                            "سرور Push دستگاه روی این سرور "
                                            "تنظیم شد (%s:%s)" %
                                            (lan_ip, ADMS_PORT), ok=True)
                        try:
                            c2.restart()
                        except Exception:
                            pass
                finally:
                    try:
                        c2.disconnect()
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                _sync_queue_job([d])
            except Exception:
                pass
            time.sleep(5)# Single serialized sync worker: auto-sync passes, manual /api/sync runs and
# revival-watch urgent syncs all go through one queue and one thread — two
# passes can never interleave and fight over the same per-device lock.
_SYNC_QUEUE: "queue.Queue" = queue.Queue()
_SYNC_WORKER_STARTED = False


def _sync_queue_job(devices):
    """Queue a sync job for `devices`; starts the worker on first use."""
    global _SYNC_WORKER_STARTED
    if not _SYNC_WORKER_STARTED:
        with _dev_lock:
            if not _SYNC_WORKER_STARTED:
                threading.Thread(target=_sync_worker_loop, daemon=True,
                                 name="sync-worker").start()
                _SYNC_WORKER_STARTED = True
    for d in devices:
        _SYNC_PENDING_IPS.add(d["ip"])
    _SYNC_QUEUE.put(devices)


_SYNC_PENDING_IPS: set = set()


def _sync_worker_loop():
    while True:
        devices = _SYNC_QUEUE.get()          # blocks; single consumer
        try:
            if SYNC_STATE["running"]:
                # Serialize behind an in-flight pass instead of interleaving.
                while SYNC_STATE["running"] and not SYNC_STATE["stop"]:
                    time.sleep(1)
            SYNC_STATE["running"] = True
            SYNC_STATE["cancel_requested"] = False
            results = {}
            for d in devices:
                if SYNC_STATE["cancel_requested"]:
                    break
                try:
                    results[d["ip"]] = _sync_one(d)
                except Exception as e:
                    results[d["ip"]] = {"error": f"{type(e).__name__}: {e}"}
                finally:
                    _SYNC_PENDING_IPS.discard(d["ip"])
            SYNC_STATE["last_results"] = results
            SYNC_STATE["last_run"] = datetime.now().isoformat(timespec="seconds")
        finally:
            SYNC_STATE["running"] = False
            SYNC_STATE["cancel_requested"] = False
            cache_invalidate("sync", "devices")
            _SYNC_QUEUE.task_done()


def _auto_sync_loop():
    db_init()
    while not SYNC_STATE["stop"]:
        interval = int(DB.get("auto_sync", {}).get(
            "interval", cfg("sync.auto_interval", 900)))
        enabled = bool(DB.get("auto_sync", {}).get("enabled", True))
        if enabled and not SYNC_STATE["running"]:
            targets = [d for d in list(DB["devices"]) if d.get("enabled")]
            if targets:
                _sync_queue_job(targets)     # same worker as manual sync
        for _ in range(max(60, interval)):
            if SYNC_STATE["stop"]:
                return
            time.sleep(1)


_threading_started = False


def start_background_workers():
    global _threading_started
    if _threading_started:
        return
    _threading_started = True
    DB.setdefault("auto_sync", {"enabled": True, "interval": 900})
    _save(DB)
    ADMS["approved"].update(DB.get("adms_approved", []))
    threading.Thread(target=_auto_sync_loop, daemon=True,
                     name="auto-sync").start()
    threading.Thread(target=_revival_watch_loop, daemon=True,
                     name="revival-watch").start()
    threading.Thread(target=_adms_listener, daemon=True,
                     name="adms").start()


# ----------------------------------------------------------------------------
# ADMS push listener — devices push attendance in real time (HTTP on 8081).
# Patterned after zkteco_sync's /iclock endpoints; every serial must be
# approved once before its events are stored (approval queue).
# ----------------------------------------------------------------------------
ADMS_PORT = int(os.environ.get("ADMS_PORT", "8081"))
ADMS = {"events": 0, "pending": [], "approved": set(), "last": None,
        "cmds": {},          # sn -> {"next": id, "queue": [...]} (in-memory)
        "cmd_log": [],       # concluded commands, newest first
        "transfers": {}}     # (sn, cmdid, table) -> multi-packet reassembly
ADMS_LOCK = threading.Lock()
ADMS_BOOT = set()            # serials already bootstrapped this run


def _adms_log(msg):
    # Windows consoles/redirects default to a charmap codec; a non-encodable
    # character must never take down the response we are about to send.
    try:
        print(f"[adms] {msg}", flush=True)
    except UnicodeEncodeError:
        print("[adms] " + str(msg).encode("ascii", "replace").decode(),
              flush=True)


def _adms_encode_time(dt):
    """ZKTeco's packed clock for `SET OPTIONS DateTime=` (spec §12.5.1).
    NOT a Unix timestamp — every month has 31 days, by design. Verified
    against the spec's own worked example: 2018-02-22 14:54:54 = 583080894."""
    return (((dt.year - 2000) * 12 * 31 + (dt.month - 1) * 31 + dt.day - 1)
            * 86400 + (dt.hour * 60 + dt.minute) * 60 + dt.second)


# zkteco_sync's pinned handshake reply (Attendance PUSH protocol).
# Realtime=1 makes punches arrive the moment they happen; TransFlag advertises
# users+attlog. Byte-for-byte the block two of their production devices parse.
def _adms_option_block(sn):
    return "\n".join([
        f"GET OPTION FROM: {sn}",
        "ATTLOGStamp=9999",
        "OPERLOGStamp=9999",
        "ATTPHOTOStamp=None",
        "ErrorDelay=30",
        "Delay=10",
        "TransTimes=00:00;14:05",
        "TransInterval=1",
        "TransFlag=1111000000",
        "TimeZone=0",
        "Realtime=1",
        "Encrypt=None",
    ])


# ---- command queue (zkteco_sync's commands.py, minimal in-memory form) ----
def _adms_queue_cmd(sn, cmd):
    with ADMS_LOCK:
        st = ADMS["cmds"].setdefault(sn, {"next": 1, "queue": []})
        cid = st["next"]
        st["next"] += 1
        st["queue"].append(
            {"id": cid, "cmd": cmd, "sent": 0,
             "queued": datetime.now().isoformat(timespec="seconds")})
    _adms_log(f"queued C:{cid}:{cmd[:70]} for {sn}")
    return cid


def _adms_cancel_queries(sn, table="attlog"):
    """Remove stale query commands unsupported by Attendance PUSH units."""
    with ADMS_LOCK:
        st = ADMS["cmds"].get(sn)
        if not st:
            return 0
        before = len(st["queue"])
        st["queue"] = [c for c in st["queue"]
                        if not (c.get("cmd", "").startswith("DATA QUERY")
                                and f"tablename={table}" in c.get("cmd", ""))]
        return before - len(st["queue"])


def _adms_conclude(sn, cid, ret, note=""):
    with ADMS_LOCK:
        st = ADMS["cmds"].get(sn) or {}
        row = next((c for c in st.get("queue", []) if c["id"] == cid), None)
        if row:
            st["queue"].remove(row)
        ADMS["cmd_log"].insert(0, {
            "sn": sn, "id": cid, "cmd": (row or {}).get("cmd", ""),
            "return": ret, "note": note,
            "at": datetime.now().isoformat(timespec="seconds")})
        del ADMS["cmd_log"][30:]
    _adms_log(f"cmd {cid} for {sn}: return={ret} {note}")


def _adms_next_commands(sn):
    """Hand out queued commands as C:<id>:<cmd> lines (delivered once; after
    15 min without an ack the command is re-offered — the queue's patience)."""
    with ADMS_LOCK:
        st = ADMS["cmds"].get(sn)
        if not st:
            return []
        now = time.time()
        out = []
        for c in st["queue"]:
            if c.get("sent") and now - c["sent"] < 900:
                continue
            if not c.get("sent"):
                c["sent"] = now
            out.append(f"C:{c['id']}:{c['cmd']}")
            if len(out) >= 4:
                break
        return out


def _adms_accept_packet(sn, cmdid, table, body, packidx, packcnt):
    """Buffer one querydata packet; return the whole payload once complete."""
    key = (sn, cmdid, table.lower())
    now = time.time()
    with ADMS_LOCK:
        t = ADMS["transfers"]
        for k in [k for k, v in t.items() if now - v["updated"] > 600]:
            _adms_log(f"abandoned incomplete transfer {k} "
                      f"({len(v['parts'])}/{v['packcnt']} packets)")
            t.pop(k)
        e = t.get(key)
        if e and e["packcnt"] != packcnt:
            e = None
        if e is None:
            e = {"packcnt": packcnt, "parts": {}, "updated": now}
            t[key] = e
        e["parts"][packidx] = body
        e["updated"] = now
        if len(e["parts"]) < packcnt:
            return None
        t.pop(key)
        return "".join(e["parts"][i] for i in sorted(e["parts"]))


def _adms_parse_users(body):
    """tabledata tablename=user - keyed TSV, parsed by key never by position."""
    users = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        if line[:5].lower() == "user ":
            line = line[5:]
        f = {}
        for pair in line.split("\t"):
            k, sep, v = pair.partition("=")
            if sep:
                f[k.strip().lower()] = v.strip()
        pin = f.get("pin", "").strip()
        if not pin:
            continue
        users.append({
            "user_id": pin[:24], "name": f.get("name", ""),
            "privilege": int(f["privilege"]) if f.get("privilege", "").isdigit() else 0,
            "card": f.get("cardno") or "0", "uid": f.get("uid", "")})
    return users


def _adms_maybe_bootstrap(sn, ip):
    """Record the handshake without queueing unsupported pull commands.

    Attendance PUSH terminals upload ATTLOG/USER through cdata themselves;
    DATA QUERY is a Security PUSH command and can leave these units retrying.
    """
    if sn in ADMS_BOOT:
        return
    ADMS_BOOT.add(sn)
    _adms_cancel_queries(sn, "user")
    _adms_cancel_queries(sn, "attlog")
    _connection_log(ip, "adms-handshake",
                    "Attendance PUSH فعال؛ بدون DATA QUERY", ok=True,
                    source="adms")


def _adms_tabledata(ip, sn, q, body):
    tname = q.get("tablename", "").strip().lower()
    count = q.get("count", "").strip()
    if tname == "user":
        users = _adms_parse_users(body)
        if users:
            db_save_users(ip, users)
        _adms_log(f"tabledata user: {len(users)} users from SN={sn} ({ip})")
        return f"user={count or len(users)}"
    if tname == "attlog":
        n = _adms_store_events(ip, body)
        if n:
            ADMS["events"] += n
        _adms_log(f"tabledata attlog: +{n} rows from SN={sn} ({ip})")
        _connection_log(ip, "adms-attlog", f"دریافت {n} رکورد از Push",
                ok=True, source="adms")
        return f"attlog={count or n}"
    _adms_log(f"tabledata {tname!r} from SN={sn} — logged and acked")
    return f"{tname}={count or 0}" if tname else "OK"


def _adms_querydata(ip, sn, q, body):
    """DATA QUERY answers land here; reassemble packcnt/packidx fragments."""
    tname = q.get("tablename", "").strip()
    cmdid = q.get("cmdid", "").strip()
    count = q.get("count", "").strip()

    def _qi(v, d):
        v = (v or "").strip()
        return int(v) if v.isdigit() else d

    packcnt = max(1, _qi(q.get("packcnt"), 1))
    packidx = max(1, _qi(q.get("packidx"), 1))
    payload = body
    if packcnt > 1:
        payload = _adms_accept_packet(sn, cmdid, tname, body, packidx, packcnt)
        if payload is None:
            return f"{tname}={count or '?'}" if tname else "OK"
    ret = 0
    if tname.lower() == "user":
        users = _adms_parse_users(payload)
        if users:
            db_save_users(ip, users)
        ret = len(users)
        _adms_log(f"querydata user: {ret} users from SN={sn} ({ip})")
    elif tname.lower() == "attlog":
        ret = _adms_store_events(ip, payload)
        if ret:
            ADMS["events"] += ret
        _adms_log(f"querydata attlog: +{ret} rows from SN={sn} ({ip})")
    else:
        ret = len([l for l in payload.splitlines() if l.strip()])
        _adms_log(f"querydata {tname!r} from SN={sn} — {ret} lines, logged+acked")
    if cmdid.isdigit():
        _adms_conclude(sn, int(cmdid), ret, f"DATA QUERY {tname} → {ret} records")
    return f"{tname}={count or ret}" if tname else "OK"


def _adms_devicecmd(sn, body, q):
    """Ack lines: ID=<id>&Return=<code>&CMD=<name> (body preferred, query fallback)."""
    acks = []
    for line in body.replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        f = {}
        for pair in line.split("&"):
            k, _, v = pair.partition("=")
            if _:
                f[k.strip().lower()] = v.strip()
        if f.get("id", "").lstrip("-").isdigit():
            acks.append((int(f["id"]), f.get("return", ""), f.get("cmd", "")))
    if not acks and q.get("id", "").lstrip("-").isdigit():
        acks.append((int(q["id"]), q.get("return", ""), q.get("cmd", "")))
    for cid, ret, cmd in acks:
        _adms_conclude(sn, cid, ret or "?", f"{cmd} ack")
    if not acks:
        _adms_log(f"devicecmd from SN={sn}: {body[:200]!r}")


_DT_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")


def _adms_store_events(device_ip, body):
    """ATTLOG line parser — zkteco_sync's positional rules first (they run in
    production against these exact units): user_id \t timestamp \t status \t
    punch. The tolerant timestamp-scan fallback stays for firmwares that
    reorder fields, but position wins wherever the shape matches — the old
    heuristic mis-read a 2-digit user id (e.g. '96') as the punch code."""
    n = 0
    with _sqlite_lock, _db() as con:
        for line in body.splitlines():
            line = line.strip()
            if not line or line[:2] in ("C:", "S:", "O:") \
                    or line.startswith("TableName"):
                continue
            fields = line.split("\t")
            ts = None
            ts_i = None
            status, punch = 0, 255
            uid = None
            # -- positional (Attendance PUSH wire format) --------------
            if len(fields) >= 3:
                m = _DT_RE.fullmatch(fields[1].strip()) if len(fields) > 1 \
                    else None
                if m:
                    uid = fields[0].strip()
                    ts = "%s-%s-%s %s:%s:%s" % m.groups()
                    ts_i = 1
                    status = int(fields[2]) if fields[2].strip().isdigit() else 0
                    if len(fields) > 3 and fields[3].strip().isdigit():
                        punch = int(fields[3])
            # -- tolerant fallback (unknown order) ---------------------
            if ts is None:
                for i, f in enumerate(fields):
                    m = _DT_RE.search(f)
                    if m:
                        ts = "%s-%s-%s %s:%s:%s" % m.groups()
                        ts_i = i
                        break
                if ts is None:
                    continue
                uid = (fields[1] if ts_i == 0 else fields[0]).strip()
                punch = 255
                for f in fields:
                    fs = f.strip()
                    if fs.isdigit() and len(fs) <= 3:
                        punch = int(fs)
                        break
            if not uid:
                continue
            # Sanity window (zkteco_sync rule: never store what we cannot
            # trust). Follow-up pushes after SET OPTIONS carry packed-time
            # garbage like '2131-05-16' — skipped and logged, not stored.
            if not ("2000-" <= ts[:5] <= "2099-"):
                _adms_log(f"skipped non-ATTLOG line from {device_ip}: "
                          f"{line[:160]!r}")
                continue
            cur = con.execute(
                "INSERT OR IGNORE INTO attendance(device,user_id,ts,punch,"
                "status,source) VALUES(?,?,?,?,?,?)",
                (device_ip, uid, ts, punch, status, "adms"))
            if cur.rowcount:
                cache_invalidate("archive")
            n += cur.rowcount
    return n


def _adms_handle(client, addr):
    """One /iclock/* HTTP request from a device. Routing follows zkteco_sync
    ADMS router: cdata (handshake + push tables), getrequest (command queue
    drain), querydata (DATA QUERY answers, multi-packet), devicecmd (acks)."""
    try:
        client.settimeout(10)
        req = b""
        while b"\r\n\r\n" not in req and len(req) < 65536:
            ch = client.recv(4096)
            if not ch:
                return
            req += ch
        head, _, rest = req.partition(b"\r\n\r\n")
        lines = head.decode("latin-1", "replace").split("\r\n")
        method, path, _ver = (lines[0].split(" ") + ["", ""])[:3]
        clen = 0
        for ln in lines[1:]:
            if ln.lower().startswith("content-length:"):
                clen = int(ln.split(":", 1)[1].strip() or "0")
        while len(rest) < min(clen, 2 * 1024 * 1024):
            ch = client.recv(65536)
            if not ch:
                break
            rest += ch
        body = rest[:clen].decode("utf-8", "replace")

        # ---- query string, case-insensitive keys -----------------------
        q = {}
        if "?" in path:
            for part in path.split("?", 1)[1].split("&"):
                k, _, v = part.partition("=")
                if k:
                    q[k.lower()] = v
        sn = q.get("sn", "").strip()

        # ---- serial authorisation (same rule as before) ----------------
        if not sn:
            _adms_log(f"request without SN from {addr[0]}: {path[:100]}")
            reply = "OK"
        elif sn not in ADMS["approved"]:
            if sn not in [p["sn"] for p in ADMS["pending"]]:
                ADMS["pending"].append(
                    {"sn": sn, "ip": addr[0], "port": 4370,
                     "seen": datetime.now().isoformat(timespec="seconds")})
                _adms_log(f"pending device SN={sn} from {addr[0]}")
            reply = "OK"   # never 4xx: a refusing body restarts some firmwares' setup
        else:
            ADMS["last"] = datetime.now().isoformat(timespec="seconds")
            ip = next((d["ip"] for d in DB["devices"] if
                       (d.get("info", {}).get("serial") or "").strip() == sn
                       or (d.get("serial") or "").strip() == sn), addr[0])
            upath = path.split("?", 1)[0].rstrip("/").lower()

            if upath.endswith("/cdata"):
                table = q.get("table", "").strip()
                b = body.strip()
                if method == "GET" or (not b and table.lower() in ("", "options")):
                    # handshake — the byte-exact option block pins the protocol
                    reply = _adms_option_block(sn)
                    _adms_maybe_bootstrap(sn, ip)
                elif table.lower() == "attlog":
                    n = _adms_store_events(ip, body)
                    if n:
                        ADMS["events"] += n
                        _adms_log(f"+{n} events from SN={sn} ({ip})")
                    reply = "OK"
                elif table.lower() == "tabledata":
                    reply = _adms_tabledata(ip, sn, q, body)
                else:
                    # OPERLOG / rtlog / rtstate / unknown: acked, never
                    # silently dropped — the body reaches the console.
                    if b:
                        _adms_log(f"cdata table={table!r} from SN={sn}: {b[:400]!r}")
                    reply = "OK"
            elif upath.endswith("/getrequest"):
                cmds = _adms_next_commands(sn)
                _connection_log(ip, "adms-poll",
                                f"polling دستگاه؛ {len(cmds)} فرمان", ok=True,
                                source="adms")
                reply = "\n".join(cmds) if cmds else "OK"
            elif upath.endswith("/querydata"):
                reply = _adms_querydata(ip, sn, q, body)
            elif upath.endswith("/devicecmd"):
                _adms_devicecmd(sn, body, q)
                reply = "OK"
            elif upath.endswith("/ping"):
                reply = "OK"
            else:
                _adms_log(f"unhandled /iclock path {upath!r} from SN={sn}")
                reply = "OK"

        client.sendall(
            ("HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
             "Content-Length: %d\r\nConnection: close\r\n\r\n%s"
             % (len(reply.encode()), reply)).encode())
    except Exception as e:
        _adms_log(f"handler error from {addr[0]}: {e}")
    finally:
        try:
            client.close()
        except Exception:
            pass


def _adms_listener():
    db_init()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((HOST, ADMS_PORT))
        srv.listen(8)
        _adms_log(f"listening on {HOST}:{ADMS_PORT} — set device ADMS "
                  f"server to {HOST}:{ADMS_PORT}")
    except Exception as e:
        _adms_log(f"cannot bind port {ADMS_PORT}: {e}")
        return
    while True:
        try:
            client, addr = srv.accept()
            threading.Thread(target=_adms_handle, args=(client, addr),
                             daemon=True).start()
        except Exception:
            time.sleep(0.5)


# ----------------------------------------------------------------------------
# Network scanner (background thread + polling endpoint)
# ----------------------------------------------------------------------------
SCAN = {"running": False, "progress": "", "done": 0, "total": 0,
        "found": [], "started": None, "finished": None}


def _scan_worker(subnets: list, timeout: float, deep: bool):
    SCAN.update(running=True, progress="شروع…", done=0, total=0,
                found=[], started=datetime.now().isoformat(timespec="seconds"),
                finished=None)
    hosts = []
    for net in subnets:
        try:
            base = ".".join(net.split("/")[0].split(".")[:3])
            hosts.extend(f"{base}.{i}" for i in range(1, 255))
        except Exception:
            continue
    SCAN["total"] = len(hosts)
    found = []
    done = 0

    def probe(ip):
        # ZK clocks answer on 4370; FK/B-series (Faratechno AI09F-class)
        # answer only on 5005. Classify by whichever port accepts TCP.
        ok, ms = tcp_check(ip, 4370, timeout)
        if ok:
            return ip, 4370, ms
        ok5, ms5 = tcp_check(ip, 5005, timeout)
        if ok5:
            return ip, 5005, ms5
        return ip, None, None

    with ThreadPoolExecutor(max_workers=512) as ex:
        futs = {ex.submit(probe, h): h for h in hosts}
        for fut in as_completed(futs):
            ip, port, ms = fut.result()
            done += 1
            SCAN["done"] = done
            SCAN["progress"] = f"اسکن {done}/{SCAN['total']}"
            if port:
                found.append({"ip": ip, "port": port, "latency_ms": ms,
                              "model": "", "serial": "", "platform": "",
                              "firmware": "", "known": _find_dev(ip) is not None})

    if deep:                      # ZK handshake on candidates only
        SCAN["progress"] = "شناسایی دستگاه‌ها…"
        known_ips = {d["ip"] for d in DB["devices"]}

        def deep_probe(item):
            if item["port"] == 5005:
                # FK device: ping + record count instead of a ZK handshake
                try:
                    with FKClient(item["ip"], 5005, timeout=6) as fk:
                        if fk.ping():
                            item.update(model="Faratechno AI09F (FK/B-series)",
                                        platform="FK5005",
                                        serial="")
                            if item["ip"] in known_ips:
                                d = _find_dev(item["ip"])
                                if d:
                                    item["known"] = True
                                    d["last_state"] = "online"
                                    d["last_check"] = datetime.now(
                                    ).isoformat(timespec="seconds")
                except Exception:
                    pass
                return item
            dev = {"ip": item["ip"], "port": 4370, "password": 0,
                   "use_udp": False, "timeout": 6, "label": "", "info": {}}
            try:
                info = zk_identify(dev)
                item.update(model=info.get("model", ""),
                            serial=info.get("serial", ""),
                            platform=info.get("platform", ""),
                            firmware=info.get("firmware", ""))
                if item["ip"] in known_ips:
                    d = _find_dev(item["ip"])
                    if d:
                        item["known"] = True
                        d["info"] = info
                        d["last_state"] = "online"
                        d["last_check"] = datetime.now().isoformat(
                            timespec="seconds")
            except Exception:
                pass
            return item

        with ThreadPoolExecutor(max_workers=16) as ex:
            found = list(ex.map(deep_probe, found))
        _save(DB)

    found.sort(key=lambda x: tuple(int(p) for p in x["ip"].split(".")))
    SCAN["found"] = found
    SCAN["progress"] = f"پایان — {len(found)} دستگاه یافت شد"
    SCAN["running"] = False
    SCAN["finished"] = datetime.now().isoformat(timespec="seconds")
    cache_invalidate("scan", "devices")


# ----------------------------------------------------------------------------
# Flask app
# ----------------------------------------------------------------------------
app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0  # LAN app: always revalidate JS/CSS edits


NEW_INDEX = BASE_DIR / "static" / "index.html"


@app.get("/")
def index():
    """Modern UI (static/index.html) when present; legacy inline page otherwise."""
    if NEW_INDEX.exists():
        return send_from_directory(NEW_INDEX.parent, NEW_INDEX.name)
    return Response(HTML_PAGE, mimetype="text/html")


@app.get("/legacy")
def legacy_index():
    return Response(HTML_PAGE, mimetype="text/html")


@app.get("/health")
def health():
    return jsonify(ok=True, time=datetime.now().isoformat(timespec="seconds"))


# ---------------- devices CRUD ----------------
@app.get("/api/devices")
def api_devices():
    # 5 s TTL: kills the per-keystroke device-table churn without hiding
    # state changes (every registry write below calls cache_invalidate).
    return cache_get("devices", cfg("cache_ttl.devices", 5.0),
                      lambda: jsonify(
        devices=DB["devices"], subnets=DB.get("subnets", [])))


@app.post("/api/devices")
def api_device_add():
    b = request.get_json(force=True)
    ip = (b.get("ip") or "").strip()
    if not ip:
        return jsonify(error="IP لازم است"), 400
    with _dev_lock:
        if _find_dev(ip):
            return jsonify(error="دستگاه از قبل ثبت شده"), 409
        # Optional serial: normally discovered by identify/scan, but a
        # hand-entered value (from the device label) pins the ADMS identity
        # immediately and serves as an override when identify cannot read it.
        dev = {"ip": ip, "port": int(b.get("port") or 4370),
               "password": int(b.get("password") or 0),
               "use_udp": bool(b.get("use_udp", False)),
               "label": (b.get("label") or "").strip(),
               "location": (b.get("location") or "").strip(),
               "enabled": True,
               "info": {"serial": (b.get("serial") or "").strip()},
               "last_state": "unknown",
               "last_check": None, "last_log_ts": None}
        DB["devices"].append(dev)
        _save(DB)
    cache_invalidate("devices")
    return jsonify(ok=True, device=dev)


@app.patch("/api/devices/<ip>")
def api_device_edit(ip):
    """Edit device metadata (label, location, enabled) without re-adding."""
    b = request.get_json(force=True)
    with _dev_lock:
        dev = _find_dev(ip)
        if not dev:
            return jsonify(error="یافت نشد"), 404
        if "label" in b:
            dev["label"] = (b.get("label") or "").strip()
        if "location" in b:
            dev["location"] = (b.get("location") or "").strip()
        if "enabled" in b:
            dev["enabled"] = bool(b["enabled"])
        _save(DB)
    cache_invalidate("devices")
    return jsonify(ok=True, device=dev)


@app.delete("/api/devices/<ip>")
def api_device_del(ip):
    with _dev_lock:
        dev = _find_dev(ip)
        if not dev:
            return jsonify(error="یافت نشد"), 404
        DB["devices"].remove(dev)
        _save(DB)
    cache_invalidate("devices")
    return jsonify(ok=True)


@app.post("/api/devices/<ip>/ping")
def api_device_ping(ip):
    dev = _find_dev(ip)
    if not dev:
        return jsonify(error="یافت نشد"), 404
    timeout = float(dev.get("timeout", 6))
    ok, ms = tcp_check(dev["ip"], int(dev.get("port", 4370)), timeout)
    network_online = ok or icmp_check(dev["ip"], timeout)
    dev["last_state"] = "online" if network_online else "offline"
    dev["last_check"] = datetime.now().isoformat(timespec="seconds")
    _save(DB)
    cache_invalidate("devices")
    return jsonify(ok=network_online, tcp_ok=ok, latency_ms=ms,
                   state=dev["last_state"])


@app.post("/api/devices/<ip>/identify")
def api_device_identify(ip):
    dev = _find_dev(ip)
    if not dev:
        return jsonify(error="یافت نشد"), 404
    try:
        if is_fk_device(dev):
            with FKClient(dev["ip"], int(dev.get("port", 5005)),
                          timeout=int(dev.get("timeout", 10))) as fk:
                if not fk.ping():
                    raise ConnectionError("FK ping بدون پاسخ")
                count = fk.get_count()
            info = {"model": dev.get("label") or "Faratechno AI09F (FK/B-series)",
                    "protocol": "fk5005",
                    "serial": ((dev.get("info") or {}).get("serial") or "")}
            dev["info"] = info
            dev["last_state"] = "online"
            dev["last_check"] = datetime.now().isoformat(timespec="seconds")
            _save(DB)
            info = dict(info, fk_record_count=count)
            cache_invalidate("devices")
            return jsonify(ok=True, info=info)
        info = zk_identify(dev)
        return jsonify(ok=True, info=info)
    except Exception as e:
        dev["last_state"] = "offline"
        dev["last_check"] = datetime.now().isoformat(timespec="seconds")
        _save(DB)
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 502


@app.post("/api/devices/<ip>/set_time")
def api_device_set_time(ip):
    """Set the device clock — zkteco_sync style (devices.py /set_time).

    Two transports, chosen automatically:
    * Push devices (green-label WiFi, ADMS-approved): the spec §12.5.1
      command `SET OPTIONS DateTime=<packed>` is queued; the device executes
      it on its next getrequest poll (~12 s). The interactive TCP path is
      not available to this firmware.
    * Everything else: live set_time over TCP, immediately verified.
    Body (all optional): {"sync": true} (default — server time) or
    {"dt": "YYYY-MM-DD HH:MM:SS"} for an explicit wall-clock value."""
    dev = _find_dev(ip)
    if not dev:
        return jsonify(error="یافت نشد"), 404
    b = request.get_json(force=True, silent=True) or {}
    if b.get("dt"):
        try:
            target = datetime.strptime(str(b["dt"]).strip(),
                                       "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return jsonify(error="قالب زمان باید YYYY-MM-DD HH:MM:SS باشد"), 400
    else:
        target = datetime.now()
    info = dev.get("info") or {}
    is_push = ("AK3750" in (info.get("platform") or "").upper()
               or (info.get("serial") or "").upper().startswith("AW"))
    sn = ((info.get("serial") or "").strip()) if is_push else ""
    # Direct TCP works on every model incl. green-label (verified live:
    # the firmware rejects interactive ENROLL over TCP but accepts the
    # plain time-write). ADMS `SET OPTIONS DateTime=` is the fallback for
    # when the device is mid-sync / in its session cooldown.
    if is_fk_device(dev):
        return jsonify(ok=False,
                       error="تنظیم ساعت از راه دور برای دستگاه‌های FK "
                             "پشتیبانی نمی‌شود؛ از منوی خود دستگاه استفاده کنید"), 501
    try:
        zk_set_time(dev, target)
        _connection_log(ip, "set-time",
                        "ساعت از راه TCP تنظیم شد", ok=True, source="sdk")
        return jsonify(ok=True, transport="tcp",
                       time_set=target.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as tcp_err:
        if sn and sn in ADMS["approved"]:
            cid = _adms_queue_cmd(
                sn, "SET OPTIONS DateTime=%d" % _adms_encode_time(target))
            _connection_log(ip, "set-time",
                            f"TCP ناموفق ({type(tcp_err).__name__})؛ فرمان "
                            f"تنظیم ساعت C:{cid} در صف Push گذاشته شد",
                            ok=None, source="adms")
            return jsonify(ok=True, queued=True, cmd_id=cid, sn=sn,
                           time_set=target.strftime("%Y-%m-%d %H:%M:%S"),
                           note="دستگاه در اولین polling (حدود ۱۲ ثانیه) "
                                "ساعت را اعمال می‌کند")
        return jsonify(ok=False, error=str(tcp_err)), 502


@app.post("/api/devices/<ip>/restart")
def api_device_restart(ip):
    dev = _find_dev(ip)
    if not dev:
        return jsonify(error="یافت نشد"), 404
    if is_fk_device(dev):
        return jsonify(ok=False,
                       error="ری‌استارت از راه دور برای دستگاه‌های FK پشتیبانی "
                             "نمی‌شود؛ از منوی خود دستگاه استفاده کنید"), 501
    try:
        with _dev_lock_for(ip):
            conn = _zk(dev).connect()
            conn.restart()
            return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 502


@app.get("/api/devices/<ip>/users")
def api_device_users(ip):
    dev = _find_dev(ip)
    if not dev:
        return jsonify(error="یافت نشد"), 404
    try:
        if is_fk_device(dev):
            # FK/B-series: there is no separate user-table command; names
            # ride along with the attendance dump (one 01c7 per employee).
            recs, _n, err = fk_fetch_logs(dev, include_users=True)
            if err and not recs:
                return jsonify(ok=False, error=err), 502
            names = _users_cache.get(ip, {})
            users = [{"uid": i, "user_id": uid, "name": names.get(uid, ""),
                      "privilege": 0, "card": "0"}
                     for i, uid in enumerate(sorted({r["user_id"] for r in recs},
                                                    key=lambda x: (len(x), x)))]
            return jsonify(ok=True, users=users)
        with _dev_lock_for(ip):
            conn = _zk(dev).connect()
            try:
                if _is_green_label(conn):
                    conn.disable_device()
                    try:
                        ublob = _gl_pull_with_fallback(
                            conn, ip, "users",
                            const.CMD_USERTEMP_RRQ, const.FCT_USER)
                        users = _gl_parse_users(ublob)
                    finally:
                        try:
                            conn.enable_device()
                        except Exception:
                            pass
                else:
                    users = [{"uid": u.uid, "user_id": str(u.user_id),
                              "name": (u.name or "").strip(),
                              "privilege": int(u.privilege), "card": str(u.card)}
                             for u in conn.get_users()]
            finally:
                try:
                    conn.disconnect()
                except Exception:
                    pass
        return jsonify(ok=True, users=users)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 502


@app.get("/api/users")
def api_users():
    ip = request.args.get("device", "all")
    with _sqlite_lock, _db() as con:
        if ip == "all":
            rows = con.execute(
                "SELECT device,user_id,name,privilege,card,updated FROM employees "
                "ORDER BY device,user_id").fetchall()
        else:
            rows = con.execute(
                "SELECT device,user_id,name,privilege,card,updated FROM employees "
                "WHERE device=? ORDER BY user_id", (ip,)).fetchall()
    return jsonify(ok=True, users=[{"device": r[0], "user_id": r[1],
        "name": r[2], "privilege": r[3], "card": r[4], "updated": r[5]}
        for r in rows])


@app.post("/api/devices/<ip>/users")
def api_user_create(ip):
    dev = _find_dev(ip)
    if not dev:
        return jsonify(error="یافت نشد"), 404
    b = request.get_json(force=True, silent=True) or {}
    user_id = str(b.get("user_id") or "").strip()
    name = str(b.get("name") or "").strip()
    if not user_id or not name:
        return jsonify(error="شناسه و نام کاربر الزامی است"), 400
    try:
        uid = int(b.get("uid") or user_id)
        privilege = 14 if bool(b.get("admin")) else 0
        lock = _dev_lock_for(ip)
        lock.acquire()
        conn = None
        try:
            conn = _zk(dev).connect()
            try:
                conn.set_user(uid=uid, name=name, privilege=privilege,
                              password=str(b.get("password") or ""),
                              group_id="0", user_id=user_id,
                              card=int(b.get("card") or 0))
            finally:
                if conn is not None:
                    conn.disconnect()
        finally:
            lock.release()
        db_save_users(ip, [{"user_id": user_id, "name": name,
                            "privilege": privilege, "card": b.get("card") or 0,
                            "uid": uid}])
        return jsonify(ok=True, user={"device": ip, "uid": uid,
                       "user_id": user_id, "name": name})
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 502


@app.get("/api/devices/<ip>/enroll")
def api_enroll_status(ip):
    return jsonify(ok=True, enrollment=_enrollment_status(ip))


@app.post("/api/devices/<ip>/enroll")
def api_enroll_start(ip):
    dev = _find_dev(ip)
    if not dev:
        return jsonify(error="یافت نشد"), 404
    b = request.get_json(force=True, silent=True) or {}
    user_id = str(b.get("user_id") or "").strip()
    uid = int(b.get("uid") or user_id or 0)
    if not user_id:
        return jsonify(error="شناسه کاربر الزامی است"), 400
    source_ip = (b.get("source") or "").strip()
    with ENROLLMENT_LOCK:
        if ENROLLMENTS.get(ip, {}).get("running"):
            return jsonify(error="ثبت اثر انگشت این دستگاه در حال اجراست"), 409
        ENROLLMENTS[ip] = {"running": True, "stage": "starting",
                           "user_id": user_id, "uid": uid,
                           "source": source_ip,
                           "started": datetime.now().isoformat(timespec="seconds"),
                           "message": "در حال اتصال؛ منتظر بمانید"}

    def worker():
        conn = None
        try:
            lock = _dev_lock_for(ip)
            with lock:
                conn = _zk(dev).connect()
                if _is_green_label(conn):
                    # Green-label firmware rejects interactive enrollment
                    # (CMD 61 -> NAK, verified live) but ACCEPTS template
                    # writes (CMD 110, verified byte-identical round-trip).
                    # So: copy the fingerprint from a normal device.
                    if not source_ip or source_ip == ip:
                        with ENROLLMENT_LOCK:
                            ENROLLMENTS[ip].update(
                                running=False, stage="need-source", ok=False,
                                message=(
                                    "این دستگاه (Green Label) ثبت تعاملی با "
                                    "حسگر را از راه دور قبول نمی‌کند. انگشت را "
                                    "روی یک دستگاه دیگر (مثلاً UF100) ثبت کنید "
                                    "و «دستگاه مبدأ» را انتخاب کنید؛ قالب اثر "
                                    "انگشت به‌صورت خودکار کپی می‌شود."),
                                finished=datetime.now().isoformat(
                                    timespec="seconds"))
                        return
                    with ENROLLMENT_LOCK:
                        ENROLLMENTS[ip].update(
                            stage="copy-read",
                            message=(f"خواندن اثر انگشت کاربر از {source_ip}…"))
                    sdev = _find_dev(source_ip)
                    if not sdev:
                        raise RuntimeError("دستگاه مبدأ یافت نشد")
                    sconn = None
                    slock = _dev_lock_for(source_ip)
                    with slock:
                        try:
                            sconn = _zk(sdev).connect()
                            if _is_green_label(sconn):
                                raise RuntimeError(
                                    "دستگاه مبدأ هم Green Label است؛ کپی فقط "
                                    "از یک دستگاه معمولی (UF100/MB20) کار می‌کند")
                            fing = sconn.get_user_template(uid=int(uid),
                                                           temp_id=int(
                                                               b.get("finger") or 0),
                                                           user_id=user_id)
                            if not fing:
                                raise RuntimeError(
                                    "کاربر در دستگاه مبدأ اثر انگشت ثبت‌شده "
                                    "ندارد؛ اول روی دستگاه مبدأ ثبت کنید")
                            tpl = fing.template
                            sfid = int(fing.fid)
                        finally:
                            if sconn is not None:
                                try:
                                    sconn.disconnect()
                                except Exception:
                                    pass
                    with ENROLLMENT_LOCK:
                        ENROLLMENTS[ip].update(
                            stage="copy-write",
                            message=(f"نوشتن قالب ({len(tpl)} بایت) روی {ip}…"))
                    _gl_write_template(conn, uid, user_id,
                                       name=str(b.get("name") or ""),
                                       privilege=int(b.get("privilege") or 0),
                                       fid=sfid, template=tpl)
                    with ENROLLMENT_LOCK:
                        ENROLLMENTS[ip].update(
                            running=False, stage="done", ok=True,
                            message=("قالب اثر انگشت با موفقیت کپی شد"),
                            finished=datetime.now().isoformat(
                                timespec="seconds"))
                    return
                with ENROLLMENT_LOCK:
                    ENROLLMENTS[ip].update(stage="waiting", message=(
                        "انگشت را روی دستگاه بگذارید؛ ثبت معمولاً سه بار انجام می‌شود"))
                ok = conn.enroll_user(uid=uid, temp_id=0, user_id=user_id)
            with ENROLLMENT_LOCK:
                ENROLLMENTS[ip].update(running=False, stage="done", ok=bool(ok),
                                       message="ثبت اثر انگشت موفق بود" if ok else
                                       "دستگاه ثبت اثر انگشت را تأیید نکرد",
                                       finished=datetime.now().isoformat(timespec="seconds"))
        except Exception as e:
            with ENROLLMENT_LOCK:
                ENROLLMENTS[ip].update(running=False, stage="error", ok=False,
                    message=f"{type(e).__name__}: {e}",
                    finished=datetime.now().isoformat(timespec="seconds"))
        finally:
            if conn is not None:
                try:
                    conn.disconnect()
                except Exception:
                    pass

    threading.Thread(target=worker, daemon=True,
                     name=f"enroll-{ip}").start()
    return jsonify(ok=True, started=True, enrollment=_enrollment_status(ip))


@app.post("/api/devices/<ip>/enroll/cancel")
def api_enroll_cancel(ip):
    # pyzk's enroll_user owns the protocol exchange; cancellation is exposed
    # as an operator state and the worker releases the device lock on return.
    with ENROLLMENT_LOCK:
        job = ENROLLMENTS.get(ip)
        if not job or not job.get("running"):
            return jsonify(ok=True, cancelled=False, message="ثبت فعالی وجود ندارد")
        job.update(cancel_requested=True, message="لغو درخواست شد؛ در حال پایان ارتباط")
    return jsonify(ok=True, cancelled=True)


# ---------------- logs ----------------
@app.get("/api/fetch-progress")
def api_fetch_progress():
    """Live stage/progress of the last/current logs fetch per device."""
    return jsonify(ok=True, progress=FETCH_PROGRESS)


@app.get("/api/connection-logs")
def api_connection_logs():
    ip = request.args.get("device", "all").strip()
    limit = min(max(int(request.args.get("limit", 500)), 1),
                CONNECTION_LOG_LIMIT)

    def build():
        with CONNECTION_LOG_LOCK:
            rows = list(CONNECTION_LOG)
        if ip != "all":
            rows = [row for row in rows if row["device"] == ip]
        return jsonify(ok=True, logs=rows[-limit:][::-1],
                       total=len(rows),
                       devices=[d["ip"] for d in DB["devices"]])

    # 5 s TTL; the limit=2000 dashboard-style calls collapse into one build.
    return cache_get(f"conn:{ip}:{limit}",
                     cfg("cache_ttl.connection_logs", 5.0), build)


@app.get("/api/logs")
def api_logs():
    """?device=<ip|all>&from=YYYY-MM-DD&to=YYYY-MM-DD&limit=N"""
    ip = request.args.get("device", "all")
    d_from, d_to = request.args.get("from"), request.args.get("to")
    limit = min(int(request.args.get("limit", 5000)), 100000)

    targets = [d for d in DB["devices"] if d.get("enabled")] if ip == "all" \
        else ([d for d in DB["devices"] if d["ip"] == ip])
    if ip != "all" and not targets:
        return jsonify(error="یافت نشد"), 404

    timeout = request.args.get("timeout")

    def fetch_target(dev):
        target = dict(dev)
        if timeout:
            try:
                target["timeout"] = max(2, min(120, int(float(timeout))))
            except ValueError:
                pass
        return target, fetch_logs_for(target)

    all_recs, errors = [], {}
    if len(targets) > 1:
        with ThreadPoolExecutor(max_workers=min(4, len(targets))) as ex:
            futs = {ex.submit(fetch_target, d): d for d in targets}
            for fut in as_completed(futs):
                dev = futs[fut]
                target, result = fut.result()
                recs, _, err = result
                if err:
                    errors[target["ip"]] = err
                all_recs.extend(recs)
    elif targets:
        target, result = fetch_target(targets[0])
        recs, _, err = result
        if err:
            errors[target["ip"]] = err
        all_recs.extend(recs)

    if d_from:
        all_recs = [r for r in all_recs if r["timestamp"] >= d_from + " 00:00:00"]
    if d_to:
        all_recs = [r for r in all_recs
                    if r["timestamp"] <= d_to + " 23:59:59"]
    all_recs.sort(key=lambda r: r["timestamp"], reverse=True)

    # A device that failed the live pull still owes the user an answer: the
    # SQLite archive (fed by auto-sync and earlier successful pulls) usually
    # holds the requested range. Fill from the archive per failed device and
    # say so in the response.
    from_archive = {}
    if errors:
        failed_ips = [ip for ip in errors]
        name_cache = {}
        try:
            with _sqlite_lock, _db() as con:
                for dip in failed_ips:
                    conds, args = ["device=?"], [dip]
                    if d_from:
                        conds.append("ts>=?"); args.append(d_from + " 00:00:00")
                    if d_to:
                        conds.append("ts<=?"); args.append(d_to + " 23:59:59")
                    rows = con.execute(
                        f"SELECT user_id,ts,punch,status FROM attendance "
                        f"WHERE {' AND '.join(conds)} ORDER BY ts DESC",
                        args).fetchall()
                    if not rows:
                        continue
                    dev = _find_dev(dip) or {}
                    label = dev.get("label") or (dev.get("info") or {}).get("model", "")
                    names = _users_cache.get(dip, {})
                    got = [{"device": dip, "label": label,
                            "user_id": uid, "name": names.get(uid, ""),
                            "timestamp": ts,
                            "punch": p or 255,
                            "punch_label": PUNCH_LABELS.get(p or 255, PUNCH_UNKNOWN),
                            "status": st or 0,
                            "archived": True} for uid, ts, p, st in rows]
                    all_recs.extend(got)
                    from_archive[dip] = len(got)
                    errors[dip] += f" — {len(got)} رکورد از بایگانی نمایش داده شد"
        except Exception:
            pass
        all_recs.sort(key=lambda r: r["timestamp"], reverse=True)

    return jsonify(ok=True, total=len(all_recs), shown=min(limit, len(all_recs)),
                   records=all_recs[:limit], errors=errors,
                   from_archive=from_archive)


@app.get("/api/logs/new")
def api_new_logs():
    """Fetch live records and return only keys absent from the local archive."""
    ip = request.args.get("device", "all")
    d_from, d_to = request.args.get("from"), request.args.get("to")
    limit = min(int(request.args.get("limit", 5000)), 100000)
    targets = [d for d in DB["devices"] if d.get("enabled")] if ip == "all" \
        else [d for d in DB["devices"] if d["ip"] == ip]
    if ip != "all" and not targets:
        return jsonify(error="یافت نشد"), 404
    # Green-label WiFi units can take minutes to stream their whole ATTLOG.
    # Use their ADMS polling channel instead so the request returns immediately.
    if len(targets) == 1:
        device = targets[0]
        serial = ((device.get("info") or {}).get("serial") or "").strip()
        platform = ((device.get("info") or {}).get("platform") or "").upper()
        if serial and (serial.upper().startswith("AW") or
                       "AK3750" in platform):
            cancelled = _adms_cancel_queries(serial, "attlog")
            ADMS["approved"].add(serial)
            with _dev_lock:
                DB["adms_approved"] = sorted(ADMS["approved"])
                _save(DB)
            _connection_log(device["ip"], "adms-push-request",
                            f"Realtime فعال است؛ {cancelled} فرمان قدیمی حذف شد",
                            ok=True, source="adms")
            _connection_log(device["ip"], "adms-query-queued",
                            "منتظر polling و ارسال ATTLOG از دستگاه",
                            source="adms")
            return jsonify(ok=True, pending=True, cmd_id=None,
                           serial=serial, total=0, shown=0, records=[],
                           errors={}, note=(
                               "درخواست ارسال شد؛ دستگاه در polling بعدی "
                               "ترددهای جدید را به سرور ارسال می‌کند"))
    all_recs, errors = [], {}
    for dev in targets:
        target = dict(dev)
        try:
            if request.args.get("timeout"):
                target["timeout"] = max(2, min(120, int(float(
                    request.args["timeout"]))))
        except ValueError:
            pass
        recs, _, err = fetch_logs_for(target, include_users=True, fresh=True)
        if err:
            errors[target["ip"]] = err
        all_recs.extend(recs)
    if d_from:
        all_recs = [r for r in all_recs if r["timestamp"] >= d_from + " 00:00:00"]
    if d_to:
        all_recs = [r for r in all_recs if r["timestamp"] <= d_to + " 23:59:59"]
    keys = [(r["device"], r["user_id"], r["timestamp"], r.get("punch"))
            for r in all_recs]
    with _sqlite_lock, _db() as con:
        existing = set(con.execute(
            "SELECT device,user_id,ts,punch FROM attendance WHERE device IN "
            f"({','.join('?' for _ in targets)})",
            [d["ip"] for d in targets]).fetchall()) if targets else set()
    new_recs = [r for r, key in zip(all_recs, keys) if key not in existing]
    new_recs.sort(key=lambda r: r["timestamp"], reverse=True)
    return jsonify(ok=True, total=len(new_recs), shown=min(limit, len(new_recs)),
                   records=new_recs[:limit], errors=errors,
                   note="رکوردها فقط نمایش داده شدند و هنوز در بایگانی ذخیره نشده‌اند")


# ---------------- archive / auto-sync / ADMS ----------------
@app.get("/api/archive")
def api_archive():
    """Instant queries over the local SQLite mirror (30 s TTL cache,
    invalidated by every attendance/employee write)."""
    ip = request.args.get("device", "all")
    d_from, d_to = request.args.get("from"), request.args.get("to")
    q = request.args.get("q", "").strip()
    limit = min(int(request.args.get("limit", 5000)), 100000)
    ck = f"arch:{ip}:{d_from}:{d_to}:{q}:{limit}"
    return cache_get(ck, cfg("cache_ttl.archive", 30.0),
                     lambda: _archive_query(ip, d_from, d_to, q, limit))


def _archive_query(ip, d_from, d_to, q, limit):
    sql = "SELECT device,user_id,ts,punch,status,source FROM attendance WHERE 1=1"
    args = []
    if ip != "all":
        sql += " AND device=?"
        args.append(ip)
    if d_from:
        sql += " AND ts>=?"
        args.append(d_from + " 00:00:00")
    if d_to:
        sql += " AND ts<=?"
        args.append(d_to + " 23:59:59")
    if q:
        sql += " AND (user_id LIKE ? OR user_id IN"
        sql += " (SELECT user_id FROM employees WHERE name LIKE ?))"
        args += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    with _sqlite_lock, _db() as con:
        rows = con.execute(sql, args).fetchall()
        total = con.execute(
            "SELECT COUNT(*) FROM attendance").fetchone()[0]
    names = {}
    with _sqlite_lock, _db() as con:
        for dev, uid, nm in con.execute("SELECT device,user_id,name FROM employees"):
            names[(dev, uid)] = nm
    recs = [{
        "device": r[0],
        "label": (_find_dev(r[0]) or {}).get("label")
                 or (_find_dev(r[0]) or {}).get("info", {}).get("model", ""),
        "user_id": r[1],
        "name": names.get((r[0], r[1]), ""),
        "timestamp": r[2],
        "punch": r[3],
        "punch_label": PUNCH_LABELS.get(r[3], PUNCH_UNKNOWN),
        "status": r[4],
        "source": r[5],
    } for r in rows]
    return jsonify(ok=True, total=total, shown=len(recs), records=recs)

@app.get("/api/employees")
def api_employees():
    with _sqlite_lock, _db() as con:
        rows = con.execute(
            "SELECT device,user_id,name,privilege,card,updated FROM employees"
            " ORDER BY device,user_id").fetchall()
    return jsonify(ok=True, employees=[
        {"device": r[0], "user_id": r[1], "name": r[2],
         "privilege": r[3], "card": r[4], "updated": r[5]} for r in rows])


@app.post("/api/sync")
def api_sync_now():
    """Sync one device (or all) right now, via the single sync worker."""
    if SYNC_STATE["running"]:
        return jsonify(error="همگام‌سازی در حال اجراست"), 409
    ip = (request.get_json(force=True, silent=True) or {}).get("device", "all")
    targets = [d for d in DB["devices"]
               if d.get("enabled") and (ip == "all" or d["ip"] == ip)]
    if ip != "all" and not targets:
        return jsonify(error="یافت نشد"), 404

    _sync_queue_job(targets)
    return jsonify(ok=True, started=True, devices=[d["ip"] for d in targets])


@app.post("/api/sync/stop")
def api_sync_stop():
    if not SYNC_STATE["running"]:
        return jsonify(ok=True, stopped=False, message="همگام‌سازی فعالی وجود ندارد")
    SYNC_STATE["cancel_requested"] = True
    _connection_log(SYNC_STATE.get("current_device") or "all", "sync-stop",
                    "درخواست توقف توسط کاربر", ok=None, source="user")
    cache_invalidate("sync")
    return jsonify(ok=True, stopped=True, message="توقف پس از پایان دستگاه جاری انجام می‌شود")


@app.get("/api/sync")
def api_sync_state_ep():
    # 2 s TTL — the Sync tab polls this; state flips invalidate it below.
    return cache_get("sync", cfg("cache_ttl.sync", 2.0), _sync_state_payload)


def _sync_state_payload():
    with _sqlite_lock, _db() as con:
        states = con.execute(
            "SELECT device,last_sync,last_count,last_error FROM sync_state").fetchall()
        total = con.execute("SELECT COUNT(*) FROM attendance").fetchone()[0]
    return jsonify(ok=True, running=SYNC_STATE["running"],
                   last_run=SYNC_STATE["last_run"],
                   results=SYNC_STATE.get("last_results", {}),
                   auto=DB.get("auto_sync", {}),
                   db_total=total,
                   devices=[{"device": r[0], "last_sync": r[1],
                             "last_count": r[2], "last_error": r[3]}
                            for r in states])


@app.post("/api/sync/settings")
def api_sync_settings():
    b = request.get_json(force=True)
    with _dev_lock:
        a = DB.setdefault("auto_sync", {"enabled": True, "interval": 900})
        if "enabled" in b:
            a["enabled"] = bool(b["enabled"])
        if "interval" in b:
            a["interval"] = max(60, int(b["interval"]))
        _save(DB)
    cache_invalidate("sync")
    return jsonify(ok=True, auto=a)


@app.get("/api/settings")
def api_settings_get():
    """Full runtime settings + where the backup dir lives (native separators)."""
    env_overrides = []
    for dotted in sorted(_SETTINGS_RANGE):
        env_name = "HOZOR_" + dotted.upper().replace(".", "_")
        if os.environ.get(env_name):
            env_overrides.append(env_name)
    return jsonify(ok=True, settings=load_settings(),
                   defaults=json.loads(json.dumps(_SETTINGS_DEFAULTS)),
                   backup_dir=str(DATA_DIR / "backups"),
                   settings_file=str(_settings_path()),
                   env_overrides=env_overrides,
                   note="web.threads در بوت اعمال می‌شود (بعد از تغییر، ری‌استارت لازم است)")


@app.post("/api/settings")
def api_settings_post():
    """Update settings live. Whitelisted numeric keys only; atomic save."""
    b = request.get_json(force=True, silent=True)
    if not isinstance(b, dict) or not b:
        return jsonify(ok=False, error="بدنهٔ درخواست نامعتبر است"), 400
    errs = _validate_settings(b)
    if errs:
        return jsonify(ok=False, error="؛ ".join(errs)), 400
    saved = save_settings(b)
    # TTLs changed -> drop cached payloads so new values take effect at once
    cache_invalidate()
    _connection_log("server", "settings", 
                    ", ".join(sorted(_flatten_keys(b))),
                    ok=True, source="system")
    return jsonify(ok=True, settings=saved)


def _flatten_keys(d: dict, prefix: str = "") -> list:
    out = []
    for k, v in d.items():
        dotted = f"{prefix}.{k}" if prefix else k
        out.extend(_flatten_keys(v, dotted) if isinstance(v, dict) else [dotted])
    return out


@app.get("/api/adms")
def api_adms_status():
    with ADMS_LOCK:
        queues = {sn: [dict(c, sent=bool(c["sent"])) for c in st["queue"]]
                  for sn, st in ADMS["cmds"].items() if st["queue"]}
        return jsonify(ok=True, port=ADMS_PORT, events=ADMS["events"],
                       last=ADMS["last"],
                       pending=ADMS["pending"],
                       approved=sorted(ADMS["approved"]),
                       queues=queues, cmd_log=ADMS["cmd_log"][:10])


@app.post("/api/adms/approve")
def api_adms_approve():
    b = request.get_json(force=True)
    sn = (b.get("sn") or "").strip()
    if not sn:
        return jsonify(error="SN لازم است"), 400
    if sn not in ADMS["approved"]:
        ADMS["approved"].add(sn)
        with _dev_lock:
            DB["adms_approved"] = sorted(ADMS["approved"])
            _save(DB)
    ADMS["pending"] = [p for p in ADMS["pending"] if p["sn"] != sn]
    return jsonify(ok=True, approved=sorted(ADMS["approved"]))


def _adms_sn_for_ip(ip):
    d = _find_dev(ip)
    return ((d or {}).get("info") or {}).get("serial") or ""


def _adms_device_for_key(key):
    """Resolve an ADMS query target sent as either IP or serial number."""
    key = (key or "").strip()
    device = _find_dev(key)
    if device:
        return device
    return next((d for d in DB["devices"]
                 if ((d.get("info") or {}).get("serial") or "").strip() == key),
                None)


@app.post("/api/adms/query")
def api_adms_query():
    """Request a push refresh; Attendance PUSH devices are not queried."""
    b = request.get_json(force=True, silent=True) or {}
    device_key = (b.get("device") or "").strip()
    table = (b.get("table") or "attlog").strip().lower()
    if table not in ("attlog", "user"):
        return jsonify(error="table باید attlog یا user باشد"), 400
    dev = _adms_device_for_key(device_key)
    sn = ((dev or {}).get("info") or {}).get("serial") or ""
    if not sn:
        return jsonify(error="سریال دستگاه یافت نشد — ابتدا identify کنید"), 400
    if table == "attlog" and (sn.upper().startswith("AW") or
                               "AK3750" in ((dev.get("info") or {}).get(
                                   "platform") or "").upper()):
        cancelled = _adms_cancel_queries(sn, table)
        return jsonify(ok=True, sn=sn, pending=True, cmd_id=None,
                       cancelled=cancelled,
                       note="Realtime فعال است؛ منتظر ارسال ATTLOG دستگاه باشید")
    if sn not in ADMS["approved"]:
        ADMS["approved"].add(sn)
        with _dev_lock:
            DB["adms_approved"] = sorted(ADMS["approved"])
            _save(DB)
    cmd = f"DATA QUERY tablename={table},fielddesc=*,filter=*"
    cid = _adms_queue_cmd(sn, cmd)
    return jsonify(ok=True, sn=sn, cmd_id=cid, cmd=cmd,
                   note="دستگاه در اولین polling دستور را دریافت و داده را "
                        "به سرور push می‌کند")


@app.get("/api/archive.csv")
def api_archive_csv():
    """CSV export straight from the SQLite archive (instant, any range)."""
    j = api_archive()
    data = j.get_json()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADERS + ["منبع"])
    for r in data.get("records", []):
        w.writerow([r["label"], r["device"], r["user_id"], r["name"],
                    r["timestamp"], r["punch_label"], r["status"],
                    r["source"]])
    return Response(buf.getvalue().encode("utf-8-sig"),
                    mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f"attachment; filename=archive_"
                             f"{date.today().isoformat()}.csv"})


# ---------------- scan ----------------
@app.post("/api/scan")
def api_scan_start():
    if SCAN["running"]:
        return jsonify(error="اسکن در حال اجراست"), 409
    b = request.get_json(force=True, silent=True) or {}
    subnets = [s.strip() for s in (b.get("subnets") or DB.get("subnets") or
                                   DEFAULT_SUBNETS) if s.strip()]
    DB["subnets"] = subnets
    _save(DB)
    timeout = float(b.get("timeout", 0.6))
    deep = bool(b.get("deep", True))
    threading.Thread(target=_scan_worker, args=(subnets, timeout, deep),
                     daemon=True).start()
    return jsonify(ok=True)


@app.get("/api/scan")
def api_scan_state():
    # 10 s TTL — but only while idle; a running scan is always served live.
    if SCAN["running"]:
        return jsonify(SCAN)
    return cache_get("scan", cfg("cache_ttl.scan", 10.0),
                     lambda: jsonify(SCAN))


@app.post("/api/scan/adopt")
def api_scan_adopt():
    b = request.get_json(force=True)
    ip = (b.get("ip") or "").strip()
    if not ip:
        return jsonify(error="IP لازم است"), 400
    with _dev_lock:
        if _find_dev(ip):
            return jsonify(ok=True, existed=True)
        dev = {"ip": ip, "port": int(b.get("port") or 4370), "password": 0,
               "use_udp": False, "label": b.get("model") or "",
               "location": "", "enabled": True,
               "info": {"model": b.get("model", ""),
                        "serial": b.get("serial", ""),
                        "platform": b.get("platform", ""),
                        "firmware": b.get("firmware", "")},
               "last_state": "online",
               "last_check": datetime.now().isoformat(timespec="seconds"),
               "last_log_ts": None}
        DB["devices"].append(dev)
        _save(DB)
    return jsonify(ok=True, existed=False)


# ---------------- export ----------------
def _export_rows(d_from, d_to, ip):
    """Re-fetch logs and return filtered rows (shared by csv/xlsx)."""
    targets = [d for d in DB["devices"] if d.get("enabled")] if ip == "all" \
        else [d for d in DB["devices"] if d["ip"] == ip]
    rows = []
    with ThreadPoolExecutor(max_workers=min(4, len(targets) or 1)) as ex:
        for recs, _, _ in ex.map(zk_fetch_logs, targets):
            rows.extend(recs)
    if d_from:
        rows = [r for r in rows if r["timestamp"] >= d_from + " 00:00:00"]
    if d_to:
        rows = [r for r in rows if r["timestamp"] <= d_to + " 23:59:59"]
    rows.sort(key=lambda r: r["timestamp"], reverse=True)
    return rows


HEADERS = ["دستگاه", "IP", "شناسه کاربر", "نام", "زمان", "نوع", "وضعیت"]


@app.get("/api/export.csv")
def api_export_csv():
    rows = _export_rows(request.args.get("from"), request.args.get("to"),
                        request.args.get("device", "all"))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADERS)
    for r in rows:
        w.writerow([r["label"], r["device"], r["user_id"], r["name"],
                    r["timestamp"], r["punch_label"], r["status"]])
    data = buf.getvalue().encode("utf-8-sig")   # Excel-friendly UTF-8
    return Response(data, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f"attachment; filename=attendance_"
                             f"{date.today().isoformat()}.csv"})


@app.get("/api/export.xlsx")
def api_export_xlsx():
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
        from openpyxl.utils import get_column_letter
    except ImportError:
        return jsonify(error="openpyxl نصب نیست: pip install openpyxl"), 500

    rows = _export_rows(request.args.get("from"), request.args.get("to"),
                        request.args.get("device", "all"))
    wb = Workbook()
    ws = wb.active
    ws.title = "تردد"
    ws.sheet_view.rightToLeft = True
    ws.append(HEADERS)
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows:
        ws.append([r["label"], r["device"], r["user_id"], r["name"],
                   r["timestamp"], r["punch_label"], r["status"]])
    for i, width in enumerate((16, 14, 14, 24, 20, 16, 10), start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    bio = io.BytesIO()
    wb.save(bio)
    return Response(bio.getvalue(),
                    mimetype="application/vnd.openxmlformats-officedocument"
                             ".spreadsheetml.sheet",
                    headers={"Content-Disposition":
                             f"attachment; filename=attendance_"
                             f"{date.today().isoformat()}.xlsx"})


# ----------------------------------------------------------------------------
# UI (single page, RTL, vanilla JS)
# ----------------------------------------------------------------------------
HTML_PAGE = r"""<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>سامانه حضور و غیاب — استعلام و مدیریت دستگاه‌ها</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--card2:#273449;--txt:#e2e8f0;--mut:#94a3b8;
--acc:#38bdf8;--ok:#34d399;--bad:#f87171;--warn:#fbbf24;--bd:#334155}
*{box-sizing:border-box}

/* ---------- custom scrollbars (تم هم‌رنگ UI) ---------- */
/* Firefox */
*{scrollbar-width:thin;scrollbar-color:var(--scroll-thumb,rgba(56,189,248,.45)) transparent}
/* Webkit (Chrome/Edge/Safari) */
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(56,189,248,.45);border-radius:8px;
 border:2px solid transparent;background-clip:content-box}
::-webkit-scrollbar-thumb:hover{background:rgba(56,189,248,.75);
 border:2px solid transparent;background-clip:content-box}
::-webkit-scrollbar-corner{background:transparent}
/* dark color-scheme so native widgets (select, checkbox) follow the theme */
:root{color-scheme:dark}

/* fade hint beside horizontally scrollable tables: in RTL the overflow
   continues at the LEFT edge, so the gradient sits there; it only appears
   (.has-hscroll) when the table really overflows. Non-interactive. */
.twrap{position:relative;overflow:auto}
.twrap::after{content:'';position:absolute;top:0;bottom:0;left:0;width:26px;
 pointer-events:none;opacity:0;transition:opacity .25s;
 background:linear-gradient(to left,var(--card),transparent)}
.twrap.has-hscroll::after{opacity:1}

body{margin:0;background:var(--bg);color:var(--txt);
font-family:Tahoma,"Segoe UI",sans-serif;font-size:13px}
header{display:flex;align-items:center;gap:12px;padding:12px 20px;
background:var(--card);border-bottom:1px solid var(--bd);position:sticky;top:0;z-index:9}
header h1{font-size:16px;margin:0;color:var(--acc)}
.badge{padding:2px 10px;border-radius:20px;font-size:11px;background:var(--card2)}
main{max-width:1250px;margin:0 auto;padding:16px}
.tabs{display:flex;gap:6px;margin-bottom:14px}
.tabs button{background:var(--card);color:var(--mut);border:1px solid var(--bd);
padding:8px 18px;border-radius:8px 8px 0 0;cursor:pointer;font-family:inherit;font-size:13px}
.tabs button.on{color:var(--acc);border-bottom-color:var(--bg);font-weight:bold}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;
padding:14px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}
.kpi{background:var(--card2);border-radius:10px;padding:12px;text-align:center}
.kpi b{font-size:26px;display:block;color:var(--acc)}
.kpi.ok b{color:var(--ok)} .kpi.bad b{color:var(--bad)}
table{width:100%;border-collapse:collapse}
th{color:var(--mut);text-align:right;padding:7px 8px;border-bottom:2px solid var(--bd);
font-size:12px;white-space:nowrap}
td{padding:7px 8px;border-bottom:1px solid var(--bd);vertical-align:middle}
tr:hover td{background:#243247}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-left:5px}
.on{background:var(--ok)} .off{background:var(--bad)} .unk{background:var(--mut)}
.btn{background:var(--card2);color:var(--txt);border:1px solid var(--bd);
padding:5px 11px;border-radius:6px;cursor:pointer;font-family:inherit;font-size:12px}
.btn:hover{border-color:var(--acc);color:var(--acc)}
.btn.busy{color:var(--acc);border-color:var(--acc)}
.spinner{display:inline-block;width:11px;height:11px;margin-left:5px;
border:2px solid currentColor;border-left-color:transparent;border-radius:50%;
vertical-align:-1px;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.btn.p{background:#075985;border-color:#0369a1;color:#fff}
.btn.d{background:#7f1d1d;border-color:#991b1b;color:#fff}
.btn:disabled{opacity:.45;cursor:not-allowed}
input,select,textarea{background:#0b1220;color:var(--txt);border:1px solid var(--bd);
border-radius:6px;padding:6px 8px;font-family:inherit;font-size:12px}
textarea{width:100%;min-height:90px;direction:ltr;text-align:left}
label{color:var(--mut);font-size:11px;display:block;margin-bottom:3px}
#nav-btn{display:none;background:var(--card2);color:var(--txt);border:1px solid var(--bd);
border-radius:6px;cursor:pointer;font-size:16px;line-height:1;padding:6px 10px}
#nav-btn:hover{border-color:var(--acc)}

/* ---------- responsive: tablet ≤1024px ---------- */
@media (max-width:1024px){
 main{padding:12px}
 .toast{max-width:300px}
}
/* ---------- responsive: small tablet / large phone ≤768px ---------- */
@media (max-width:768px){
 ::-webkit-scrollbar{display:none;width:0;height:0}
 *{scrollbar-width:none;-ms-overflow-style:none}
 body{font-size:13px}
 header{padding:8px 10px;gap:8px;position:sticky;top:0}
 header h1{font-size:13px}
 .badge{display:none}
 #nav-btn{display:inline-block}
 .tabs{display:none;position:absolute;top:0;left:0;right:0;flex-direction:column;
  gap:0;background:var(--card);border-bottom:1px solid var(--bd);z-index:8;
  max-height:70vh;overflow-y:auto;margin-bottom:0;padding:4px 0}
 .tabs button{border-radius:0;border:none;border-bottom:1px solid var(--bd);
  padding:13px 18px;text-align:right;font-size:14px}
 .tabs button.on{border-bottom-color:var(--bd)}
 body.nav-open .tabs{display:flex}
 main{padding:10px;max-width:100%;position:relative}
 .row{flex-direction:column;align-items:stretch}
 .row>div{min-width:0;width:100%}
 .card{padding:10px;border-radius:8px}
 .kpi b{font-size:22px}
 .actions .btn{padding:9px 12px;font-size:13px}
}
/* ---------- responsive: phone ≤480px ---------- */
@media (max-width:480px){
 body{font-size:14px;overflow-x:hidden}
 header h1{font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 header .btn{padding:9px 10px;min-height:38px}
 .tabs button{min-height:44px;font-size:14px}
 .btn,.st-in{min-height:40px;font-size:13px}
 input,select,textarea{font-size:14px;min-height:40px}
 label{font-size:12px}
 td,th{padding:9px 7px;font-size:12px}
 .card{margin-bottom:10px}
 .kpi b{font-size:20px}
 .actions{gap:6px}
 .actions .btn{flex:1 1 auto}
}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end}
.row>div{flex:1;min-width:130px}
.mut{color:var(--mut)} .ltr{direction:ltr;text-align:left;display:inline-block}
#toasts{position:fixed;bottom:14px;left:14px;z-index:99;display:flex;
flex-direction:column;gap:6px}
.toast{background:var(--card2);border:1px solid var(--acc);border-radius:8px;
padding:9px 14px;max-width:420px;box-shadow:0 4px 14px #0008}
.toast.err{border-color:var(--bad)}
.bar{height:6px;background:var(--card2);border-radius:4px;overflow:hidden;margin-top:8px}
.bar i{display:block;height:100%;background:var(--acc);width:0;transition:width .3s}
h2{font-size:14px;margin:0 0 10px;color:var(--acc)}
.actions{display:flex;gap:5px;flex-wrap:wrap}
.tag{background:var(--card2);border:1px solid var(--bd);border-radius:5px;
padding:1px 7px;font-size:11px;color:var(--mut)}
</style>
</head>
<body>
<header>
  <button id="nav-btn" aria-label="منو" onclick="document.body.classList.toggle('nav-open')">☰</button>
  <h1>🖥 سامانه حضور و غیاب</h1>
  <span class="badge" id="clock"></span>
  <span style="flex:1"></span>
    <button class="btn d" id="stop-sync-btn" onclick="stopSync()">توقف همگام‌سازی</button>
  <button class="btn p" onclick="checkAll()">بررسی اتصال همه</button>
  <button class="btn" onclick="loadDevices(true)">↻</button>
</header>
<main>

<div class="tabs">
  <button class="on" data-t="dash" onclick="tab('dash',this)">داشبورد</button>
  <button data-t="devs" onclick="tab('devs',this)">دستگاه‌ها</button>
    <button data-t="users" onclick="tab('users',this)">کاربران</button>
  <button data-t="logs" onclick="tab('logs',this)">ترددها</button>
  <button data-t="arch" onclick="tab('arch',this)">بایگانی</button>
  <button data-t="sync" onclick="tab('sync',this)">همگام‌سازی</button>
    <button data-t="conn" onclick="tab('conn',this)">لاگ ارتباط</button>
  <button data-t="scan" onclick="tab('scan',this)">اسکن شبکه</button>
  <button data-t="settings" onclick="tab('settings',this)">تنظیمات</button>
</div>

<!-- ============ DASHBOARD ============ -->
<section id="t-dash">
  <div class="card"><div class="grid" id="kpis"></div></div>
  <div class="card"><h2>وضعیت سریع دستگاه‌ها</h2>
    <div class="twrap"><table id="dash-tbl"></table></div></div>
</section>

<!-- ============ USERS ============ -->
<section id="t-users" style="display:none">
    <div class="card"><h2>مدیریت کاربران</h2>
        <div class="row">
            <div><label>دستگاه</label><select id="u-dev" onchange="loadUsers()"></select></div>
            <div><label>فیلتر دستگاه</label><input id="u-filter" placeholder="IP یا مدل" oninput="filterUserDevices()" style="direction:ltr"></div>
            <div><label>شناسه کاربر</label><input id="u-id" style="direction:ltr"></div>
            <div><label>UID عددی</label><input id="u-uid" style="direction:ltr"></div>
            <div><label>نام و نام خانوادگی</label><input id="u-name"></div>
            <div><label>شماره کارت اختیاری</label><input id="u-card" style="direction:ltr"></div>
            <div><label>&nbsp;</label><button class="btn p" onclick="createUser()">ساخت کاربر روی دستگاه</button></div>
        </div>
        <div class="row">
            <div><label>دستگاه مبدأ برای کپی اثر انگشت (اختیاری)</label><select id="u-src"></select></div>
            <div style="flex:2"><label>&nbsp;</label><div class="mut">دستگاه‌های Green Label (مثل WL50) ثبت تعاملی با حسگر را از راه دور قبول نمی‌کنند؛ اثر انگشت را روی دستگاه مبدأ ثبت کنید و از آنجا کپی شود. اگر خالی بماند، ثبت مستقیم روی خود دستگاه انجام می‌شود.</div></div>
        </div>
        <div class="mut" style="margin-top:8px">پس از ساخت کاربر، برای ثبت اثر انگشت همان ردیف روی «شروع ثبت اثر انگشت» بزنید و شخص سه بار انگشت خود را روی دستگاه قرار دهد.</div>
        <div id="u-sum" class="mut" style="margin-top:8px"></div>
    </div>
    <div class="card"><div class="twrap"><table id="users-tbl"></table></div></div>
</section>

<!-- ============ DEVICES ============ -->
<section id="t-devs" style="display:none">
  <div class="card"><h2>دستگاه‌های ثبت‌شده</h2>
    <div class="twrap"><table id="dev-tbl"></table></div></div>
  <div class="card"><h2>افزودن دستگاه</h2>
    <div class="row">
      <div><label>IP دستگاه</label><input id="n-ip" placeholder="172.16.x.x" style="direction:ltr"></div>
      <div><label>پورت</label><input id="n-port" value="4370" style="direction:ltr;width:80px"></div>
      <div><label>رمز (0=بدون رمز)</label><input id="n-pass" value="0" style="direction:ltr;width:90px"></div>
      <div><label>شماره سریال (اختیاری)</label><input id="n-serial" placeholder="مثلا AWWZ195160307" style="direction:ltr"></div>
      <div><label>برچسب / مدل</label><input id="n-label" placeholder="مثلا MB20"></div>
      <div><label>موقعیت</label><input id="n-loc" placeholder="مثلا دفتر مرکزی"></div>
      <div><button class="btn p" onclick="addDevice()">افزودن</button></div>
    </div>
    <p class="mut" style="margin-bottom:0">سریال معمولاً خودکار خوانده می‌شود (identify)؛ فقط برای دستگاهی که در لحظه افزودن آفلاین است، از روی برچسب دستگاه وارد کنید.
    مدل‌های پشتیبانی‌شده: UF100، MB20، F70، WL50، Timmy AI09F
    (پروتکل ZK روی TCP/UDP 4370)</p>
  </div>
</section>

<!-- ============ LOGS ============ -->
<section id="t-logs" style="display:none">
  <div class="card"><h2>استعلام تردد</h2>
    <div class="row">
      <div><label>دستگاه</label><select id="q-dev"></select></div>
      <div><label>از تاریخ</label><input id="q-from" type="date"></div>
      <div><label>تا تاریخ</label><input id="q-to" type="date"></div>
      <div><label>تایم‌اوت (ثانیه)</label><input id="q-timeout" value="10" style="direction:ltr;width:80px"
             title="حداکثر انتظار برای هر بسته شبکه — برای دستگاه‌های WiFi کندتر، بالاتر بگذارید"></div>
      <div style="flex:0"><label>&nbsp;</label><button class="btn p" onclick="fetchLogs()">استعلام</button></div>
          <div style="flex:0"><label>&nbsp;</label><button class="btn" onclick="fetchArchivedLogs()">نمایش فوری بایگانی</button></div>
    <div style="flex:0"><label>&nbsp;</label><button class="btn" onclick="fetchNewLogs()">بررسی رکوردهای جدید</button></div>
      <div style="flex:0"><label>&nbsp;</label><button class="btn" onclick="exportData('csv')">خروجی CSV</button></div>
      <div style="flex:0"><label>&nbsp;</label><button class="btn" onclick="exportData('xlsx')">خروجی Excel</button></div>
    </div>
    <div id="log-sum" class="mut" style="margin-top:8px"></div>
    <div id="fetch-prog" class="mut" style="margin-top:4px;white-space:pre-wrap"></div>
  </div>
  <div class="card"><div class="twrap" style="max-height:60vh"><table id="log-tbl"></table></div></div>
</section>

<!-- ============ ARCHIVE ============ -->
<section id="t-arch" style="display:none">
  <div class="card"><h2>بایگانی محلی (SQLite — آنی، بدون اتصال به دستگاه)</h2>
    <div class="row">
      <div><label>دستگاه</label><select id="a-dev"></select></div>
      <div><label>از تاریخ</label><input id="a-from" type="date"></div>
      <div><label>تا تاریخ</label><input id="a-to" type="date"></div>
      <div><label>جستجوی نام/شناسه</label><input id="a-q" placeholder="مثلا gazmeh یا 731"></div>
      <div style="flex:0"><label>&nbsp;</label><button class="btn p" onclick="fetchArchive()">نمایش</button></div>
      <div style="flex:0"><label>&nbsp;</label><button class="btn" onclick="window.open('/api/archive.csv?'+AQ())">خروجی CSV</button></div>
    </div>
    <div id="a-sum" class="mut" style="margin-top:8px"></div>
  </div>
  <div class="card"><div class="twrap"><table id="arch-tbl"></table></div></div>
</section>

<!-- ============ SYNC ============ -->
<section id="t-sync" style="display:none">
  <div class="card"><h2>همگام‌سازی خودکار</h2>
    <div class="row">
      <div><label>وضعیت</label>
        <select id="sy-en"><option value="1">فعال</option><option value="0">غیرفعال</option></select></div>
      <div><label>بازه (ثانیه)</label><input id="sy-int" value="900" style="width:100px;direction:ltr"></div>
      <div style="flex:0"><label>&nbsp;</label><button class="btn" onclick="saveSyncSettings()">ذخیره تنظیمات</button></div>
      <div style="flex:0"><label>&nbsp;</label><button class="btn p" onclick="syncNow('all')">همگام‌سازی همه</button></div>
    </div>
    <div id="sy-sum" class="mut" style="margin-top:8px"></div>
  </div>
  <div class="card"><h2>وضعیت دستگاه‌ها</h2>
    <div class="twrap"><table id="sy-tbl"></table></div></div>
  <div class="card"><h2>دستگاه‌های push (ADMS — پورت 8081)</h2>
    <div id="adms-sum" class="mut"></div>
    <div class="twrap"><table id="adms-tbl"></table></div>
    <div class="mut" style="margin-top:6px">استعلام از طریق پروتکل PUSH (الگوی zkteco_sync): دستور DATA QUERY در صف قرار می‌گیرد و دستگاه در اولین polling، داده را به سرور push می‌کند.</div>
  </div>
</section>

<!-- ============ CONNECTION LOG ============ -->
<section id="t-conn" style="display:none">
    <div class="card"><h2>لاگ ارتباط و خطاهای دستگاه‌ها</h2>
        <div class="row">
            <div><label>دستگاه</label><select id="c-dev"></select></div>
            <div style="flex:0"><label>&nbsp;</label><button class="btn p" onclick="loadConnectionLogs()">به‌روزرسانی</button></div>
            <div style="flex:0"><label>&nbsp;</label><button class="btn" onclick="clearConnectionTable()">پاک‌کردن نمایش</button></div>
        </div>
        <div id="c-sum" class="mut" style="margin-top:8px"></div>
    </div>
    <div class="card"><div class="twrap" style="max-height:65vh"><table id="conn-tbl"></table></div></div>
</section>

<!-- ============ SCAN ============ -->
<section id="t-scan" style="display:none">
  <div class="card"><h2>اسکن شبکه (پورت 4370)</h2>
    <div class="row" style="align-items:flex-start">
      <div style="flex:2"><label>زیرشبکه‌ها (هر خط یک رنج)</label>
        <textarea id="s-nets"></textarea></div>
      <div style="flex:0;min-width:110px"><label>تایم‌اوت (ثانیه)</label>
        <input id="s-to" value="0.6" style="direction:ltr;width:90px"></div>
      <div style="flex:0"><label>&nbsp;</label>
        <button class="btn p" id="s-btn" onclick="startScan()">شروع اسکن</button></div>
    </div>
    <div class="bar"><i id="s-bar"></i></div>
    <div id="s-prog" class="mut" style="margin-top:6px"></div>
  </div>
  <div class="card"><h2>نتایج</h2>
    <div class="twrap"><table id="scan-tbl"></table></div></div>
</section>

<!-- ============ SETTINGS ============ -->
<section id="t-settings" style="display:none">
  <div class="card">
    <h2>تنظیمات سیستم</h2>
    <div class="mut" style="margin-bottom:6px">مقادیر پس از ذخیره بلافاصله اعمال می‌شوند؛ فقط «تعداد نخ وب‌سرور» پس از ری‌استارت برنامه اثر می‌گذارد.
      فایل تنظیمات: <span id="st-file" class="ltr"></span></div>
    <div class="mut" id="st-env" style="margin-bottom:10px"></div>
    <div class="row"><div style="flex:0"><label>&nbsp;</label>
      <button class="btn p" onclick="saveSettings()">ذخیره همه</button></div>
      <div style="flex:0"><label>&nbsp;</label>
      <button class="btn" onclick="loadSettings()">بازخوانی</button></div></div>
  </div>
  <div id="st-sections"></div>
</section>

</main>
<div id="toasts"></div>

<script>
let DEVS=[], SUBNETS=[], S_TIMER=null, LAST_Q=null;
let ACTION_BUTTON=null;
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function toast(msg,err){const t=document.createElement('div');
 t.className='toast'+(err?' err':'');t.textContent=msg;$('#toasts').appendChild(t);
 setTimeout(()=>t.remove(),err?7000:4000);}
function buttonReady(button){
 if(!button)return;
 clearTimeout(button._releaseTimer);
 button.disabled=false;button.classList.remove('busy');
 button.innerHTML=button._originalHtml||button.innerHTML;
 delete button.dataset.busy;
 delete button._originalHtml;
}
function buttonBusy(button){
 if(!button||button.dataset.busy==='1')return;
 button.dataset.busy='1';button._originalHtml=button.innerHTML;
 button.classList.add('busy');button.disabled=true;
 button.innerHTML='<span class="spinner"></span>در حال پردازش…';
}
document.addEventListener('click',e=>{
 const button=e.target.closest('button.btn');
 if(!button||button.disabled)return;
 buttonBusy(button);button._apiSeen=false;ACTION_BUTTON=button;
 setTimeout(()=>{
    if(ACTION_BUTTON===button)ACTION_BUTTON=null;
    if(!button._apiSeen&&button.dataset.busy==='1')
     button._releaseTimer=setTimeout(()=>buttonReady(button),900);
 },0);
});
async function api(url,opt){
 const button=ACTION_BUTTON;
 if(button)button._apiSeen=true;
 try{
  const r=await fetch(url,opt);
  const j=await r.json().catch(()=>({error:'پاسخ نامعتبر'}));
  if(!r.ok)throw new Error(j.error||r.status);
  if(button){buttonReady(button);toast('عملیات با موفقیت انجام شد');}
  return j;
 }catch(e){
  if(button){buttonReady(button);toast('عملیات ناموفق: '+e.message,true);}
  throw e;
 }}

function tab(id,btn){document.body.classList.remove('nav-open');
 document.querySelectorAll('.tabs button').forEach(b=>b.classList.remove('on'));
 btn.classList.add('on');['dash','devs','users','logs','arch','sync','conn','scan','settings'].forEach(t=>
 $('#t-'+t).style.display=t===id?'':'none');
 if(id==='users'){fillUserDeviceSelect();loadUsers();}
 if(id==='logs')fillDevSelect();
 if(id==='arch')fillArchSelect();
 if(id==='sync')loadSync();
 if(id==='conn'){fillConnectionSelect();loadConnectionLogs();}
 if(id==='settings')loadSettings();}

/* ---------- dashboard ---------- */
async function loadDevices(){try{
 const j=await api('/api/devices');DEVS=j.devices;SUBNETS=j.subnets;
 $('#s-nets').value=SUBNETS.join('\n');renderDash();renderDevs();}catch(e){toast(e.message,1)}}
async function checkAll(){for(const d of DEVS){
 try{await api('/api/devices/'+d.ip+'/ping',{method:'POST'})}catch(e){}}
 loadDevices();toast('بررسی اتصال انجام شد');}
function renderDash(){
 const on=DEVS.filter(d=>d.last_state==='online').length,
       off=DEVS.filter(d=>d.last_state==='offline').length;
 $('#kpis').innerHTML=
  `<div class="kpi"><b>${DEVS.length}</b>دستگاه ثبت‌شده</div>`+
  `<div class="kpi ok"><b>${on}</b>آنلاین</div>`+
  `<div class="kpi bad"><b>${off}</b>آفلاین</div>`;
 let h='<tr><th>وضعیت</th><th>IP</th><th>مدل</th><th>سریال</th><th>آخرین تردد</th><th>آخرین بررسی</th></tr>';
 for(const d of DEVS){const c=d.last_state==='online'?'on':d.last_state==='offline'?'off':'unk';
  h+=`<tr><td><span class="dot ${c}"></span>${d.last_state==='online'?'آنلاین':d.last_state==='offline'?'آفلاین':'نامشخص'}</td>`+
     `<td class="ltr">${esc(d.ip)}</td><td>${esc(d.label||d.info?.model||'—')}</td>`+
     `<td class="ltr">${esc(d.info?.serial||'—')}</td>`+
     `<td>${esc(d.last_log_ts||'—')}</td><td>${esc(d.last_check||'—')}</td></tr>`;}
 $('#dash-tbl').innerHTML=h;}

/* ---------- devices ---------- */
function renderDevs(){
 let h='<tr><th>وضعیت</th><th>IP</th><th>پورت</th><th>برچسب/مدل</th><th>پلتفرم</th><th>سریال</th><th>فریم‌ور</th><th>MAC</th><th>عملیات</th></tr>';
 for(const d of DEVS){const c=d.last_state==='online'?'on':d.last_state==='offline'?'off':'unk';
  h+=`<tr><td><span class="dot ${c}"></span></td><td class="ltr"><b>${esc(d.ip)}</b></td>`+
     `<td class="ltr">${d.port}</td><td>${esc(d.label||d.info?.model||'—')}</td>`+
     `<td class="ltr">${esc(d.info?.platform||'—')}</td>`+
     `<td class="ltr">${esc(d.info?.serial||'—')}</td>`+
     `<td class="ltr">${esc(d.info?.firmware||'—')}</td>`+
     `<td class="ltr">${esc(d.info?.mac||'—')}</td>`+
     `<td><div class="actions">`+
     `<button class="btn" onclick="pingOne('${d.ip}')">اتصال</button>`+
     `<button class="btn" onclick="identifyOne('${d.ip}')">شناسایی</button>`+
     `<button class="btn" onclick="quickLogs('${d.ip}')">ترددها</button>`+
     `<button class="btn" onclick="showUsers('${d.ip}')">کاربران</button>`+
     `<button class="btn" onclick="syncTime('${d.ip}')">همگام‌سازی ساعت</button>`+
     `<button class="btn" onclick="syncTimeCustom('${d.ip}')">تنظیم دلخواه ساعت</button>`+
     `<button class="btn" onclick="reboot('${d.ip}')">ری‌استارت</button>`+
     `<button class="btn d" onclick="delDevice('${d.ip}')">حذف</button>`+
     `</div></td></tr>`;}
 $('#dev-tbl').innerHTML=h;}
async function pingOne(ip){try{const j=await api('/api/devices/'+ip+'/ping',{method:'POST'});
 toast(j.ok?`✅ ${ip} — ${j.latency_ms} ms`:`❌ ${ip} قطع است`,!j.ok);}catch(e){toast(e.message,1)}
 loadDevices();}
async function identifyOne(ip){toast('شناسایی '+ip+' …');
 try{const j=await api('/api/devices/'+ip+'/identify',{method:'POST'});
 toast(`✅ ${ip}: ${j.info.model||''} — سریال ${j.info.serial||'?'}`);}catch(e){toast(e.message,1)}
 loadDevices();}
async function delDevice(ip){if(!confirm('حذف دستگاه '+ip+'؟'))return;
 await api('/api/devices/'+encodeURIComponent(ip),{method:'DELETE'});loadDevices();toast('حذف شد');}
async function addDevice(){const ip=$('#n-ip').value.trim();
 if(!ip){toast('IP را وارد کنید',1);return}
 try{
  await api('/api/devices',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({ip,port:+$('#n-port').value||4370,
    password:+$('#n-pass').value||0,serial:$('#n-serial').value.trim(),
    label:$('#n-label').value.trim(),
    location:$('#n-loc').value.trim()})});
  toast('دستگاه اضافه شد');$('#n-ip').value='';$('#n-serial').value='';loadDevices();identifyOne(ip);}
 catch(e){toast(e.message,1)}}
function nowStr(){const d=new Date();const p=n=>String(n).padStart(2,'0');
 return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+
  p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds());}
async function syncTime(ip){try{const j=await api('/api/devices/'+encodeURIComponent(ip)+'/set_time',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({sync:true})});
  toast(j.queued?
   ('فرمان تنظیم ساعت ('+(j.time_set||'')+') در صف Push قرار گرفت — دستگاه حدود ۱۲ ثانیه دیگر اعمال می‌کند'):
   ('✅ ساعت دستگاه روی زمان سرور تنظیم شد'));
 }catch(e){toast(e.message,1)}}
async function syncTimeCustom(ip){
 const dt=prompt('زمان دلخواه دستگاه (YYYY-MM-DD HH:MM:SS):', nowStr());
 if(dt===null)return;
 const v=dt.trim();
 if(!/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(v)){toast('قالب درست نیست — نمونه: 2026-09-14 08:00:00',1);return}
 try{const j=await api('/api/devices/'+encodeURIComponent(ip)+'/set_time',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({dt:v})});
  toast(j.queued?
   ('فرمان تنظیم ساعت ('+(j.time_set||'')+') در صف Push قرار گرفت — دستگاه حدود ۱۲ ثانیه دیگر اعمال می‌کند'):
   ('✅ ساعت دستگاه روی '+v+' تنظیم شد'));
 }catch(e){toast(e.message,1)}}
async function reboot(ip){if(!confirm('ری‌استارت '+ip+'؟'))return;
 try{await api('/api/devices/'+ip+'/restart',{method:'POST'});toast('دستگاه در حال راه‌اندازی مجدد');}
 catch(e){toast(e.message,1)}}
async function showUsers(ip){try{
 const j=await api('/api/devices/'+encodeURIComponent(ip)+'/users');
 let h='<tr><th>UID</th><th>شناسه</th><th>نام</th><th>نقش</th><th>کارت</th></tr>';
 j.users.forEach(u=>h+=`<tr><td>${u.uid}</td><td class="ltr">${esc(u.user_id)}</td>`+
  `<td>${esc(u.name)}</td><td>${u.privilege===14?'مدیر':'عادی'}</td><td class="ltr">${esc(u.card)}</td></tr>`);
 $('#log-tbl').innerHTML=h;$('#log-sum').textContent=
  `کاربران ${ip}: ${j.users.length} نفر`;
 tab('logs',document.querySelectorAll('.tabs button')[2]);}catch(e){toast(e.message,1)}}

/* ---------- logs ---------- */
function fillDevSelect(){const s=$('#q-dev'),v=s.value;
 s.innerHTML='<option value="all">همه دستگاه‌ها</option>'+
  DEVS.map(d=>`<option value="${d.ip}">${esc(d.ip)} ${esc(d.label||d.info?.model||'')}</option>`).join('');
 if(v)s.value=v;}
function fillUserDeviceSelect(){filterUserDevices();
 const s=$('#u-src'),cv=s.value;
 s.innerHTML='<option value="">— بدون مبدأ (ثبت مستقیم) —</option>'+
  DEVS.map(d=>`<option value="${d.ip}">${esc(d.ip)} ${esc(d.label||d.info?.model||'')}</option>`).join('');
 if(cv)s.value=cv;}
function filterUserDevices(){const s=$('#u-dev'),current=s.value||'all',term=($('#u-filter').value||'').trim().toLowerCase();
 const matches=d=>!term||`${d.ip} ${d.label||''} ${d.info?.model||''}`.toLowerCase().includes(term);
 const options=DEVS.filter(matches).map(d=>`<option value="${d.ip}">${esc(d.ip)} ${esc(d.label||d.info?.model||'')}</option>`).join('');
 s.innerHTML='<option value="all">همه دستگاه‌ها</option>'+options;
 s.value=(current==='all'||DEVS.some(d=>d.ip===current&&matches(d)))?current:'all';
 loadUsers();}
async function loadUsers(){try{
 const j=await api('/api/users?device='+encodeURIComponent($('#u-dev').value||'all'));
 $('#u-sum').textContent=`${j.users.length} کاربر در سامانه`;
 let h='<tr><th>دستگاه</th><th>شناسه</th><th>نام</th><th>کارت</th><th>آخرین تغییر</th><th>عملیات</th></tr>';
 for(const u of j.users)h+=`<tr><td class="ltr">${esc(u.device)}</td>`+
  `<td class="ltr">${esc(u.user_id)}</td><td>${esc(u.name)}</td>`+
  `<td class="ltr">${esc(u.card||'')}</td><td class="ltr">${esc(u.updated||'')}</td>`+
  `<td><button class="btn p" onclick="startEnroll('${esc(u.device)}','${esc(u.user_id)}','${esc(u.name)}')">شروع ثبت اثر انگشت</button>`+
  ` <button class="btn" onclick="checkEnroll('${esc(u.device)}','${esc(u.user_id)}')">وضعیت ثبت</button></td></tr>`;
 $('#users-tbl').innerHTML=h||'<tr><td class="mut">کاربری ثبت نشده است</td></tr>';
 }catch(e){toast(e.message,1)}}
async function createUser(){const ip=$('#u-dev').value,id=$('#u-id').value.trim(),name=$('#u-name').value.trim();
 if(ip==='all'){toast('ابتدا یک دستگاه مشخص انتخاب کنید',1);return}
 if(!id||!name){toast('شناسه و نام کاربر الزامی است',1);return}
 try{await api('/api/devices/'+encodeURIComponent(ip)+'/users',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({user_id:id,uid:+$('#u-uid').value||+id,name,card:+$('#u-card').value||0})});
  toast('کاربر روی دستگاه ساخته شد؛ اکنون ثبت اثر انگشت را شروع کنید');loadUsers();
 }catch(e){toast(e.message,1)}}
async function startEnroll(ip,id,name){
 const src=($('#u-src')?.value||'').trim();
 if(!src&&!confirm(`کاربر ${name} آماده ثبت اثر انگشت است؟`))return;
 if(src&&!confirm(`قالب اثر انگشت ${name} از ${src} به ${ip} کپی شود؟\n(اثر انگشت باید از قبل روی دستگاه مبدأ ثبت شده باشد)`))return;
 try{await api('/api/devices/'+encodeURIComponent(ip)+'/enroll',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({user_id:id,source:src||undefined,name:name})});
  toast(src?'کپی اثر انگشت آغاز شد':'دستگاه آماده ثبت است؛ انگشت را روی حسگر بگذارید');pollEnroll(ip,id);
 }catch(e){toast(e.message,1)}}
async function pollEnroll(ip,id){
 const timer=setInterval(async()=>{try{const j=await fetch('/api/devices/'+encodeURIComponent(ip)+'/enroll').then(r=>r.json()),e=j.enrollment;
  if(!e||e.user_id!==id)return;
  $('#u-sum').textContent=(e.stage?('['+e.stage+'] '):'')+(e.message||'');
  if(!e.running){clearInterval(timer);toast(e.message||'ثبت اثر انگشت پایان یافت',e.ok===false);loadUsers();}
 }catch(e){clearInterval(timer);toast(e.message,1)}},UI_POLL.enroll_ms);
}
async function checkEnroll(ip,id){try{const j=await api('/api/devices/'+encodeURIComponent(ip)+'/enroll');
 const e=j.enrollment;toast(e&&e.user_id===id?(e.message||e.stage):'ثبت فعالی برای این کاربر نیست',e&&e.ok===false);
 }catch(e){toast(e.message,1)}}
async function quickLogs(ip){tab('logs',document.querySelectorAll('.tabs button')[2]);
 $('#q-dev').value=ip;fetchLogs();}
let P_TIMER=null,P_ACTIVE=false;
/* Progress polling with a dynamic interval: 1 s while any device stage is
   actively running (connect/attlog/…), 5 s once everything is idle/done. */
function progDelay(){const p=window._PROG_LAST||{};
 for(const k in p){const s=p[k];if(s.stage&&s.stage!=='done'&&s.stage!=='error')return UI_POLL.progress_active_ms}
 return UI_POLL.progress_idle_ms;}
async function progTick(){try{await pollProg()}catch(e){}
 if(!P_ACTIVE)return;P_TIMER=setTimeout(progTick,progDelay());}
function startProgPolling(){P_ACTIVE=true;if(!P_TIMER)progTick();}
function stopProgPolling(){P_ACTIVE=false;
 if(P_TIMER){clearTimeout(P_TIMER);P_TIMER=null;}}
async function fetchLogs(){const q=Q();LAST_Q=q;$('#log-sum').textContent='در حال دریافت…';
 $('#fetch-prog').textContent='';
 stopProgPolling();startProgPolling();
 try{const j=await api('/api/logs?'+q);renderLogTable(j);}
 catch(e){$('#log-sum').textContent='';toast(e.message,1)}
 finally{stopProgPolling();setTimeout(pollProg,300);}}
async function fetchArchivedLogs(){
 const q=AQForLogs();LAST_Q=q;$('#log-sum').textContent='در حال خواندن بایگانی…';
 try{const j=await api('/api/archive?'+q);renderLogTable(j);
  $('#log-sum').textContent=`${j.records.length} رکورد از بایگانی محلی (بدون اتصال به دستگاه)`;
 }catch(e){$('#log-sum').textContent='';toast(e.message,1)}
}
async function fetchNewLogs(){
 const q=Q();LAST_Q=q;$('#log-sum').textContent='در حال دریافت زنده و مقایسه با دیتابیس…';
 $('#fetch-prog').textContent='دریافت کامل دستگاه ممکن است چند دقیقه طول بکشد؛ بعد فقط رکوردهای جدید نمایش داده می‌شود.';
 stopProgPolling();startProgPolling();
 try{const j=await api('/api/logs/new?'+q);renderLogTable(j);
    if(j.pending){
    $('#log-sum').textContent='درخواست دریافت جدید ثبت شد؛ منتظر polling دستگاه';
     setTimeout(fetchArchivedLogs,7000);
    }else $('#log-sum').textContent=`${j.total} رکورد جدید که در دیتابیس موجود نیست`+
     (Object.keys(j.errors||{}).length?` — خطا: ${Object.entries(j.errors).map(([k,v])=>k+': '+v).join(' | ')}`:'');
 }catch(e){$('#log-sum').textContent='';toast(e.message,1)}
 finally{stopProgPolling();setTimeout(pollProg,300);}
}
function AQForLogs(){const p=new URLSearchParams({device:$('#q-dev').value||'all'});
 if($('#q-from').value)p.set('from',$('#q-from').value);
 if($('#q-to').value)p.set('to',$('#q-to').value);
 p.set('limit','20000');return p.toString();}
async function pollProg(){try{
 const j=await fetch('/api/fetch-progress').then(r=>r.json());
 window._PROG_LAST=j.progress||{};
 const p=j.progress||{},selected=$('#q-dev')?.value||'all';let lines=[];
 for(const ip in p){const s=p[ip];
    if(selected!=='all'&&ip!==selected)continue;
  const ok=s.ok===true?'✔':(s.ok===false?'✘':'…');
  lines.push(`${ok} ${ip} — ${s.stage} ${s.note||''} (${s.updated||''})`);}
 $('#fetch-prog').textContent=lines.join('\n');}catch(e){}}
function Q(){const p=new URLSearchParams({device:$('#q-dev').value||'all'});
 if($('#q-from').value)p.set('from',$('#q-from').value);
 if($('#q-to').value)p.set('to',$('#q-to').value);
 const timeout=$('#q-timeout').value.trim();
 if(timeout)p.set('timeout',timeout);
 return p.toString();}
function fillConnectionSelect(){const s=$('#c-dev'),v=s.value;
 s.innerHTML='<option value="all">همه دستگاه‌ها</option>'+
  DEVS.map(d=>`<option value="${d.ip}">${esc(d.ip)} ${esc(d.label||d.info?.model||'')}</option>`).join('');
 if(v)s.value=v;}
function clearConnectionTable(){$('#conn-tbl').innerHTML='';$('#c-sum').textContent='';}
async function loadConnectionLogs(){try{
 const j=await api('/api/connection-logs?device='+encodeURIComponent($('#c-dev').value||'all')+'&limit=2000');
 $('#c-sum').textContent=`${j.logs.length} رویداد — نگهداری حداکثر ۲۰۰۰ رویداد اخیر`;
 let h='<tr><th>زمان</th><th>دستگاه</th><th>رویداد</th><th>جزئیات</th><th>نتیجه</th><th>منبع</th></tr>';
 for(const r of j.logs){const result=r.ok===true?'موفق':r.ok===false?'خطا':'—';
  h+=`<tr><td class="ltr">${esc(r.time)}</td><td class="ltr">${esc(r.device)}</td>`+
     `<td>${esc(r.event)}</td><td>${esc(r.detail)}</td><td>${result}</td>`+
     `<td>${esc(r.source)}</td></tr>`;}
 $('#conn-tbl').innerHTML=h||'<tr><td class="mut">لاگی ثبت نشده است</td></tr>';
 }catch(e){toast(e.message,1)}}
async function stopSync(){try{
 const j=await api('/api/sync/stop',{method:'POST'});
 toast(j.message||'درخواست توقف ثبت شد');loadSync();
 }catch(e){toast(e.message,1)}}
function renderLogTable(j){
 $('#log-sum').textContent=`${j.total} رکورد تردد`+
  (Object.keys(j.errors||{}).length?` — خطا: ${Object.entries(j.errors).map(([k,v])=>k+': '+v).join(' | ')}`:'');
 let h='<tr><th>دستگاه</th><th>IP</th><th>شناسه</th><th>نام</th><th>زمان</th><th>نوع</th><th>وضعیت</th></tr>';
 for(const r of j.records)
  h+=`<tr><td>${esc(r.label||'')}</td><td class="ltr">${esc(r.device)}</td>`+
     `<td class="ltr">${esc(r.user_id)}</td><td>${esc(r.name||'')}</td>`+
     `<td class="ltr">${esc(r.timestamp)}</td><td>${esc(r.punch_label)}</td><td>${r.status}</td></tr>`;
 $('#log-tbl').innerHTML=h||'<tr><td class="mut">رکوردی نیست</td></tr>';}
function exportData(kind){if(!LAST_Q){toast('اول استعلام بگیرید',1);return;}
 window.open('/api/export.'+kind+'?'+LAST_Q);toast('خروجی در حال دانلود است');}

/* ---------- archive ---------- */
function fillArchSelect(){const s=$('#a-dev'),v=s.value;
 s.innerHTML='<option value="all">همه دستگاه‌ها</option>'+
  DEVS.map(d=>`<option value="${d.ip}">${esc(d.ip)} ${esc(d.label||d.info?.model||'')}</option>`).join('');
 if(v)s.value=v;}
function AQ(){const p=new URLSearchParams({device:$('#a-dev').value||'all'});
 if($('#a-from').value)p.set('from',$('#a-from').value);
 if($('#a-to').value)p.set('to',$('#a-to').value);
 if($('#a-q').value.trim())p.set('q',$('#a-q').value.trim());
 p.set('limit','20000');return p.toString();}
async function fetchArchive(){try{$('#a-sum').textContent='در حال جستجو…';
 const j=await api('/api/archive?'+AQ());
 $('#a-sum').textContent=`${j.records.length} رکورد نمایش — کل بایگانی: ${j.total}`;
 let h='<tr><th>دستگاه</th><th>IP</th><th>شناسه</th><th>نام</th><th>زمان</th><th>نوع</th><th>منبع</th></tr>';
 for(const r of j.records)
  h+=`<tr><td>${esc(r.label||'')}</td><td class="ltr">${esc(r.device)}</td>`+
     `<td class="ltr">${esc(r.user_id)}</td><td>${esc(r.name||'')}</td>`+
     `<td class="ltr">${esc(r.timestamp)}</td><td>${esc(r.punch_label)}</td>`+
     `<td><span class="tag">${esc(r.source)}</span></td></tr>`;
 $('#arch-tbl').innerHTML=h||'<tr><td class="mut">بایگانی خالی است — از تب همگام‌سازی شروع کنید</td></tr>';
 }catch(e){toast(e.message,1)}}

/* ---------- sync & adms ---------- */
async function syncNow(ip){try{
 await api('/api/sync',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({device:ip})});toast('همگام‌سازی شروع شد');
 setTimeout(loadSync,1500);}catch(e){toast(e.message,1)}}
async function saveSyncSettings(){try{
 await api('/api/sync/settings',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({enabled:$('#sy-en').value==='1',interval:+$('#sy-int').value||900})});
 toast('تنظیمات ذخیره شد');loadSync();}catch(e){toast(e.message,1)}}
async function approveSn(sn){try{
 await api('/api/adms/approve',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({sn})});toast('دستگاه '+sn+' تأیید شد');loadSync();}catch(e){toast(e.message,1)}}
async function admsQuery(sn,table){try{
 const j=await api('/api/adms/query',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({device:admsDevice(sn),table})});
 toast('دستور #'+j.cmd_id+' در صف قرار گرفت — منتظر polling دستگاه…');loadSync();}catch(e){toast(e.message,1)}}
function admsDevice(sn){
 const d=DEVS.find(item=>(item.info?.serial||'').trim()===sn);
 return d?.ip||sn;
}
async function loadSync(){try{
 const j=await api('/api/sync');
 $('#sy-en').value=j.auto?.enabled?'1':'0';$('#sy-int').value=j.auto?.interval||900;
 $('#sy-sum').textContent=(j.running?'همگام‌سازی در حال اجراست…':'آخرین اجرا: '+(j.last_run||'—'))+
  ' — کل بایگانی: '+j.db_total+' رکورد';
 let h='<tr><th>دستگاه</th><th>آخرین همگام‌سازی</th><th>رکورد جدید</th><th>خطا</th><th>عملیات</th></tr>';
 const res=j.results||{};
 const st={};(j.devices||[]).forEach(d=>st[d.device]=d);
 for(const ip in res){const r=res[ip];
  h+=`<tr><td class="ltr">${esc(ip)}</td><td>${esc(st[ip]?.last_sync||'—')}</td>`+
     `<td>${r.new??0}</td><td class="mut">${esc((r.error||'').slice(0,80)||'—')}</td>`+
     `<td><button class="btn" onclick="syncNow('${esc(ip)}')">همگام‌سازی</button></td></tr>`;}
 for(const d of (j.devices||[])){if(st[d.device]&&res[d.device])continue;
  h+=`<tr><td class="ltr">${esc(d.device)}</td><td>${esc(d.last_sync||'—')}</td>`+
     `<td>${d.last_count??0}</td><td class="mut">${esc((d.last_error||'').slice(0,80)||'—')}</td>`+
     `<td><button class="btn" onclick="syncNow('${esc(d.device)}')">همگام‌سازی</button></td></tr>`;}
 $('#sy-tbl').innerHTML=h||'<tr><td class="mut">دستگاهی ثبت نشده</td></tr>';
 const a=await api('/api/adms');
 $('#adms-sum').textContent=`روشن روی پورت ${a.port} — رویدادهای دریافتی: ${a.events} — آخرین: ${a.last||'—'}`;
 let ah='<tr><th>Serial</th><th>IP</th><th>دیده‌شده</th><th>استعلام کاربران</th><th>استعلام تردد</th><th>عملیات</th></tr>';
 for(const p of (a.pending||[]))
  ah+=`<tr><td class="ltr">${esc(p.sn)}</td><td class="ltr">${esc(p.ip)}</td><td>${esc(p.seen)}</td>`+
     `<td colspan="2" class="mut">در انتظار تأیید</td>`+
     `<td><button class="btn p" onclick="approveSn('${esc(p.sn)}')">تأیید</button></td></tr>`;
 for(const sn of (a.approved||[]))
  ah+=`<tr><td class="ltr">${esc(sn)}</td><td colspan="2" class="mut">تأییدشده</td>`+
     `<td><button class="btn" onclick="admsQuery('${esc(sn)}','user')">استعلام</button></td>`+
     `<td><button class="btn" onclick="admsQuery('${esc(sn)}','attlog')">استعلام</button></td>`+
     `<td class="mut">${((a.queues||{})[sn]||[]).map(c=>'#'+c.id).join(' ')||'—'}</td></tr>`;
 if(!(a.pending||[]).length&&!(a.approved||[]).length)
  ah+='<tr><td colspan="6" class="mut">دستگاهی روی پورت push دیده نشده — آدرس سرور را در دستگاه تنظیم کنید</td></tr>';
 $('#adms-tbl').innerHTML=ah;
 const cl=a.cmd_log||[];
 if(cl.length){let ch='<div class="mut" style="margin-top:8px">آخرین دستورات:</div><table>';
  for(const c of cl.slice(0,6))
   ch+=`<tr><td class="ltr">#${c.id} ${esc(c.sn)}</td><td class="ltr">${esc(c.cmd.slice(0,50))}</td>`+
      `<td>${esc(String(c.return))}</td><td class="mut">${esc(c.note||'')}</td></tr>`;
  ch+='</table>';
  $('#adms-tbl').closest('div.card').insertAdjacentHTML('beforeend',ch);}
 }catch(e){toast(e.message,1)}}

/* ---------- scan ---------- */
async function startScan(){
 const nets=$('#s-nets').value.split('\n').map(s=>s.trim()).filter(Boolean);
 try{await api('/api/scan',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({subnets:nets,timeout:+$('#s-to').value||0.6,deep:true})});
 if(!S_TIMER)S_TIMER=setInterval(pollScan,UI_POLL.scan_ms);toast('اسکن شروع شد');}
 catch(e){toast(e.message,1)}}
async function pollScan(){const j=await fetch('/api/scan').then(r=>r.json());
 $('#s-prog').textContent=j.progress||'';
 $('#s-bar').style.width=j.total?((j.done/j.total)*100)+'%':'0';
 if(!j.running&&j.found){clearInterval(S_TIMER);S_TIMER=null;renderScan(j.found);
  $('#s-btn').disabled=false;}
 else if(j.running)$('#s-btn').disabled=true;}
function renderScan(found){
 let h='<tr><th>IP</th><th>پینگ</th><th>مدل</th><th>سریال</th><th>پلتفرم</th><th>فریم‌ور</th><th>عملیات</th></tr>';
 if(!found.length){h+='<tr><td colspan="7" class="mut">دستگاهی یافت نشد</td></tr>';}
 for(const f of found)
  h+=`<tr><td class="ltr"><b>${esc(f.ip)}</b>${f.known?' <span class="tag">ثبت‌شده</span>':''}</td>`+
     `<td class="ltr">${f.latency_ms} ms</td><td>${esc(f.model||'—')}</td>`+
     `<td class="ltr">${esc(f.serial||'—')}</td><td class="ltr">${esc(f.platform||'—')}</td>`+
     `<td class="ltr">${esc(f.firmware||'—')}</td><td>`+
     (f.known?'':`<button class="btn p" onclick="adopt('${f.ip}','${esc(f.model)}','${esc(f.serial)}','${esc(f.platform)}','${esc(f.firmware)}')">افزودن</button>`)+
     `</td></tr>`;
 $('#scan-tbl').innerHTML=h;}
async function adopt(ip,model,serial,platform,firmware){try{
 await api('/api/scan/adopt',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({ip,model,serial,platform,firmware})});
 toast(ip+' اضافه شد');pollScan();}catch(e){toast(e.message,1)}}

/* ---------- settings (تنظیمات) ---------- */
const ST_SECTIONS=[
 {key:'cache_ttl',title:'مدت کش (ثانیه)',desc:'مدت نگهداری پاسخ APIها در حافظه؛ عدد کمتر=داده تازه‌تر، بار بیشتر روی دستگاه‌ها.',
  fields:[['devices','لیست دستگاه‌ها (/api/devices)'],['scan','نتایج اسکن شبکه'],['connection_logs','لاگ ارتباط'],['archive','بایگانی ترددها'],['sync','وضعیت همگام‌سازی']]},
 {key:'lock_timeout',title:'تایم‌اوت قفل دستگاه (ثانیه)',desc:'حداکثر انتظار برای آزاد شدن قفل دستگاه قبل از خطای «دستگاه مشغول».',
  fields:[['default','پول کامل ZK (سبزلیبل)'],['fk','دستگاه FK (پورت 5005)'],['set_time','عملیات سریع (ساعت/کاربر)']]},
 {key:'sync',title:'همگام‌سازی خودکار',desc:'فاصلهٔ بین پاس‌های همگام‌سازی خودکار؛ بلافاصله در چرخهٔ بعدی اعمال می‌شود.',
  fields:[['auto_interval','فاصلهٔ همگام‌سازی خودکار (ثانیه، حداقل ۶۰)'],['lock_queue_timeout','صف انتظار صف همگام‌سازی (ثانیه)']]},
 {key:'backup',title:'پشتیبان‌گیری خودکار',desc:'هر چند ساعت یک کپی ساده از attendance.db گرفته می‌شود.',
  fields:[['interval_hours','فاصلهٔ بکاپ (ساعت)'],['keep','تعداد بکاپ نگه‌داشتی']],
  info:()=>('محل ذخیره: '+(window._ST&&window._ST.backup_dir||''))},
 {key:'log',title:'چرخش لاگ',desc:'اندازهٔ هر فایل attendance.log و تعداد نسخه‌های نگه‌داشتی.',
  fields:[['max_bytes','حداکثر حجم هر فایل لاگ (بایت)'],['backups','تعداد فایل‌های لاگ قدیمی']]},
 {key:'web',title:'وب‌سرور',desc:'تغییر تعداد نخ‌ها پس از ری‌استارت برنامه اعمال می‌شود.',
  fields:[['threads','تعداد نخ وب‌سرور']]},
 {key:'ui_polling',title:'فاصلهٔ به‌روزرسانی خودکار صفحه (میلی‌ثانیه)',desc:'نرخ polling مرورگر؛ اعداد بزرگ‌تر یعنی ترافیک کمتر.',
  fields:[['devices_ms','لیست دستگاه‌ها'],['sync','وضعیت همگام‌سازی'],['scan','نتیجهٔ اسکن'],['enroll_ms','ثبت اثر انگشت'],['progress_active_ms','پیشرفت دریافت (فعال)'],['progress_idle_ms','پیشرفت دریافت (بیکار)']]}
];
window._ST=null;
async function loadSettings(){try{
 const j=await api('/api/settings');window._ST=j;
 $('#st-file').textContent=j.settings_file||'';
 $('#st-env').textContent=(j.env_overrides&&j.env_overrides.length)
   ?('متغیر محیطی فعال (اولویت بالاتر از این فرم): '+j.env_overrides.join('، ')):'';
 renderSettings(j.settings,j.defaults||{});
}catch(e){toast(e.message,1)}}
function renderSettings(s,defs){const box=$('#st-sections');box.innerHTML='';
 for(const sec of ST_SECTIONS){
  const card=document.createElement('div');card.className='card';
  let html=`<h2>${sec.title}</h2><div class="mut">${sec.desc}</div><div class="row" style="margin-top:8px">`;
  for(const[f,label]of sec.fields){
   const v=(s[sec.key]||{})[f];const lo=window._ST_RANGES[sec.key+'.'+f];
   html+=`<div style="flex:0;min-width:170px"><label>${label}</label>`+
     `<input class="st-in" data-k="${sec.key}.${f}" value="${v??''}"`+
     (lo?` min="${lo[0]}" max="${lo[1]}"`:``)+` style="direction:ltr"></div>`;}
  if(sec.info)html+=`<div style="flex:1"><label>&nbsp;</label><div class="mut">${sec.info()}</div></div>`;
  html+=`<div style="flex:1"></div><div style="flex:0"><label>&nbsp;</label>`+
   `<button class="btn" onclick="resetSection('${sec.key}')">بازگشت به پیش‌فرض</button></div>`;
  card.innerHTML=html+'</div>';box.appendChild(card);}}
window._ST_RANGES={
 'cache_ttl.devices':[0,600],'cache_ttl.scan':[0,600],'cache_ttl.connection_logs':[0,600],
 'cache_ttl.archive':[0,600],'cache_ttl.sync':[0,600],
 'lock_timeout.default':[3,600],'lock_timeout.fk':[1,120],'lock_timeout.set_time':[1,120],
 'sync.auto_interval':[60,86400],'sync.lock_queue_timeout':[5,600],
 'backup.interval_hours':[1,168],'backup.keep':[1,100],
 'log.max_bytes':[100000,100000000],'log.backups':[0,20],'web.threads':[1,64],
 'ui_polling.devices_ms':[2000,600000],'ui_polling.sync_ms':[2000,600000],
 'ui_polling.scan_ms':[1000,600000],'ui_polling.enroll_ms':[500,60000],
 'ui_polling.progress_active_ms':[500,60000],'ui_polling.progress_idle_ms':[2000,600000]};
function readSettingsForm(){const out={};
 document.querySelectorAll('.st-in').forEach(inp=>{
  const parts=inp.dataset.k.split('.');let node=out;
  for(let i=0;i<parts.length-1;i++){node[parts[i]]=node[parts[i]]||{};node=node[parts[i]];}
  const v=parseFloat(inp.value);
  node[parts[parts.length-1]]=isNaN(v)?inp.value:v;});
 return out;}
async function saveSettings(){try{
 const body=readSettingsForm();
 const j=await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(body)});
 toast('تنظیمات ذخیره شد و بلافاصله اعمال شد');window._ST=Object.assign(window._ST||{},{settings:j.settings});
 loadPollConfig();renderSettings(j.settings,j.settings);
}catch(e){toast(e.message,1)}}
async function resetSection(key){try{
 const d=(window._ST&&window._ST.defaults&&window._ST.defaults[key])||{};
 const j=await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({[key]:d})});
 toast('بخش به پیش‌فرض برگشت');window._ST.settings=j.settings;renderSettings(j.settings,j.defaults||{});
}catch(e){toast(e.message,1)}}

/* ---------- init ---------- */
setInterval(()=>{$('#clock').textContent=new Date().toLocaleString('fa-IR')},1000);
$('#q-from').value=new Date(Date.now()-6*864e5).toISOString().slice(0,10);
$('#q-to').value=new Date().toISOString().slice(0,10);
$('#a-from').value='';$('#a-to').value='';
let UI_POLL={devices_ms:8000,sync_ms:5000,scan_ms:2000,enroll_ms:1200,
 progress_active_ms:1000,progress_idle_ms:5000};
function _markTwraps(){document.querySelectorAll('.twrap').forEach(w=>{
 if(w.scrollWidth>w.clientWidth+4)w.classList.add('has-hscroll');
 else w.classList.remove('has-hscroll');});}
setInterval(_markTwraps,1500);window.addEventListener('resize',_markTwraps);
function loadPollConfig(){try{const s=(window._ST&&window._ST.settings)||{};
 if(s.ui_polling)Object.assign(UI_POLL,s.ui_polling);}catch(e){}}
fetch('/api/settings').then(r=>r.json()).then(j=>{window._ST=j;loadPollConfig();
 if($('#t-settings').style.display!=='none')loadSettings();}).catch(()=>{});
loadDevices().then(()=>pollScan());
/* Background refresh: pause entirely while the tab is hidden (saves device
   pings and battery); rates come from /api/settings (ui_polling). */
(function schedDevices(){setTimeout(async()=>{if(!document.hidden)await loadDevices();
   schedDevices()},UI_POLL.devices_ms)})();
(function schedSync(){setTimeout(async()=>{if(!document.hidden)await loadSync();
   schedSync()},UI_POLL.sync_ms)})();
/* Deep-link: #settings (یا نام هر تب) همان تب را هنگام باز شدن صفحه فعال می‌کند */
(function(){const h=location.hash.replace('#','');if(h){
 const b=document.querySelector('.tabs button[data-t="'+h+'"]');if(b)tab(h,b);}})();
</script>
</body>
</html>
"""

# ----------------------------------------------------------------------------
# Production runtime: waitress WSGI server (multi-threaded, stable on
# Windows), rotating file logging, periodic DB backup, graceful shutdown.
# ----------------------------------------------------------------------------
LOG_FILE = BASE_DIR / "attendance.log"
WEB_THREADS = int(cfg("web.threads", _env_num("WEB_THREADS", 16)))


def _setup_logging():
    """werkzeug/request logs + app stdout into a size-capped rotating file."""
    handler = RotatingFileHandler(
        LOG_FILE, maxBytes=cfg("log.max_bytes", 2_000_000),
        backupCount=cfg("log.backups", 3), encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logging.getLogger("werkzeug").addHandler(handler)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def _backup_db_now() -> str:
    """Plain file copy of attendance.db (WAL-safe: copy -wal/-shm too)."""
    bdir = DATA_DIR / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = bdir / f"attendance_{stamp}.db"
    shutil.copy2(DB_FILE, dest)
    for suffix in ("-wal", "-shm"):
        side = Path(str(DB_FILE) + suffix)
        if side.exists():
            shutil.copy2(side, Path(str(dest) + suffix))
    # retention: newest backup.keep files win
    backups = sorted(bdir.glob("attendance_*.db"), reverse=True)
    for old in backups[cfg("backup.keep", 10):]:
        for p in (old, Path(str(old) + "-wal"), Path(str(old) + "-shm")):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
    return str(dest)


def _backup_loop():
    while True:
        time.sleep(cfg("backup.interval_hours", 6) * 3600)
        try:
            dest = _backup_db_now()
            _connection_log("server", "db-backup", dest, ok=True, source="system")
        except Exception as e:
            _connection_log("server", "db-backup", f"{type(e).__name__}: {e}",
                            ok=False, source="system")


def _graceful_shutdown(signum, frame):
    """Stop background workers so device locks and DB handles release.
    Daemon threads die with the process; this just avoids cutting a device
    session or a SQLite write mid-flight."""
    try:
        SYNC_STATE["stop"] = True
        SYNC_STATE["cancel_requested"] = True
        _SYNC_QUEUE.put([])         # unblock the worker, it exits via stop
    except Exception:
        pass
    # Second signal = user really wants out, right now.
    signal.signal(signal.SIGINT if signum == signal.SIGINT else signal.SIGTERM,
                  signal.SIG_DFL)


if __name__ == "__main__":
    db_init()
    _setup_logging()
    start_background_workers()
    threading.Thread(target=_backup_loop, daemon=True,
                     name="db-backup").start()
    signal.signal(signal.SIGINT, _graceful_shutdown)
    signal.signal(signal.SIGTERM, _graceful_shutdown)
    atexit.register(lambda: _backup_db_now() if DB_FILE.exists() else None)
    print("* Attendance manager  ->  http://0.0.0.0:%d" % PORT_WEB)
    print("* SQLite archive      ->  %s" % DB_FILE)
    print("* ADMS push listener  ->  port %d (/iclock)" % ADMS_PORT)
    print("* devices registry    ->  %s" % DEVICES_FILE)
    print("* server              ->  waitress, %d threads" % WEB_THREADS)
    from waitress import serve
    serve(app, host=HOST, port=PORT_WEB, threads=WEB_THREADS,
          connection_limit=100, channel_timeout=120,
          # long device pulls stream slowly — give generous window sizes
          recv_bytes=65536, send_bytes=65536)
