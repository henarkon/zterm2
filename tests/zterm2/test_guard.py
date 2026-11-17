import json
from pathlib import Path
import subprocess
import sys
import unittest


GUARD = Path(__file__).with_name("guarded_run.py")


class GuardTest(unittest.TestCase):
    def run_guard(self, arguments, code):
        result = subprocess.run([sys.executable, "-B", str(GUARD), *arguments, "--",
                                 sys.executable, "-B", "-c", code], capture_output=True, text=True,
                                timeout=8)
        return result, json.loads(result.stdout)

    def test_external_deadline_kills_child(self):
        result, report = self.run_guard(["--seconds", "0.2"], "import time; time.sleep(5)")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["stop_reason"], "time-limit")
        self.assertLess(report["elapsed_seconds"], 3)

    def test_external_memory_guard_kills_child(self):
        result, report = self.run_guard(["--mib", "16"],
                                       "import time; data=bytearray(32*1024*1024); time.sleep(5)")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["stop_reason"], "memory-limit")
        self.assertGreater(report["peak_physical_mib"], 16)

    def test_success_includes_observer_and_timebase(self):
        result, report = self.run_guard([], "import time; time.sleep(0.15)")
        self.assertEqual(result.returncode, 0)
        self.assertIsNone(report["stop_reason"])
        self.assertGreater(report["samples"], 0)
        self.assertGreater(report["mach_timebase"][1], 0)


if __name__ == "__main__":
    unittest.main()
