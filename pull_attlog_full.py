# -*- coding: utf-8 -*-
"""Full ATTLOG stream downloader for ZKTeco 'green label' WiFi units.

Firmware behaviour (measured on AK3750WIFI_TFT / WL50):
  * CMD_GET_FREE_SIZES returns an empty packet -> pyzk short-circuits.
  * After 1503 the device answers with the total size and then serves
    the stream as a run of 4096-byte CMD_DATA frames that coalesce in
    the TCP socket. Reading them requires an aggressive recv + a frame
    walker (pyzk's per-frame logic only ever takes the first one).
  * A 1503 session that is abandoned (no CMD_FREE_DATA) blocks all
    further 1503s until the device reboots.

Usage: python pull_attlog_full.py <device_ip> <out.bin>
"""
import socket
import sys
import time
from struct import pack, unpack

from zk import ZK, const

CMD_DATA = const.CMD_DATA                  # 1501
CMD_PREPARE_DATA = const.CMD_PREPARE_DATA  # 1500
CHUNK = 0xFFc0                             # mirror pyzk's request size


def make_ack(c):
    """Build a protocol-valid CMD_ACK_OK TCP frame (mirrors pyzk)."""
    head = pack("<4H", const.CMD_ACK_OK, 0, c._ZK__session_id,
                const.USHRT_MAX - 1)
    n = len(head)
    chk = 0
    p = head
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


def recv_until_quiet(sock, first_timeout=8.0, quiet=2.0, cap=2_000_000):
    """Recv aggressively; stop when the socket goes quiet or cap reached."""
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


def walk_frames(buf):
    """Yield (cmd, payload) for every complete TCP frame in buf."""
    out = []
    pos = 0
    while len(buf) - pos >= 16:
        m1, m2, flen = unpack("<HHI", buf[pos:pos + 8])
        if m1 != 0x5050 or m2 != 0x7473 or flen < 8:
            break
        if len(buf) - pos < 8 + flen:
            break
        inner = buf[pos + 8: pos + 8 + flen]
        cmd = unpack("<H", inner[:2])[0]
        # inner = [cmd:2][chk:2][session:2][reply:2] + payload
        out.append((cmd, inner[8:]))
        pos += 8 + flen
    return out, buf[pos:]


def collect(c, want_bytes):
    """Collect want_bytes of stream via 1504 pulls.

    Each 1504 is answered by a run of 4096-byte CMD_DATA frames that
    coalesce on the socket. pyzk's __send_command consumes only the head
    of the first frame (its 1024-byte recv window) — so we must COMPLETE
    that inline frame first, then drain the rest and walk them.
    """
    sock = c._ZK__sock
    stream = b""
    idle = 0
    while len(stream) < want_bytes and idle < 6:
        off = len(stream)
        want = min(CHUNK, want_bytes - off)
        try:
            r = c._ZK__send_command(1504, pack("<ii", off, want), 1024)
        except Exception as e:
            print(f"  1504 error: {e}")
            idle += 1
            time.sleep(2)
            continue
        if not r.get("status"):
            print(f"  1504 refused at {off}")
            idle += 1
            time.sleep(1.5)
            continue
        got = 0
        if r.get("code") == CMD_DATA:
            # complete the inline frame
            have = len(c._ZK__data_recv)
            target = c._ZK__tcp_length
            if have < target:
                c._ZK__data_recv += c._ZK__recieve_raw_data(target - have)
            chunk = c._ZK__data_recv[8:target]
            stream += chunk[:want_bytes - len(stream)]
            got += len(chunk)
            try:
                sock.send(make_ack(c))
            except Exception:
                pass
        # drain any further whole frames pushed behind it
        buf = recv_until_quiet(sock, first_timeout=3.0, quiet=1.2)
        frames, _rest = walk_frames(buf)
        for cmd, payload in frames:
            if cmd == CMD_DATA and payload:
                if len(stream) < want_bytes:
                    stream += payload[:want_bytes - len(stream)]
                got += len(payload)
                try:
                    sock.send(make_ack(c))
                except Exception:
                    pass
        if got == 0:
            idle += 1
            print(f"  empty round (idle {idle}, {len(stream)}/{want_bytes})")
        else:
            idle = 0
            print(f"  +{got} -> {len(stream)}/{want_bytes}")
    return stream


def pull(c, cmd, fct=0):
    # preamble identical to the flow that succeeded repeatedly
    c.free_data()
    time.sleep(1.0)
    c.read_sizes()
    time.sleep(1.0)

    resp = c._ZK__send_command(1503, pack("<bhii", 1, cmd, fct, 0), 1024)
    if not resp.get("status"):
        raise RuntimeError(f"1503 refused: code={resp.get('code')}")

    # The size arrives either in this ACK payload or as a leading u32 of
    # a pushed DATA frame — handle both.
    total = None
    stream = b""
    # data_recv holds the inner packet: [hdr:8][payload...]; tcp_length
    # counts the whole inner packet.
    payload_ack = c._ZK__data_recv[8:c._ZK__tcp_length]
    if resp.get("code") == CMD_PREPARE_DATA and len(payload_ack) >= 5:
        total = unpack("<I", payload_ack[1:5])[0]
    elif resp.get("code") == const.CMD_ACK_OK and len(payload_ack) >= 5:
        total = unpack("<I", payload_ack[1:5])[0]

    # brief peek: some sessions push the first DATA frame right away
    buf = recv_until_quiet(c._ZK__sock, first_timeout=1.2, quiet=0.8)
    frames, _ = walk_frames(buf)
    for fcmd, payload in frames:
        if fcmd == CMD_DATA and payload:
            stream += payload
            if total is None and len(payload) >= 4:
                total = unpack("<I", payload[:4])[0]
    if stream:
        print(f"  pushed frame: {len(stream)} bytes")
    if not total:
        raise RuntimeError(f"no size from 1503; ack_payload={payload_ack[:16].hex()}")
    print(f"  1503 ok, total={total} (+4 prefix)")

    stream += collect(c, total + 4 - len(stream))
    try:
        c._ZK__send_command(const.CMD_FREE_DATA, b"", 1024)
    except Exception:
        pass
    print("  session freed")
    return stream


def main():
    ip = sys.argv[1]
    out = sys.argv[2]
    c = ZK(ip, port=4370, timeout=20, password=0,
           force_udp=False, ommit_ping=True).connect()
    try:
        c.disable_device()
        try:
            blob = pull(c, const.CMD_ATTLOG_RRQ)
            with open(out, "wb") as f:
                f.write(blob)
            declared = unpack("<I", blob[:4])[0]
            print(f"saved {out}: {len(blob)} bytes (declared {declared}, "
                  f"records={(len(blob) - 4) // 22})")
        finally:
            try:
                c.enable_device()
            except Exception:
                pass
    finally:
        try:
            c.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    main()
