import errno
import fcntl
import os
from pathlib import Path
import re
import select
import signal
import struct
import subprocess
import tempfile
import termios
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]


class FixtureProcess:
    def __init__(self, executable, instance="alpha", exit_code=0):
        self.master, self.slave = os.openpty()
        self.original = termios.tcgetattr(self.slave)
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 800, 480))
        self.process = subprocess.Popen([str(executable), instance, str(exit_code)],
                                        stdin=self.slave, stdout=self.slave, stderr=self.slave,
                                        env={"TERM": "xterm-256color", "LANG": "en_US.UTF-8"},
                                        start_new_session=True)
        self.transcript = b""
        try:
            self.read_until(lambda: b"SIZE " in self.transcript)
        except BaseException:
            self.close()
            raise

    def read_until(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while not condition():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"Fixture timeout: {self.transcript[-1000:]!r}")
            readers, _, _ = select.select([self.master], [], [], remaining)
            if readers:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    chunk = b""
                if not chunk:
                    raise AssertionError(f"Unexpected PTY EOF: {self.transcript[-1000:]!r}")
                self.transcript += chunk

    def input_bytes(self, instance="alpha"):
        records = re.findall(rb"INPUT " + instance.encode() + rb" ([0-9a-f]+)\r\n", self.transcript)
        return b"".join(bytes.fromhex(record.decode()) for record in records)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.master is not None:
            os.close(self.master)
        os.close(self.slave)


class FixtureTests(unittest.TestCase):
    def assert_restored_termios(self, fixture):
        observed = termios.tcgetattr(fixture.slave)
        observed[3] &= ~termios.PENDIN
        expected = list(fixture.original)
        expected[3] &= ~termios.PENDIN
        self.assertEqual(observed, expected, "Compare configuration, excluding kernel-managed PENDIN")

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="zterm2-embedded-fixture-")
        cls.executable = Path(cls.directory.name) / "fixture"
        subprocess.run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                        "-mmacosx-version-min=13.0", str(ROOT / "tests/zterm2_embedded/fixture.c"),
                        "-o", str(cls.executable)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def fixture(self, instance="alpha", exit_code=0):
        fixture = FixtureProcess(self.executable, instance, exit_code)
        self.addCleanup(fixture.close)
        return fixture

    def test_ansi_unicode_and_protocols(self):
        fixture = self.fixture()
        self.assertIn("été — 日本語 — e\u0301 — 🐚".encode(), fixture.transcript)
        for sequence in (b"\x1b[?1049h", b"\x1b[?2004h", b"\x1b[?1006h", b"\x1b[38;2;80;180;255m"):
            self.assertIn(sequence, fixture.transcript)

    def test_fragmented_paste_unicode_option_and_mouse(self):
        fixture = self.fixture()
        fragments = [b"\x1b[20", b"0~", "été\n日本語".encode(), b"\x1b[201~", b"\x1bf", b"\x1b[<0;12;8M\x1b[<0;12;8m"]
        expected = b""
        for fragment in fragments:
            os.write(fixture.master, fragment)
            expected += fragment
            fixture.read_until(lambda: len(fixture.input_bytes()) >= len(expected))
        self.assertEqual(fixture.input_bytes(), expected)

    def test_large_paste(self):
        fixture = self.fixture()
        payload = b"\x1b[200~" + ("été ".encode() * 1000) + b"\x1b[201~"
        for offset in range(0, len(payload), 128):
            fragment = payload[offset:offset + 128]
            os.write(fixture.master, fragment)
            fixture.read_until(lambda: len(fixture.input_bytes()) >= min(offset + 128, len(payload)))
        self.assertEqual(fixture.input_bytes(), payload)

    def test_resize_cells_and_pixels(self):
        fixture = self.fixture()
        for columns, rows, width, height in ((2, 1, 40, 40), (132, 45, 2640, 1800), (80, 24, 1600, 960)):
            fcntl.ioctl(fixture.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, width, height))
            fixture.process.send_signal(signal.SIGWINCH)
            marker = f"SIZE alpha {columns} {rows} {width} {height}\r\n".encode()
            fixture.read_until(lambda: marker in fixture.transcript)

    def test_exit_codes_and_termios_restoration(self):
        for exit_code in (0, 75):
            with self.subTest(exit_code=exit_code):
                fixture = self.fixture(exit_code=exit_code)
                os.write(fixture.master, b"\x04")
                fixture.read_until(lambda: f"EXIT alpha {exit_code}".encode() in fixture.transcript)
                self.assertEqual(fixture.process.wait(timeout=5), exit_code)
                self.assert_restored_termios(fixture)
                self.assertIn(b"\x1b[?2004l", fixture.transcript)

    def test_requested_stop(self):
        fixture = self.fixture()
        fixture.process.terminate()
        fixture.read_until(lambda: b"EXIT alpha 143" in fixture.transcript)
        self.assertEqual(fixture.process.wait(timeout=5), 143)
        self.assert_restored_termios(fixture)

    def test_two_instances(self):
        first = self.fixture("alpha")
        second = self.fixture("beta")
        os.write(first.master, b"FIRST")
        os.write(second.master, b"SECOND")
        first.read_until(lambda: first.input_bytes("alpha") == b"FIRST")
        second.read_until(lambda: second.input_bytes("beta") == b"SECOND")
        self.assertNotIn(b"SECOND", first.transcript)
        self.assertNotIn(b"FIRST", second.transcript)

    def test_repeated_creation(self):
        initial_descriptors = len(os.listdir("/dev/fd"))
        for iteration in range(12):
            fixture = FixtureProcess(self.executable, f"cycle-{iteration}")
            try:
                os.write(fixture.master, b"\x04")
                self.assertEqual(fixture.process.wait(timeout=5), 0)
            finally:
                fixture.close()
        self.assertEqual(len(os.listdir("/dev/fd")), initial_descriptors)

    def test_hangup_during_activity(self):
        fixture = self.fixture()
        os.write(fixture.master, b"activity")
        fixture.read_until(lambda: fixture.input_bytes() == b"activity")
        os.close(fixture.master)
        fixture.master = None
        self.assertEqual(fixture.process.wait(timeout=5), 0)

    def test_rapid_resizes_converge(self):
        fixture = self.fixture()
        for columns in range(2, 100):
            fcntl.ioctl(fixture.slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, columns, columns * 20, 960))
            fixture.process.send_signal(signal.SIGWINCH)
        fixture.read_until(lambda: b"SIZE alpha 99 24 1980 960\r\n" in fixture.transcript)

    def test_invalid_launch_arguments(self):
        result = subprocess.run([str(self.executable), "bad instance", "0"], capture_output=True)
        self.assertEqual(result.returncode, 64)


if __name__ == "__main__":
    unittest.main(verbosity=2)
