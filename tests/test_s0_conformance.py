"""S0 conformance through the real candidate processes (skipped when a candidate is not built)."""
import tempfile
import unittest

from harness.benchctl import executable
from harness.s0 import run_candidate


class S0Conformance(unittest.TestCase):
    def check(self, name, strategy):
        exe = executable(name)
        if not exe.exists():
            self.skipTest(f'{name} not built')
        with tempfile.TemporaryDirectory() as directory:
            report = run_candidate(name, exe, directory, fuzz=40, strategy=strategy)
        self.assertEqual(report.failed, [])
        self.assertGreater(len(report.results), 200)

    def test_rust_full_rebuild(self):
        self.check('rust', 'full-rebuild')

    def test_rust_incremental(self):
        self.check('rust', 'incremental')

    def test_zig_full_rebuild(self):
        self.check('zig', 'full-rebuild')

    def test_zig_incremental(self):
        self.check('zig', 'incremental')

    def test_hybrid_full_rebuild(self):
        self.check('hybrid', 'full-rebuild')

    def test_hybrid_incremental(self):
        self.check('hybrid', 'incremental')


if __name__ == '__main__':
    unittest.main()
