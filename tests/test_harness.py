import unittest
from unittest.mock import patch
from harness.benchctl import available,environment,invoke
class Harness(unittest.TestCase):
    def test_missing_tool_is_not_pass(self):
        with patch('harness.benchctl.tool',return_value=None):
            self.assertFalse(available('hybrid'))
            self.assertTrue(all(v['state']=='NOT AVAILABLE' for v in environment()['tools'].values()))
    def test_process_exit_and_metrics(self):
        import sys
        code,out,err,ns,rss=invoke([sys.executable,'-c','import sys;print("{}",end="");sys.exit(7)'])
        self.assertEqual(code,7);self.assertEqual(out,b'{}');self.assertEqual(err,'');self.assertGreater(ns,0)
        if rss is not None:self.assertGreater(rss,0)

class Profiles(unittest.TestCase):
    def envelope(self):
        from oracle.oracle import execute
        from tests.cases import BASE, query
        return {'profile_schema':'csl.eval.profile/v0.1','representation':'typed-hash-v1',
                'phases_ns':{'load':1,'index':2,'query':3,'result':4},'result':execute(BASE,query(entity_id=1))}

    def test_profile_validation(self):
        from copy import deepcopy
        from harness.benchctl import unpack_profile
        envelope=self.envelope()
        actual,metadata=unpack_profile(envelope,10)
        self.assertEqual(actual,envelope['result'])
        self.assertEqual(metadata['phases_ns'],envelope['phases_ns'])
        for key,value in [('profile_schema','v9'),('representation','unknown'),('result',{}),('phases_ns',{}),('extra',True)]:
            bad={**envelope,key:value}
            with self.subTest(key=key),self.assertRaises(Exception):unpack_profile(bad,100)
        for value in [-1,True,1.5,101]:
            bad=deepcopy(envelope);bad['phases_ns']['query']=value
            with self.subTest(duration=value),self.assertRaises(Exception):unpack_profile(bad,100)
        with self.assertRaises(ValueError):unpack_profile(envelope,9)

    def test_phase_detail_consistency(self):
        from harness.benchctl import unpack_profile
        envelope={**self.envelope(),'phase_detail_ns':{'decode':1,'construct':0,'materialize':1,'encode':3}}
        actual,metadata=unpack_profile(envelope,10)
        self.assertEqual(metadata['phase_detail_ns'],envelope['phase_detail_ns'])
        for bad_detail in [{'decode':2,'construct':0,'materialize':1,'encode':3},
                           {'decode':1,'construct':0,'materialize':1,'encode':4}]:
            bad={**envelope,'phase_detail_ns':bad_detail}
            with self.subTest(detail=bad_detail),self.assertRaises(ValueError):unpack_profile(bad,10)

    def test_phase_subdetail_consistency(self):
        from harness.benchctl import unpack_profile
        base={**self.envelope(),'phase_detail_ns':{'decode':1,'construct':0,'materialize':1,'encode':3}}
        envelope={**base,'phase_subdetail_ns':{'read':1,'parse':0,'entities':1,'adjacency':1,'sort':0}}
        actual,metadata=unpack_profile(envelope,10)
        self.assertEqual(metadata['phase_subdetail_ns'],envelope['phase_subdetail_ns'])
        with self.assertRaisesRegex(ValueError,'phase_detail_ns'):
            unpack_profile({k:v for k,v in envelope.items() if k!='phase_detail_ns'},10)
        for bad_sub in [{'read':2,'parse':0,'entities':1,'adjacency':1,'sort':0},
                        {'read':1,'parse':0,'entities':2,'adjacency':1,'sort':0}]:
            bad={**envelope,'phase_subdetail_ns':bad_sub}
            with self.subTest(sub=bad_sub),self.assertRaises(ValueError):unpack_profile(bad,10)

    def test_result_mismatch_is_never_accepted(self):
        from harness.benchctl import benchmark
        from pathlib import Path
        from tempfile import TemporaryDirectory
        envelope=self.envelope();envelope['result']['snapshot']='WRONG'
        with TemporaryDirectory() as directory:
            root=Path(directory)
            with patch('harness.benchctl.ROOT',root),patch('harness.benchctl.available',return_value=True),patch('harness.benchctl.executable',return_value=Path(__file__)),patch('harness.benchctl.process',return_value=(envelope,100,1000)),patch('harness.benchctl.source_digest',return_value='test'):
                with self.assertRaisesRegex(RuntimeError,'measurement rejected'):
                    benchmark('W1','rust',str(Path('tests/golden_fixture.json').resolve()),1,profile=True)
            self.assertTrue((root/'results/phases/w1/failure-rust.json').exists())
            self.assertFalse((root/'results/phases/w1/rust-golden_fixture.json').exists())

    def test_hybrid_profile_is_rejected(self):
        from harness.benchctl import benchmark
        with self.assertRaisesRegex(ValueError,'pure Rust/Zig'):
            benchmark('W1','hybrid','SMOKE',1,profile=True)
