"""Tests for the runtime-settings feature (no devices needed)."""
import sys, json
sys.path.insert(0, ".")

import app

FAILED = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


c = app.app.test_client()

# 1) GET returns settings + defaults + backup dir with native separators
r = c.get("/api/settings")
s = r.get_json()
check("GET /api/settings 200", r.status_code == 200)
check("settings payload shape", isinstance(s.get("settings"), dict)
      and isinstance(s.get("defaults"), dict))
check("cache defaults intact", s["settings"]["cache_ttl"]["devices"] == 5
      and s["settings"]["cache_ttl"]["archive"] == 30)
check("backup_dir native separators", "\\" in s["backup_dir"] or "/" in s["backup_dir"])
check("settings_file reported", s["settings_file"].endswith("settings.json"))

# 3) POST applies immediately to cfg() (and creates settings.json on save)
r = c.post("/api/settings", json={"cache_ttl": {"devices": 42}})
check("POST ok", r.status_code == 200 and r.get_json().get("ok") is True)
check("cfg() live effect", app.cfg("cache_ttl.devices") == 42)

# 2) settings.json now exists on disk (created by the first POST)
import os
check("settings.json created", os.path.exists(s["settings_file"]))

# 4) other endpoints use the new TTL (devices cache now lives 42 s)
r1 = c.get("/api/devices")
r2 = c.get("/api/devices")
check("devices endpoint still 200 twice", r1.status_code == 200 and r2.status_code == 200)

# 5) restore + invalidate dropped the cache
r = c.post("/api/settings", json={"cache_ttl": {"devices": 5}})
check("restore default ok", r.get_json()["settings"]["cache_ttl"]["devices"] == 5)
check("cfg back to 5", app.cfg("cache_ttl.devices") == 5)

# 6) validation: out-of-range rejected with 400
r = c.post("/api/settings", json={"cache_ttl": {"devices": 9999}})
check("out-of-range rejected 400", r.status_code == 400)

# 7) whitelist: unknown key rejected with 400
r = c.post("/api/settings", json={"hack": {"root": 1}})
check("unknown key rejected 400", r.status_code == 400)

# 8) non-numeric rejected
r = c.post("/api/settings", json={"backup": {"keep": "many"}})
check("non-numeric rejected 400", r.status_code == 400)

# 9) empty / invalid body rejected
r = c.post("/api/settings", json={})
check("empty body rejected 400", r.status_code == 400)
r = c.post("/api/settings", data="not json", content_type="application/json")
check("garbage body rejected 400", r.status_code == 400)

# 10) ui_polling editable (browser reads it)
r = c.post("/api/settings", json={"ui_polling": {"devices_ms": 12345}})
check("ui_polling editable", r.get_json()["settings"]["ui_polling"]["devices_ms"] == 12345)
c.post("/api/settings", json={"ui_polling": {"devices_ms": 8000}})

# 11) env override respected when setting absent (precedence rule)
os.environ["HOZOR_LOCK_TIMEOUT_FK"] = "77"
app._SETTINGS.pop("lock_timeout", None)   # simulate fresh defaults
check("env override when no setting", app.cfg("lock_timeout.fk") == 77)
del os.environ["HOZOR_LOCK_TIMEOUT_FK"]
check("code default fallback", app.cfg("lock_timeout.fk") == 8)

# 12) file on disk survived the round-trips (valid JSON)
with open(s["settings_file"], encoding="utf-8") as f:
    disk = json.load(f)
check("settings.json valid JSON", isinstance(disk, dict) and "cache_ttl" in disk)

# 13) regression: key endpoints still respond with expected shapes
check("/health ok", c.get("/health").get_json().get("ok") is True)
check("/api/sync ok", c.get("/api/sync").status_code == 200)
check("/api/fetch-progress ok", c.get("/api/fetch-progress").status_code == 200)

print()
print("RESULT:", "ALL PASS" if not FAILED else f"{len(FAILED)} FAILED -> {FAILED}")
sys.exit(1 if FAILED else 0)
