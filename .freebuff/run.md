# Run doc — Attendance manager (single-file Flask app)

## Artifacts / dependencies

Single-file Python app (`app.py`); no build step, no Node, no env files.

```bash
pip install flask pyzk openpyxl
```

Data files, all auto-created on first run:

- `data/devices.json` — device registry (pre-seeded with the known devices).
- `.tmp/zkteco_sync/` — cloned reference implementation (FastAPI) whose ADMS/PUSH
  method was ported into `app.py`; can be deleted at any time.
- `data/attendance.db` — SQLite archive (attendance / employees / sync_state),
  fed by the auto-sync poller and the ADMS push listener.
- `data/gl_attlog_<ip>.bin`, `data/gl_users_<ip>.bin` — last-good raw snapshots
  for the green-label WiFi units (used as fallback when the device is in its
  post-session cooldown).

## Run server

```powershell
powershell -NoProfile -Command "(Start-Process -FilePath 'python.exe' -ArgumentList 'app.py' -RedirectStandardOutput '<log>' -RedirectStandardError '<log>.err' -WindowStyle Hidden -PassThru).Id"
```

- stdout and stderr must go to DIFFERENT files (`<log>` and `<log>.err`).
- Web UI binds `0.0.0.0:5000` (constants `HOST` / `PORT_WEB` in `app.py`);
  health check: `curl http://127.0.0.1:5000/health` -> `{"ok":true,...}`.
- ADMS push listener binds the same host on port **8081** (override with env
  `ADMS_PORT`). Point a device's "Cloud Server / ADMS" address at
  `<host>:8081`; its serial then appears in the approval queue
  (Sync tab) and must be approved once before events are stored.
- On boot two daemon threads start: the auto-sync poller (default every
  900 s, toggleable in the UI / `POST /api/sync/settings`) and the ADMS
  listener. `python app.py` is all that is needed.

## Ports used

| Port | Purpose |
|---|---|
| 5000 | Web UI + REST API |
| 8081 | ADMS device push (`/iclock/cdata?SN=...`) |

ADMS/PUSH protocol (ported from zkteco_sync): handshake `/iclock/cdata` (GET,
byte-exact option block with Realtime=1), real-time punch pushes (`table=ATTLOG`),
command queue via `/iclock/getrequest` (replies `C:<id>:DATA QUERY ...`), bulk
answers on `/iclock/querydata` (multi-packet `packcnt/packidx` reassembly for
`tablename=user` / `attlog`), acks on `/iclock/devicecmd`. Queue a query via
`POST /api/adms/query {device, table: user|attlog}` or the buttons in the
Sync tab; unknown serials are queued pending approval, never 4xx'd (a refusing
body restarts some firmwares' push setup).
| 4370 | Outbound ZK/SDK to devices (poller, identify, time sync) |

## Notes

- Devices: 172.16.50.30, 172.16.8.20, 172.16.0.20, 172.16.32.21,
  172.16.50.68 (WL50) — plus anything adopted from the network scan.
- The two AK3750 WiFi units are "green label" firmware: pyzk returns empty
  user/attendance lists for them, so `app.py` has a raw 1503/1504 buffered
  reader with per-frame ACKs and a 4 KB frame walker. A full pull from
  .50.68 (~355 KB) takes a few minutes; results are cached in memory (5 min)
  and on disk.
- An aborted 1503 session blocks new ones until the device reboots — the
  puller always frees the session, even on failure paths.
