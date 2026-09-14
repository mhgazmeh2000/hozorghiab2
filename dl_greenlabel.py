# -*- coding: utf-8 -*-
"""Attendance-log + user-table downloader for ZKTeco 'green label' WiFi
units (AK3750WIFI_TFT platform: WL50 / Timmy AI09F).

These firmwares return an EMPTY CMD_GET_FREE_SIZES packet, so pyzk's
get_attendance()/get_users() short-circuit to [] even though the data is
on the device. The data must be pulled with the canonical buffered flow
(CMD 1503/1504) — which pyzk's read_with_buffer() implements correctly.

Hygiene rules learned the hard way (this firmware is fragile):
  * ALWAYS disable_device() before bulk reads, enable_device() after.
  * read_with_buffer() ends with free_data() — never abandon a session
    mid-transfer; a dangling 1503 session blocks ALL further reads until
    the device reboots.
  * Kill stale client processes before retrying: orphan TCP sessions
    also poison the device.
  * After a reboot the device needs ~1-2 min before 1503 is accepted.

Usage:
    python dl_greenlabel.py <device_ip>
Saves <ip>_attlog.bin and <ip>_users.bin next to the script.
"""
import os
import sys
import time
from struct import pack, unpack

from zk import ZK, const

HERE = os.path.dirname(os.path.abspath(__file__))


def fetch(ip):
    last_err = None
    for attempt in range(1, 5):
        conn = None
        try:
            conn = ZK(ip, port=4370, timeout=20, password=0,
                      force_udp=False, ommit_ping=True).connect()
            conn.disable_device()
            try:
                att, att_size = conn.read_with_buffer(const.CMD_ATTLOG_RRQ)
                print(f"  attlog: {att_size} bytes")
                usr, usr_size = conn.read_with_buffer(
                    const.CMD_USERTEMP_RRQ, const.FCT_USER)
                print(f"  users : {usr_size} bytes")
                with open(os.path.join(HERE, f"{ip}_attlog.bin"), "wb") as f:
                    f.write(att[:att_size])
                with open(os.path.join(HERE, f"{ip}_users.bin"), "wb") as f:
                    f.write(usr[:usr_size])
                print("  saved OK")
                return True
            finally:
                try:
                    conn.free_data()
                except Exception:
                    pass
                try:
                    conn.enable_device()
                except Exception:
                    pass
        except Exception as e:
            last_err = e
            print(f"  attempt {attempt} failed: {type(e).__name__}: {e}")
            wait = 30 * attempt
            print(f"  waiting {wait}s ...")
            time.sleep(wait)
            # Force a clean slate: reboot the device if we keep failing.
            if attempt >= 2:
                try:
                    conn = ZK(ip, port=4370, timeout=20, password=0,
                              force_udp=False, ommit_ping=True).connect()
                    print("  rebooting device ...")
                    conn.restart()
                except Exception:
                    pass
                time.sleep(75)
        finally:
            if conn is not None:
                try:
                    conn.disconnect()
                except Exception:
                    pass
    raise RuntimeError(f"all attempts failed; last error: {last_err}")


if __name__ == "__main__":
    ip = sys.argv[1] if len(sys.argv) > 1 else "172.16.50.68"
    fetch(ip)
