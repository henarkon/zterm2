import argparse
import base64
import ctypes
import hashlib
import json
import mmap
import os
from pathlib import Path
import re
import select
import sys
import termios
import time
import tty


WIDTH, HEIGHT = 640, 360


def shared_memory_library():
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    library.shm_open.argtypes = [ctypes.c_char_p, ctypes.c_int]
    library.shm_open.restype = ctypes.c_int
    library.shm_unlink.argtypes = [ctypes.c_char_p]
    return library


def receive(pattern, deadline, phase):
    data = b""
    while time.monotonic() < deadline:
        readable, _, _ = select.select([sys.stdin.fileno()], [], [], min(0.05, max(0, deadline - time.monotonic())))
        if readable:
            data += os.read(sys.stdin.fileno(), 4096)
            match = re.search(pattern, data)
            if match:
                return match
            if len(data) > 8192:
                raise RuntimeError("terminal response budget exceeded")
    raise TimeoutError("terminal response deadline: " + phase + " bytes=" + repr(data[:256]))


def frame_bytes(frame):
    first = bytes([frame * 13 % 256, 30, 200, 255]) * (WIDTH // 2)
    second = bytes([20, frame * 7 % 256, 40, 255]) * (WIDTH // 2)
    upper = (first + second) * (HEIGHT // 2)
    lower = (second + first) * (HEIGHT // 2)
    return upper + lower


def publish_json(path, value):
    with path.open("x") as destination:
        json.dump(value, destination)


def main():
    parser = argparse.ArgumentParser(description="Bounded local Kitty shared-memory fixture for an isolated terminal.")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    if not sys.stdin.isatty() or not args.directory.is_dir():
        parser.error("requires its own terminal and existing private result directory")
    library = shared_memory_library()
    original = termios.tcgetattr(sys.stdin.fileno())
    live = None
    started = time.monotonic()
    fixture_hash = hashlib.sha256()
    token = hashlib.sha256(str(args.directory).encode()).hexdigest()[:6]
    shm_prefix = f"/zt2-{token}-{os.getpid()}-"
    published = 0
    maximum_shared_bytes = 0
    try:
        tty.setraw(sys.stdin.fileno())
        publish_json(args.directory / "producer.json", dict(pid=os.getpid(), shm_prefix=shm_prefix))
        time.sleep(7)
        started = time.monotonic()
        os.write(sys.stdout.fileno(), b"\x1b[8;24;106t\x1b[18t")
        size = receive(rb"\x1b\[8;(\d+);(\d+)t", time.monotonic() + 2, "initial geometry")
        rows, columns = map(int, size.groups())
        if rows < 4 or columns < 8:
            raise RuntimeError(f"invalid fixture geometry: {columns}x{rows}")
        for frame in range(120):
            if time.monotonic() - started > 10:
                raise TimeoutError("fixture total deadline")
            if frame == 30:
                os.write(sys.stdout.fileno(), b"\x1b[18t")
                size = receive(rb"\x1b\[8;(\d+);(\d+)t", time.monotonic() + 2, "measurement geometry")
                rows, columns = map(int, size.groups())
                publish_json(args.directory / "measurement-start.json", dict(monotonic=time.monotonic()))
            pixels = frame_bytes(frame)
            if frame >= 30:
                fixture_hash.update(pixels)
            live = f"{shm_prefix}{frame}".encode()
            descriptor = library.shm_open(live, os.O_CREAT | os.O_EXCL | os.O_RDWR, ctypes.c_uint(0o600))
            if descriptor < 0:
                raise OSError(ctypes.get_errno(), "shm_open")
            try:
                os.ftruncate(descriptor, len(pixels))
                maximum_shared_bytes = max(maximum_shared_bytes, os.fstat(descriptor).st_size)
                with mmap.mmap(descriptor, len(pixels)) as mapping:
                    mapping[:] = pixels
            finally:
                os.close(descriptor)
            payload = base64.b64encode(live)
            header = f"\x1b_Ga=T,f=32,s={WIDTH},v={HEIGHT},t=s,S={len(pixels)},i=31,p=1,c={columns},r={rows},C=1;".encode()
            os.write(sys.stdout.fileno(), header + payload + b"\x1b\\")
            reply = receive(rb"\x1b_G[^;]*;([^\x1b]+)\x1b\\", time.monotonic() + 2, f"frame {frame} ACK")
            if reply.group(1) != b"OK":
                raise RuntimeError("Kitty receiver rejected frame: " + reply.group(1).decode(errors="replace"))
            library.shm_unlink(live)
            live = None
            if frame >= 30:
                published += 1
            remaining = started + (frame + 1) / 30 - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
        os.write(sys.stdout.fileno(), b"\x1b[18t")
        final_size = receive(rb"\x1b\[8;(\d+);(\d+)t", time.monotonic() + 2, "final geometry")
        if tuple(map(int, final_size.groups())) != (rows, columns):
            raise RuntimeError("geometry changed during measurement")
        publish_json(args.directory / "result.json", dict(success=True, measured_frames=published,
                     total_frames=120, geometry=[WIDTH, HEIGHT], cells=[columns, rows], requested_fps=30,
                     fixture_sha256=fixture_hash.hexdigest(), monotonic=time.monotonic(), live_shm=0,
                     startup_settle_seconds=7, warmup_seconds=1,
                     transport="kitty-rgba-posix-shm", maximum_pending_images=1,
                     maximum_shared_bytes=maximum_shared_bytes))
    except Exception as error:
        publish_json(args.directory / "error.json", dict(error=str(error), monotonic=time.monotonic()))
        raise
    finally:
        if live is not None:
            library.shm_unlink(live)
        os.write(sys.stdout.fileno(), b"\x1b_Ga=d,d=i,i=31,q=2;\x1b\\")
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, original)


if __name__ == "__main__":
    main()
