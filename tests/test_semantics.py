import json, tempfile, unittest
from pathlib import Path
from tests.cases import cases, malformed, BASE, query
from oracle.oracle import execute, digest_payload
from harness.generate import generate
class Semantics(unittest.TestCase):
    def test_cases(self):
        for name,fx,q,ids,checks in cases():
            with self.subTest(name=name):
                r=execute(fx,q);self.assertEqual(r['entities'],ids)
                if 'propositions' in checks:self.assertEqual(len(r['propositions']),checks['propositions'])
                if 'completeness' in checks:self.assertEqual(r['completeness']['entity_set'],checks['completeness'])
                for key in ['entities','relations','evidence']:fx[key].reverse()
                self.assertEqual(r,execute(fx,q))
                self.assertEqual(r,execute(json.loads(json.dumps(fx)),q))
    def test_malformed(self):
        for name,f,q in malformed():
            with self.subTest(name=name),self.assertRaises(Exception):execute(f,q)
    def test_golden(self):
        self.assertEqual(execute(BASE,json.loads(Path('tests/golden_query.json').read_text())),json.loads(Path('oracle/expected/golden_result.json').read_text()))
    def test_digest_is_semantic(self):
        r=execute(BASE,query(entity_id=2));p={k:r[k] for k in ['entities','propositions','knowledge','completeness']}
        self.assertEqual(r['digest'],digest_payload(p))
    def test_generator(self):
        with tempfile.TemporaryDirectory() as d:
            a,b=Path(d)/'a',Path(d)/'b';generate(a,10,40,17);generate(b,10,40,17)
            self.assertEqual(a.read_bytes(),b.read_bytes())
if __name__=='__main__':unittest.main()
