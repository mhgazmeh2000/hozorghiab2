"""Synthetic FK-5005 wire tests: run a fake device server and drive FKClient."""
import socket, struct, sys, threading
sys.path.insert(0, ".")
from app import FKClient, _fk_decode_record, _fk_packed_time_to_dt

REQ = b"\x55\xaa"
RESP = b"\xaa\x55"


def packed_time(y, mo, d, h, mi):
    return (y << 20) | (mo << 16) | (d << 11) | (h << 6) | mi


RECORDS = [
    bytes([0x01, 0x00, 0x21, 0x00]) + struct.pack("<I", 7)
    + struct.pack(">I", packed_time(2024, 5, 1, 8, 5)),
    bytes([0x01, 0x40, 0x21, 0x00]) + struct.pack("<I", 7)
    + struct.pack(">I", packed_time(2024, 5, 1, 17, 30)),
    bytes([0x01, 0x00, 0x22, 0x00]) + struct.pack("<I", 9)
    + struct.pack(">I", packed_time(2024, 5, 2, 8, 3)),
]


def fake_server(sock, stop):
    """Handle exactly the request sequence FKClient issues."""
    buf = b""

    def recv_exact(n):
        nonlocal buf
        while len(buf) < n:
            ch = sock.recv(4096)
            if not ch:
                raise ConnectionError
            buf += ch
        out, buf2 = buf[:n], buf[n:]
        buf = buf2
        return out

    def recv_request():
        assert recv_exact(2) == REQ
        payload = recv_exact(12)
        recv_exact(2)          # seq (ignored, like the real device)
        return payload

    def send(header, payload=b""):
        sock.sendall(RESP + header + (REQ + payload if payload else b""))

    while not stop.is_set():
        try:
            p = recv_request()
        except (ConnectionError, OSError):
            return
        if p == bytes.fromhex("018000000000000000000000"):          # ping
            send(bytes.fromhex("0101000000000000"))
        elif p == bytes.fromhex("01b4080000000000ffff0000"):        # count
            send(bytes.fromhex("0101010010000000"))                 # 0x1000? no — 16
        elif p[:2] == b"\x01\xa4":                                    # dump
            body = b"".join(RECORDS) + b"\xff" * 12
            send(bytes.fromhex("0101000000000000"), body)
        elif p[:2] == b"\x01\xc7":                                    # name
            uid = struct.unpack("<I", p[2:6])[0]
            nm = {7: b"gazmeh\0\0\0\0", 9: b"ali\0\0\0\0\0\0\0"}[uid]
            send(bytes.fromhex("0101000000000000"), nm)
        else:
            send(bytes.fromhex("0100000000000000"))                 # NAK-ish


def test_packed_time():
    t = packed_time(2024, 5, 1, 8, 5)
    dt = _fk_packed_time_to_dt(t)
    assert dt is not None and dt.year == 2024 and dt.minute == 5, dt
    assert _fk_packed_time_to_dt(0) is None
    print("packed time OK:", dt)


def test_decode_record():
    dec = _fk_decode_record(RECORDS[0])
    assert dec is not None
    uid, dt, st = dec
    assert uid == "7" and st == 0, dec
    assert dt.strftime("%Y-%m-%d %H:%M") == "2024-05-01 08:05", dt
    _, _, st2 = _fk_decode_record(RECORDS[1])
    assert st2 == 1, st2
    print("record decode OK:", dec, "out status:", st2)


def test_client_end_to_end():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def accept():
        conn, _ = srv.accept()
        fake_server(conn, stop)

    th = threading.Thread(target=accept, daemon=True)
    th.start()
    with FKClient("127.0.0.1", port, timeout=5) as fk:
        assert fk.ping() is True
        assert fk.get_count() == 16
        recs, names = fk.get_attendance()
        assert len(recs) == 3, recs
        assert names.get("7") == "gazmeh", names
        assert names.get("9") == "ali", names
    stop.set()
    srv.close()
    print("client end-to-end OK:", len(recs), "records,", len(names), "names")


if __name__ == "__main__":
    test_packed_time()
    test_decode_record()
    test_client_end_to_end()
    print("ALL FK TESTS PASSED")
