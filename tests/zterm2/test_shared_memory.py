import ctypes
import mmap
import os
from pathlib import Path
import subprocess
import sys
import unittest
import uuid

from kitty_fixture import shared_memory_library


class SharedMemoryTest(unittest.TestCase):
    def test_private_memory_is_readable_by_separate_receiver(self):
        library = shared_memory_library()
        name = ("/zt2-unit-" + uuid.uuid4().hex[:12]).encode()
        descriptor = library.shm_open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR, ctypes.c_uint(0o600))
        self.assertGreaterEqual(descriptor, 0)
        try:
            self.assertEqual(os.fstat(descriptor).st_mode & 0o777, 0o600)
            payload = b"opaque-rgba-test-vector"
            os.ftruncate(descriptor, len(payload))
            with mmap.mmap(descriptor, len(payload)) as mapping:
                mapping[:] = payload
            code = "import mmap,os,sys; from kitty_fixture import shared_memory_library; lib=shared_memory_library(); descriptor=lib.shm_open(sys.argv[1].encode(),os.O_RDONLY); assert descriptor>=0; mapping=mmap.mmap(descriptor,os.fstat(descriptor).st_size,prot=mmap.PROT_READ); sys.stdout.buffer.write(mapping[:]); mapping.close(); os.close(descriptor)"
            result = subprocess.run([sys.executable, "-B", "-c", code, name.decode()],
                                    cwd=Path(__file__).parent, capture_output=True, timeout=3)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout[:len(payload)], payload)
            self.assertTrue(all(value == 0 for value in result.stdout[len(payload):]))
        finally:
            os.close(descriptor)
            library.shm_unlink(name)


if __name__ == "__main__":
    unittest.main()
