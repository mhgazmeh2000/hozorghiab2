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
- Green-label TCP sessions only open during short windows. A
  `revival-watch` thread probes offline devices every 60 s; on the first
  answer it immediately re-points the device's ADMS push config to this
  server (CMD 69 `WebServerIP=...` — another project may have re-pointed
  it, which is how a device "works" in a different tool while refusing us),
  reboots it to apply, and runs a full sync while the window is open.
  SDK timeout was raised 6 s → 20 s (zkteco_sync uses 30–60 s).
- Clock setting (like zkteco_sync): `POST /api/devices/<ip>/set_time`
  accepts `{"sync": true}` (server time) or `{"dt": "YYYY-MM-DD HH:MM:SS"}`
  (custom). Direct TCP write is tried first — the green-label firmware
  ACCEPTS a plain time-write over TCP (verified live) even though it
  rejects interactive enrollment — and the ADMS `SET OPTIONS DateTime=`
  command (spec §12.5.1 packed-time encoding) is queued as fallback when
  TCP is refused (session cooldown). UI: «همگام‌سازی ساعت» (server time)
  and «تنظیم دلخواه ساعت» (custom) buttons per device row.
- Attendance PUSH ATTLOG parsing follows zkteco_sync's positional rules
  (user_id \t timestamp \t status \t punch) first, with the tolerant
  timestamp-scan as fallback — a 2-digit user id used to be mis-read as
  the punch code by the old heuristic (wire-tested).
- Fingerprint enrollment on green-label units: interactive STARTENROLL
  (CMD 61) is NAK'd by the firmware (verified live), but TEMPLATE WRITES
  work (CMD 110 "save utemp" verified with a byte-identical round-trip).
  So enrollment is remote-copied: enroll on a normal device (UF100/MB20),
  then `POST /api/devices/<ip>/enroll {user_id, uid, source}` copies the
  template to the green-label unit (`_gl_write_template`). Template table
  read: 1503/FCT_FINGERTMP via `_gl_pull`, records are
  (size:u16,uid:u16,fid:i8,valid:i8)+template, empty slots are zeroed.
- Attendance inquiry (ترددها tab): the live pull reports every stage
  (lock-wait → connect → identify → attlog → parse → done) to
  `GET /api/fetch-progress`, shown live in the page's progress panel; the
  per-request `timeout` field (2–120 s, default 10) tunes socket timeouts
  for slow WiFi units. If a device fails the live pull, its records are
  still shown from the SQLite archive (`archived: true`, with the error
  and record count in the summary line) instead of an empty table.
- All buttons show a spinner ("در حال پردازش…") while their request runs
  and a success/error toast when it finishes; long device operations
  (ping-all, logs) release the button as soon as the UI summary updates.
- `PATCH /api/devices/<ip>` edits label/location/enabled of a registered
  device without deleting it (e.g. to re-pin the WL50 label after an
  identify that overwrites it with the platform name).
- FK/B-series devices (Faratechno AI09F-class face units, e.g. 172.16.8.20)
  do NOT speak ZK/4370. The vendor suite (`faratecno/`, "محاسبه کارکرد" by
  Latifi) talks to them with the FKAttend/FKViaDev protocol on **TCP 5005**
  (16-byte `55aa` frames, documented wire format). app.py ships a native
  client (`FKClient`, `fk_fetch_logs`) — ping / record-count / 12-byte
  record dump / 10-char employee names — selected automatically whenever a
  device's port is 5005 (`is_fk_device`). Tests: `.tmp/test_fk.py` (fake
  device over loopback) — all pass. Live status: .8.20 accepts TCP on 5005
  but resets every frame from a non-vendor client (suspected client-key
  handshake in FKAttend.dll); ZK units keep working, .8.20 will need either
  the missing handshake byte-exact from a live sniff, or the vendor's own
  push (IOTC/P2P) channel. The `faratecno/` folder itself contains only
  NSIS installers (no live software on this PC — confirmed by filesystem
  search). The suite unpacks to: LatifiWorkingTimeUIWinform.exe +
  FaraTechnoDatabase.mdb (Access) + FKAttend/FKViaDev (FK protocol) +
  zkemsdk/zkemkeeper (ZK protocol) + IOTCAPIs/P2PTunnelAPIs (ThroughTek
  cloud P2P) + plcommpro (ZK panels).
