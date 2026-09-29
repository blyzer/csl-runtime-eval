import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from harness.campaign import condition_failures, sample_worker, schedule, strict_equal
from oracle.oracle import PreparedOracle, execute
from tests.cases import BASE, cases, query


class Campaign(unittest.TestCase):
    def test_order_is_paired_balanced_and_reproducible(self):
        order = schedule(10, 17, ['W1', 'W2'])
        self.assertEqual(order, schedule(10, 17, ['W1', 'W2']))
        self.assertNotEqual(order, schedule(10, 18, ['W1', 'W2']))
        self.assertEqual(len(order), 180)
        for start in range(0, len(order), 20):
            block = order[start:start + 20]
            self.assertEqual(sum(j['candidate'] == 'rust' for j in block[::2]), 5)
            for left, right in zip(block[::2], block[1::2]):
                self.assertEqual({left['candidate'], right['candidate']}, {'rust', 'zig'})
                self.assertEqual(left['query'], right['query'])
                self.assertEqual(left['iteration'], right['iteration'])

    def test_order_rotates_three_candidates_evenly(self):
        candidates = ('rust', 'zig', 'hybrid')
        order = schedule(9, 17, ['W1'], candidates)
        self.assertEqual(order, schedule(9, 17, ['W1'], candidates))
        for start in range(0, len(order), 27):
            block = order[start:start + 27]
            for position in range(3):
                seen = {block[i + position]['candidate'] for i in range(0, 27, 3)}
                self.assertEqual(seen, set(candidates))
            for group_start in range(0, 27, 3):
                group = block[group_start:group_start + 3]
                self.assertEqual({j['candidate'] for j in group}, set(candidates))
                self.assertEqual(len({j['iteration'] for j in group}), 1)
                self.assertEqual(len({j['query']['query_id'] for j in group}), 1)

    def test_equality_preserves_json_types(self):
        self.assertFalse(strict_equal({'entities':[True]}, {'entities':[1]}))
        self.assertFalse(strict_equal({'entities':[1.0]}, {'entities':[1]}))
        self.assertTrue(strict_equal({'b':None,'a':[1]}, {'a':[1],'b':None}))

    def test_machine_guards(self):
        healthy = {'cpu_count':10, 'load_average':[2,2,2], 'available_memory_bytes':4 * 1024**3, 'power':"Now drawing from 'AC Power'"}
        self.assertFalse(condition_failures(healthy, 0.5, 3 * 1024**3))
        for change in ({'load_average':[51,50,40]}, {'available_memory_bytes':None}, {'available_memory_bytes':1}):
            self.assertTrue(condition_failures({**healthy, **change}, 0.5, 3 * 1024**3))

    def test_prepared_oracle_resets_query_state(self):
        oracle = PreparedOracle(BASE)
        capped = query('TRAVERSE', input={'op':'RESOLVE','entity_id':1}, max_paths=1)
        self.assertEqual(oracle.execute(capped)['completeness']['entity_set'], 'TRUNCATED')
        self.assertEqual(oracle.execute(query(entity_id=999)), execute(BASE, query(entity_id=999)))
        for _, fixture, q, _, _ in cases():
            prepared = PreparedOracle(fixture)
            self.assertEqual(prepared.execute(q), execute(fixture,q))
            self.assertEqual(prepared.execute(q), execute(fixture,q))

    def test_sample_compares_full_result_and_rejects_invalid_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = execute(BASE, query(entity_id=1))
            envelope = {'profile_schema':'csl.eval.profile/v0.1', 'representation':'typed-hash-v1',
                        'phases_ns':dict.fromkeys(['load','index','query','result'],0), 'result':expected}
            binary = root/'candidate'
            binary.write_text(f'#!{sys.executable}\nprint({json.dumps(json.dumps(envelope))})\n')
            binary.chmod(0o755)
            reference = root/'reference.json'
            reference.write_text(json.dumps(expected))
            spec = {'job':{'candidate':'rust','workload':'W1','query':query(entity_id=1)}, 'binary':str(binary),
                    'fixture':'unused', 'reference':str(reference),'max_load':100,'min_memory':1}
            spec_path = root/'spec.json';spec_path.write_text(json.dumps(spec))
            result_path = root/'sample.json'
            sample_worker(spec_path,result_path)
            self.assertTrue(json.loads(result_path.read_text())['conformance'])
            result_path.unlink()
            reference.write_text(json.dumps({**expected,'snapshot':'wrong'}))
            with self.assertRaisesRegex(ValueError,'full oracle result mismatch'):
                sample_worker(spec_path,result_path)
            self.assertFalse(result_path.exists())
            reference.write_text(json.dumps(expected))
            envelope['phases_ns']['query']=-1
            binary.write_text(f'#!{sys.executable}\nprint({json.dumps(json.dumps(envelope))})\n')
            with self.assertRaises(Exception):sample_worker(spec_path,result_path)

    def test_sample_skips_profile_for_hybrid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = execute(BASE, query(entity_id=1))
            binary = root/'candidate'
            binary.write_text(f'#!{sys.executable}\nprint({json.dumps(json.dumps(expected))})\n')
            binary.chmod(0o755)
            reference = root/'reference.json'
            reference.write_text(json.dumps(expected))
            spec = {'job':{'candidate':'hybrid','workload':'W1','query':query(entity_id=1)}, 'binary':str(binary),
                    'fixture':'unused', 'reference':str(reference),'max_load':100,'min_memory':1}
            spec_path = root/'spec.json';spec_path.write_text(json.dumps(spec))
            result_path = root/'sample.json'
            sample_worker(spec_path,result_path)
            record = json.loads(result_path.read_text())
            self.assertTrue(record['conformance'])
            self.assertNotIn('phases_ns',record)
