"""The streaming oracle must be indistinguishable from the classic dict oracle."""
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from harness.campaign import verify_output
from harness.generate import generate
from oracle import compact
from oracle.compact import CompactOracle
from oracle.oracle import PreparedOracle, canon_bytes, execute
from tests.cases import BASE, cases, malformed, query


def write(directory, fixture, name='fixture.json', **dumps):
    path = Path(directory) / name
    path.write_text(json.dumps(fixture, **dumps))
    return path


class CompactOracleEquivalence(unittest.TestCase):
    def check(self, directory, fixture, q):
        path = write(directory, fixture)
        target = Path(directory) / 'result.json'
        info = CompactOracle(path).execute_to(q, target)
        expected = execute(fixture, q)
        self.assertEqual(target.read_bytes(), canon_bytes(expected))
        self.assertEqual(info['digest'], expected['digest'])
        self.assertEqual(info['propositions'], len(expected['propositions']))

    def test_semantic_cases_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, fixture, q, _, _ in cases():
                with self.subTest(name=name):
                    self.check(directory, fixture, q)

    def test_insertion_order_and_layout_do_not_matter(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, fixture, q, _, _ in list(cases())[:12]:
                with self.subTest(name=name):
                    shuffled = dict(reversed(list(fixture.items())))
                    for key in ('entities', 'relations', 'evidence'):
                        random.Random(1).shuffle(shuffled[key])
                    path = write(directory, shuffled, indent=3, ensure_ascii=False)
                    target = Path(directory) / 'result.json'
                    CompactOracle(path).execute_to(q, target)
                    self.assertEqual(target.read_bytes(), canon_bytes(execute(fixture, q)))

    def test_tiny_read_chunks_split_every_token(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(compact._Reader, '__init__', lambda self, path, chunk=7: (
                setattr(self, 'stream', open(path, 'r', encoding='utf-8')), setattr(self, 'chunk', 7),
                setattr(self, 'buf', ''), setattr(self, 'pos', 0), setattr(self, 'eof', False))[0]):
            for name, fixture, q, _, _ in list(cases())[:8]:
                with self.subTest(name=name):
                    self.check(directory, fixture, q)

    def test_generated_shapes_match_classic(self):
        with tempfile.TemporaryDirectory() as directory:
            from harness.benchctl import queries
            for shape in ('mixed', 'cycle', 'fanout'):
                path = Path(directory) / f'{shape}.json'
                generate(path, 60, 300, 5, shape)
                fixture = json.loads(path.read_text())
                classic, streaming = PreparedOracle(fixture), CompactOracle(path)
                for workload in ('W1', 'W2'):
                    for q in queries(workload):
                        with self.subTest(shape=shape, query=q['query_id']):
                            self.assertEqual(streaming.execute(q), classic.execute(q))

    def test_malformed_fixtures_and_queries_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for name, fixture, q in malformed():
                with self.subTest(name=name), self.assertRaises(Exception):
                    CompactOracle(write(directory, fixture)).execute_to(q, Path(directory) / 'result.json')

    def test_invalid_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for text in ('', '[]', '{"schema":', json.dumps(BASE) + ' x', '{"a":1,"a":2}'):
                path = Path(directory) / 'bad.json'
                path.write_text(text)
                with self.subTest(text=text[:20]), self.assertRaises(Exception):
                    CompactOracle(path)


class StreamingVerification(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.expected = execute(BASE, query(entity_id=1))
        self.reference = self.root / 'reference.json'
        self.reference.write_bytes(canon_bytes(self.expected))
        self.envelope = {'profile_schema': 'csl.eval.profile/v0.1', 'representation': 'typed-hash-v1',
                         'phases_ns': dict.fromkeys(['load', 'index', 'query', 'result'], 0)}

    def tearDown(self):
        self.temp.cleanup()

    def stream(self, data):
        path = self.root / 'out'
        path.write_bytes(data)
        return path.open('rb')

    def test_canonical_bytes_match_raw_and_profile_output(self):
        with self.stream(canon_bytes(self.expected) + b'\n') as out:
            self.assertEqual(verify_output(out, self.reference, False), ({}, True))
        for order in (sorted, list):   # Rust emits sorted keys, Zig its own order
            envelope = {**self.envelope, 'result': self.expected}
            keys = order(envelope)
            text = '{' + ','.join(f'{json.dumps(k)}:' + (canon_bytes(v).decode() if k == 'result' else json.dumps(v, separators=(',', ':')))
                                  for k, v in ((k, envelope[k]) for k in keys if k != 'result')) + ',"result":' + canon_bytes(self.expected).decode() + '}\n'
            with self.stream(text.encode()) as out:
                meta, matched = verify_output(out, self.reference, True)
            self.assertTrue(matched)
            self.assertEqual(meta['phases_ns'], self.envelope['phases_ns'])

    def test_a_single_changed_byte_is_rejected_even_when_large(self):
        data = bytearray(canon_bytes(self.expected))
        data[data.index(b'"entities":[') + 12] ^= 1
        with patch('harness.campaign.TREE_COMPARE_LIMIT', 0), self.stream(bytes(data)) as out:
            self.assertFalse(verify_output(out, self.reference, False)[1])

    def test_non_canonical_but_equal_output_passes_only_when_small(self):
        pretty = json.dumps(self.expected, indent=2).encode()
        with self.stream(pretty) as out:
            self.assertTrue(verify_output(out, self.reference, False)[1])
        with patch('harness.campaign.TREE_COMPARE_LIMIT', 0), self.stream(pretty) as out:
            self.assertFalse(verify_output(out, self.reference, False)[1])

    def test_types_are_pinned(self):
        wrong = json.loads(canon_bytes(self.expected))
        wrong['entities'] = [True if x == 1 else x for x in wrong['entities']]
        with self.stream(json.dumps(wrong).encode()) as out:
            self.assertFalse(verify_output(out, self.reference, False)[1])


if __name__ == '__main__':
    unittest.main()
