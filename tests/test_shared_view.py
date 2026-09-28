import hashlib,unittest
from harness.shared_view import SharedView
class SharedViews(unittest.TestCase):
    def test_lifetime_and_bounds(self):
        data=b'headerpayload';v=SharedView(1,'S1',6,7,'sha256:'+hashlib.sha256(b'payload').hexdigest())
        view=v.read(data,'S1');del data;self.assertEqual(bytes(view),b'payload');self.assertTrue(view.readonly)
        for region,snapshot in [(b'short','S1'),(b'headerpayload','S2'),(bytearray(b'headerpayload'),'S1'),(b'headercorrupt','S1')]:
            with self.assertRaises(ValueError):v.read(region,snapshot)
    def test_empty(self):
        v=SharedView(1,'S',0,0,'sha256:'+hashlib.sha256(b'').hexdigest());self.assertEqual(bytes(v.read(b'','S')),b'')
