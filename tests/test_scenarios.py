"""Scenario corpora and queries must be valid, deterministic and oracle-consistent."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from harness import scenarios
from harness.generate import generate
from oracle.compact import CompactOracle
from oracle.oracle import PreparedOracle, canon_bytes


def build(directory, scenario_id, variant='default', n=300, m=1200, seed=11):
    entry, variant = scenarios.resolve_scenario(scenario_id, variant)
    spec = entry['variants'][variant]
    if spec['shape'] == 'chain':
        m = n - 1
    path = Path(directory) / f'{scenario_id}-{variant}.json'
    meta = scenarios.generate_corpus(path, n, m, seed, spec)
    return entry, variant, path, meta


class Scenarios(unittest.TestCase):
    def test_plain_mixed_matches_the_existing_generator_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as directory:
            old, new = Path(directory) / 'old.json', Path(directory) / 'new.json'
            generate(old, 200, 900, 5, 'mixed')
            scenarios.generate_corpus(new, 200, 900, 5, scenarios.base_spec())
            self.assertEqual(old.read_bytes(), new.read_bytes())

    def test_every_scenario_builds_a_valid_corpus_and_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            for scenario_id, entry in scenarios.REGISTRY.items():
                for variant in entry['variants']:
                    with self.subTest(scenario=scenario_id, variant=variant):
                        entry, variant, path, meta = build(directory, scenario_id, variant)
                        oracle = CompactOracle(path)            # schema + integrity validation
                        jobs = scenarios.build_jobs(scenario_id, variant, meta, oracle if entry['dynamic'] else None)
                        self.assertTrue(jobs)
                        for job in jobs:                         # every query executes and is schema-valid
                            oracle.execute_to(job['query'], Path(directory) / 'r.json')

    def test_generation_is_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            for scenario_id in ('W2.X1', 'W3.S2', 'W4.S5'):
                variant = next(iter(scenarios.REGISTRY[scenario_id]['variants']))
                *_, first, _ = build(Path(directory), scenario_id, variant)
                digest = hashlib.sha256(first.read_bytes()).hexdigest()
                *_, second, _ = build(Path(directory), scenario_id, variant)
                self.assertEqual(digest, hashlib.sha256(second.read_bytes()).hexdigest())

    def test_shapes(self):
        with tempfile.TemporaryDirectory() as directory:
            *_, path, _ = build(directory, 'W2.X2')                # chain: one edge per link
            edges = json.loads(path.read_text())['relations']
            self.assertEqual([(e['object'] - e['subject']) for e in edges], [1] * len(edges))
            *_, path, _ = build(directory, 'W2.X3')                # dupes: each edge four times, evidence identical
            fx = json.loads(path.read_text())
            self.assertEqual(fx['relations'][0], fx['relations'][3])
            self.assertEqual(fx['evidence'][0], fx['evidence'][3])
            *_, path, _ = build(directory, 'W2.X4')                # sparse ids stay unique and within u64
            ids = [e['id'] for e in json.loads(path.read_text())['entities']]
            self.assertEqual(len(set(ids)), len(ids))
            self.assertLess(max(ids), 2**64)
            self.assertGreater(min(ids), 2**64 // 1000)
            *_, path, _ = build(directory, 'W2.X1', n=500, m=5000)  # power law: the hub dominates
            counts = {}
            for e in json.loads(path.read_text())['relations']:
                counts[e['subject']] = counts.get(e['subject'], 0) + 1
            self.assertGreater(counts[1], 10 * (5000 / 500))

    def test_w3_evidence_mixes_have_their_designed_selectivity(self):
        with tempfile.TemporaryDirectory() as directory:
            *_, path, _ = build(directory, 'W3.S1', n=400, m=20000)
            rows = json.loads(path.read_text())['evidence']
            share = lambda pred: sum(map(pred, rows)) / len(rows)          # noqa: E731
            self.assertAlmostEqual(share(lambda r: r['freshness_epoch'] == 1), .5, delta=.03)
            self.assertAlmostEqual(share(lambda r: r['freshness_epoch'] == 2), .1, delta=.02)
            self.assertAlmostEqual(share(lambda r: r['quality'] == 'VERIFIED'), .2, delta=.03)
            *_, path, _ = build(directory, 'W3.S2', n=400, m=20000)
            rows = json.loads(path.read_text())['evidence']
            self.assertGreater(share(lambda r: r['polarity'] == 'NEGATIVE'), .2)
            keys = [json.dumps(r, sort_keys=True) for r in rows]
            self.assertLess(len(set(keys)), len(keys))                      # duplicates are visible

    def test_cap_boundary_queries_straddle_the_truncation_point(self):
        with tempfile.TemporaryDirectory() as directory:
            entry, variant, path, meta = build(directory, 'W2.X5', n=400, m=6000)
            oracle = CompactOracle(path)
            jobs = {j['query']['query_id']: j['query'] for j in scenarios.build_jobs('W2.X5', variant, meta, oracle)}
            classic = PreparedOracle(json.loads(path.read_text()))
            state = lambda q: classic.execute(q)['completeness']['entity_set']   # noqa: E731
            for tag in ('out', 'in'):
                self.assertEqual(state(jobs[f'{tag}-cap-below']), 'TRUNCATED')
                self.assertEqual(state(jobs[f'{tag}-cap-exact']), 'TRUNCATED')   # reaching the cap is conservative
                self.assertEqual(state(jobs[f'{tag}-cap-above']), 'OBSERVED')

    def test_w4_lookup_expectation_matches_the_classic_oracle(self):
        with tempfile.TemporaryDirectory() as directory:
            entry, variant, path, meta = build(directory, 'W4.S3', n=600)
            jobs = scenarios.build_jobs('W4.S3', variant, meta)
            names, rounds = jobs[0]['resolve']['names'], jobs[0]['resolve']['rounds']
            fx = json.loads(path.read_text())
            hasher, total = hashlib.sha256(), 0
            for name in names:
                ids = sorted(e['id'] for e in fx['entities'] if fx['strings'][e['name_sid']] == name)
                hasher.update(b'[' + b','.join(str(i).encode() for i in ids) + b']\n')
                total += len(ids)
            expected = CompactOracle(path).name_lookup(names, rounds)
            self.assertEqual(expected['lookup_digest'], 'sha256:' + hasher.hexdigest())
            self.assertEqual(expected['lookup_ids_total'], total * rounds)
            self.assertEqual(expected['unique_strings'], len({fx['strings'][e['name_sid']] for e in fx['entities']}))
            # The primary query is the first name and executes like any other query.
            self.assertEqual(canon_bytes(PreparedOracle(fx).execute(jobs[0]['query']))[:1], b'{')

    def test_names_have_the_requested_length_and_duplication(self):
        with tempfile.TemporaryDirectory() as directory:
            for variant, length, ratio in (('len8-uniform', 8, .5), ('len512-uniform', 512, .5), ('len64-zipf', 64, .5)):
                *_, path, _ = build(directory, 'W4.S5', variant, n=1000)
                table = json.loads(path.read_text())['strings']
                self.assertEqual({len(s) for s in table}, {length})
                self.assertLessEqual(len(set(table)), 500)
            *_, path, _ = build(directory, 'W4.S4', 'r1', n=1000)
            self.assertEqual(len(set(json.loads(path.read_text())['strings'])), 10)


if __name__ == '__main__':
    unittest.main()


class ProfileBlocks(unittest.TestCase):
    """W4 `name_index` and X-MEM `heap` blocks are validated before a sample is accepted."""

    BASE = {'profile_schema': 'csl.eval.profile/v0.1', 'representation': 'typed-hash-v1',
            'phases_ns': dict.fromkeys(['load', 'index', 'query', 'result'], 0), 'result': None}
    INDEX = {'intern_ns': 5, 'unique_strings': 2, 'lookups': 2, 'lookup_ns': [1, 2], 'lookup_digest': 'sha256:x', 'lookup_ids_total': 3}
    HEAP = {'model': 'requested-bytes', 'allocations': 3, 'total_requested': 10, 'total_freed': 4, 'live': 6, 'peak_live': 8,
            'live_after_load': 2, 'live_after_index': 4, 'live_after_query': 5, 'retained': None}

    def check(self, **extra):
        from harness.benchctl import profile_metadata
        return profile_metadata({**self.BASE, **extra}, 10**9)

    def test_valid_blocks_are_returned(self):
        meta = self.check(name_index=self.INDEX, heap=self.HEAP)
        self.assertEqual(meta['name_index'], self.INDEX)
        self.assertEqual(meta['heap'], self.HEAP)

    def test_malformed_blocks_are_rejected(self):
        for extra in ({'name_index': {**self.INDEX, 'lookups': 3}},                 # length mismatch
                      {'name_index': {**self.INDEX, 'lookup_ns': [1, -2]}},
                      {'name_index': {k: v for k, v in self.INDEX.items() if k != 'lookup_digest'}},
                      {'heap': {**self.HEAP, 'peak_live': 1}},                       # peak below live
                      {'heap': {**self.HEAP, 'model': 'other'}},
                      {'heap': {**self.HEAP, 'live': True}},
                      {'heap': {**self.HEAP, 'unexpected': 1}}):
            with self.subTest(extra=str(extra)[:60]), self.assertRaises(ValueError):
                self.check(**extra)

    def test_lookup_check_requires_the_oracle_digest(self):
        from harness.campaign import check_lookup
        expected = {'lookup_digest': 'sha256:x', 'lookup_ids_total': 3, 'unique_strings': 2, 'lookups': 2}
        check_lookup(self.INDEX, expected)
        for key, value in (('lookup_digest', 'sha256:y'), ('lookup_ids_total', 4), ('unique_strings', 3)):
            with self.assertRaises(ValueError):
                check_lookup({**self.INDEX, key: value}, expected)
        with self.assertRaises(ValueError):
            check_lookup(None, expected)


class ArgumentBudget(unittest.TestCase):
    def test_w4_params_fit_one_argv_argument_for_every_variant(self):
        with tempfile.TemporaryDirectory() as directory:
            for scenario_id, entry in scenarios.REGISTRY.items():
                if not scenario_id.startswith('W4'):
                    continue
                for variant in entry['variants']:
                    with self.subTest(scenario=scenario_id, variant=variant):
                        _, variant, path, meta = build(directory, scenario_id, variant, n=300)
                        job = scenarios.build_jobs(scenario_id, variant, meta)[0]
                        self.assertLess(len(json.dumps({'query': job['query'], 'resolve': job['resolve']})), 120_000)
