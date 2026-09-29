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


class PeakRss(unittest.TestCase):
    """The child's own peak RSS must not inherit the harness's (Linux exec folds the parent's high-water mark)."""

    def test_peak_rss_is_the_childs_own(self):
        import sys
        from harness.s0 import Client
        exe = executable('zig')
        if not exe.exists() or not sys.platform.startswith('linux'):
            self.skipTest('Linux-only check with a built Zig candidate')
        ballast = bytearray(400 * 1024 * 1024)                      # make the parent large before spawning
        ballast[::4096] = b'x' * len(ballast[::4096])
        with tempfile.TemporaryDirectory() as directory:
            client = Client(exe, directory)
            client.open_empty({'snapshot': 'S0', 'epoch': 1, 'complete': False})
            rss = client.finish()
        self.assertIsNotNone(rss)
        self.assertLess(rss, 200 * 1024 * 1024)                     # an empty session is a few MB, not the parent's 400 MB
