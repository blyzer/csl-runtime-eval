"""Strategy C test seam only: validate immutable offset/length views.
No mapping, cross-process sharing, or measured speedup is implemented.
"""
from dataclasses import dataclass
import hashlib
@dataclass(frozen=True)
class SharedView:
    version: int
    snapshot: str
    offset: int
    length: int
    digest: str
    def read(self,region:bytes,expected_snapshot:str)->memoryview:
        if not isinstance(region,bytes):raise ValueError('region must be immutable')
        if self.version!=1 or self.snapshot!=expected_snapshot:raise ValueError('incompatible view')
        if self.offset<0 or self.length<0 or self.offset>len(region) or self.length>len(region)-self.offset:raise ValueError('view out of bounds')
        result=memoryview(region)[self.offset:self.offset+self.length]
        if 'sha256:'+hashlib.sha256(result).hexdigest()!=self.digest:raise ValueError('view digest mismatch')
        return result
