"""S0 conformance through the real candidate processes (skipped when a candidate is not built)."""
import tempfile
import unittest

from harness.benchctl import executable
from harness.s0 import run_candidate


class S0Conformance(unittest.TestCase):
    def check(self, name):
        exe = executable(name)
        if not exe.exists():
            self.skipTest(f'{name} not built')
        with tempfile.TemporaryDirectory() as directory:
            report = run_candidate(name, exe, directory, fuzz=40)
        self.assertEqual(report.failed, [])
        self.assertGreater(len(report.results), 200)

    def test_rust(self):
        self.check('rust')

    def test_zig(self):
        self.check('zig')


if __name__ == '__main__':
    unittest.main()
