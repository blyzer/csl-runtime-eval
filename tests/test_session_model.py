"""The independent S0 oracle model: ADR-0008 rules and state_digest independence."""
import json
import unittest

from harness.s0 import relabelled, special_fixture
from oracle.session_model import EVIDENCE_FIELDS, SessionError, SessionModel


def code_of(model, batch):
    try:
        model.copy().mutate(batch)
        return 'ok'
    except SessionError as error:
        return error.code


class Model(unittest.TestCase):
    def setUp(self):
        self.model = SessionModel.from_fixture(special_fixture())

    def test_digest_ignores_layout_and_unreferenced_strings(self):
        other = SessionModel.from_fixture(relabelled(special_fixture()))
        self.assertEqual(self.model.state_digest(), other.state_digest())

    def test_digest_tracks_multiplicity_and_context(self):
        base = self.model.state_digest()[0]
        row = {'op': 'ADD_RELATION', 'subject': 2, 'relation': 'CALLS', 'object': 4}
        changed = self.model.copy()
        changed.mutate([row])
        self.assertNotEqual(base, changed.state_digest()[0])
        changed.mutate([{**row, 'op': 'REMOVE_RELATION'}])
        self.assertEqual(base, changed.state_digest()[0])          # history does not matter
        self.assertEqual(changed.generation, 2)                    # ...but the generation does advance
        other = SessionModel.from_fixture({**special_fixture(), 'snapshot': 'OTHER'})
        self.assertNotEqual(base, other.state_digest()[0])

    def test_rules_of_adr_0008(self):
        m = self.model
        cases = {
            'empty': ([], 'INVALID_INPUT'),
            'add existing': ([{'op': 'ADD_ENTITY', 'id': 1, 'kind': 'TYPE', 'name': 'x', 'container': None}], 'INVALID_INPUT'),
            'remove referenced': ([{'op': 'REMOVE_ENTITY', 'id': 4}], 'INVALID_INPUT'),
            'remove referenced container': ([{'op': 'REMOVE_ENTITY', 'id': 1}], 'INVALID_INPUT'),
            'two ops one id': ([{'op': 'UPDATE_ENTITY', 'id': 5, 'set': {'name': 'a'}}, {'op': 'REMOVE_ENTITY', 'id': 5}], 'INVALID_INPUT'),
            'update empty set': ([{'op': 'UPDATE_ENTITY', 'id': 5, 'set': {}}], 'INVALID_INPUT'),
            'remove too many': ([{'op': 'REMOVE_RELATION', 'subject': 1, 'relation': 'CONTAINS', 'object': 2}] * 3, 'INVALID_INPUT'),
            'add and remove same': ([{'op': 'ADD_RELATION', 'subject': 2, 'relation': 'CALLS', 'object': 4},
                                     {'op': 'REMOVE_RELATION', 'subject': 2, 'relation': 'CALLS', 'object': 4}], 'INVALID_INPUT'),
            'unknown op': ([{'op': 'EXPLODE'}], 'INVALID_REQUEST'),
            'boolean is not an id': ([{'op': 'REMOVE_ENTITY', 'id': True}], 'INVALID_INPUT'),
            'remove one of two': ([{'op': 'REMOVE_RELATION', 'subject': 1, 'relation': 'CONTAINS', 'object': 2}], 'ok'),
            'remove dependents together': ([{'op': 'REMOVE_ENTITY', 'id': 4}, {'op': 'REMOVE_RELATION', 'subject': 2, 'relation': 'CALLS', 'object': 4},
                                            {'op': 'REMOVE_RELATION', 'subject': 4, 'relation': 'CALLS', 'object': 2**64 - 1},
                                            {'op': 'REMOVE_EVIDENCE', **dict(zip(EVIDENCE_FIELDS, (2, 2, 'CALLS', 4, 'NEGATIVE', 'PROBABLE', 2, 7)))}], 'ok'),
            'add entity and use it': ([{'op': 'ADD_ENTITY', 'id': 9, 'kind': 'TYPE', 'name': 'n', 'container': None},
                                       {'op': 'ADD_RELATION', 'subject': 9, 'relation': 'CALLS', 'object': 1}], 'ok'),
        }
        for name, (batch, expected) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(code_of(m, batch), expected)

    def test_failed_batch_changes_nothing(self):
        before = (self.model.state_digest(), self.model.generation)
        with self.assertRaises(SessionError):
            self.model.mutate([{'op': 'ADD_RELATION', 'subject': 1, 'relation': 'CALLS', 'object': 4},
                               {'op': 'REMOVE_ENTITY', 'id': 4}])
        self.assertEqual((self.model.state_digest(), self.model.generation), before)

    def test_restore_advances_generation_and_checks_context(self):
        snap = self.model.copy()
        self.model.mutate([{'op': 'UPDATE_ENTITY', 'id': 5, 'set': {'name': 'other'}}])
        self.assertEqual(self.model.restore_from(snap), 2)
        self.assertEqual(self.model.state_digest(), snap.state_digest())
        with self.assertRaises(SessionError):
            self.model.restore_from(SessionModel.from_fixture({**special_fixture(), 'snapshot': 'OTHER'}))

    def test_queries_use_the_classic_oracle_on_the_resolved_state(self):
        result = self.model.query({'schema': 'csl.eval.query/v0.1', 'query_id': 'q', 'op': 'RESOLVE', 'name': 'plain'})
        self.assertEqual(result['entities'], [1, 5, 2**64 - 1])
        self.assertEqual(json.loads(json.dumps(result))['snapshot'], 'SP')


if __name__ == '__main__':
    unittest.main()
