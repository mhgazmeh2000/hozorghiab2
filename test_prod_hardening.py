"""Smoke tests for the production-hardening changes (no device traffic)."""
import sys, time
sys.path.insert(0, ".")
import app as A

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)

# ---- 1) TTL cache behavior ------------------------------------------------
calls = {"n": 0}
def producer():
    calls["n"] += 1
    return {"v": calls["n"]}
a = A.cache_get("t1", 0.2, producer)
b = A.cache_get("t1", 0.2, producer)      # cached
check("cache hit within TTL", a is b and calls["n"] == 1)
time.sleep(0.25)
c = A.cache_get("t1", 0.2, producer)      # expired
check("cache expires after TTL", calls["n"] == 2)
A.cache_invalidate("t1")
d = A.cache_get("t1", 0.2, producer)      # invalidated
check("cache_invalidate drops entry", calls["n"] == 3)

# ---- 2) lock helper -------------------------------------------------------
lk = A._acquire_device_lock("test-ip-1", 1.0)
check("acquire free lock", lk is not None)
t0 = time.monotonic()
lk2 = A._acquire_device_lock("test-ip-1", 0.3)
check("busy lock returns None after timeout",
      lk2 is None and 0.25 <= time.monotonic() - t0 < 1.0)
lk.release()
lk3 = A._acquire_device_lock("test-ip-1", 1.0)
check("released lock re-acquirable", lk3 is not None)
if lk3: lk3.release()

# ---- 3) endpoints via Flask test client -----------------------------------
cli = A.app.test_client()
r = cli.get("/api/devices")
check("GET /api/devices 200 + shape",
      r.status_code == 200 and "devices" in r.get_json() and "subnets" in r.get_json())
r2 = cli.get("/api/devices")
check("GET /api/devices served from cache (same bytes)",
      r2.data == r.data)
r = cli.get("/api/sync")
j = r.get_json()
check("GET /api/sync 200 + shape",
      r.status_code == 200 and j.get("ok") is True and "running" in j and "db_total" in j)
r = cli.get("/api/connection-logs?limit=10")
j = r.get_json()
check("GET /api/connection-logs 200 + shape",
      r.status_code == 200 and j.get("ok") is True and isinstance(j.get("logs"), list))
r = cli.get("/api/archive?limit=3")
j = r.get_json()
check("GET /api/archive 200 + shape",
      r.status_code == 200 and j.get("ok") is True
      and isinstance(j.get("records"), list) and "total" in j)
r = cli.get("/api/scan")
j = r.get_json()
check("GET /api/scan 200 + shape",
      r.status_code == 200 and "running" in j and "progress" in j)
r = cli.get("/health")
check("GET /health 200", r.status_code == 200 and r.get_json().get("ok") is True)

# ---- 4) archive cache invalidation on write -------------------------------
t0 = time.monotonic()
cli.get("/api/archive?limit=3")            # prime cache
A.cache_invalidate("archive")              # simulate a write
r = cli.get("/api/archive?limit=3")
check("archive re-serve after invalidate", r.status_code == 200)

# ---- 5) sync queue wiring --------------------------------------------------
check("sync worker lazy-start flag exists", hasattr(A, "_sync_queue_job"))
check("pending-IP set exists", isinstance(A._SYNC_PENDING_IPS, set))

# ---- 6) FK dispatcher still routed -----------------------------------------
check("is_fk_device(5005)", A.is_fk_device({"port": 5005}))
check("is_fk_device(4370) False", not A.is_fk_device({"port": 4370}))

print()
print("FAILED:", fails) if fails else print("ALL HARDENING TESTS PASSED")
sys.exit(1 if fails else 0)
