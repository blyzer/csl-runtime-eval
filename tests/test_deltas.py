"""W8 deltas must be valid, deterministic and shaped like their composition."""
import collections
import json
from pathlib import Path
import tempfile
import unittest

from harness.deltas import COMPOSITIONS, FRACTIONS, delta_queries, make_delta, standard_queries, total_rows
from harness.generate import generate
from oracle.session_model import SessionError, SessionModel


class Deltas(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        path = Path(cls.directory.name) / 'fx.json'
        generate(path, 400, 4000, 3, 'mixed')
        cls.fixture = json.loads(path.read_text())

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_every_delta_is_valid_and_deterministic(self):
        for composition in COMPOSITIONS:
            for fraction in FRACTIONS:
                with self.subTest(composition=composition, fraction=fraction):
                    batch = make_delta(self.fixture, composition, fraction)
                    self.assertEqual(batch, make_delta(self.fixture, composition, fraction))
                    model = SessionModel.from_fixture(self.fixture)
                    model.mutate(batch)                             # raises if any ADR-0008 rule is broken
                    target = max(4, round(fraction * total_rows(self.fixture)))
                    self.assertGreaterEqual(len(batch), min(target, 4))
                    self.assertLessEqual(len(batch), 3 * target + 60)   # dependents of removals may exceed the budget

    def test_compositions_touch_the_right_kinds_of_rows(self):
        kinds = {c: collections.Counter(op['op'] for op in make_delta(self.fixture, c, 0.05)) for c in COMPOSITIONS}
        self.assertTrue(set(kinds['relation']) <= {'ADD_RELATION', 'REMOVE_RELATION'})
        self.assertTrue(set(kinds['evidence']) <= {'ADD_EVIDENCE', 'REMOVE_EVIDENCE'})
        self.assertIn('UPDATE_ENTITY', kinds['entity'])
        self.assertIn('ADD_ENTITY', kinds['entity'])
        self.assertEqual({'ADD_ENTITY', 'UPDATE_ENTITY', 'REMOVE_ENTITY', 'ADD_RELATION', 'REMOVE_RELATION', 'ADD_EVIDENCE', 'REMOVE_EVIDENCE'} - set(kinds['mixed']), set())

    def test_delta_size_scales_with_fraction(self):
        sizes = [len(make_delta(self.fixture, 'relation', f)) for f in FRACTIONS]
        self.assertEqual(sizes, sorted(sizes))
        self.assertLess(sizes[0], sizes[-1])

    def test_query_sets_execute_on_the_mutated_state(self):
        batch = make_delta(self.fixture, 'mixed', 0.01)
        model = SessionModel.from_fixture(self.fixture)
        model.mutate(batch)
        for q in standard_queries(self.fixture) + delta_queries(batch, self.fixture):
            model.query(q)

    def test_a_delta_with_an_added_bad_op_is_rejected(self):
        batch = make_delta(self.fixture, 'relation', 0.01) + [{'op': 'ADD_RELATION', 'subject': 10**9, 'relation': 'CALLS', 'object': 1}]
        with self.assertRaises(SessionError):
            SessionModel.from_fixture(self.fixture).mutate(batch)


if __name__ == '__main__':
    unittest.main()
