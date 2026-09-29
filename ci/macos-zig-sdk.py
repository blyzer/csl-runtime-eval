#!/usr/bin/env python3
"""Opt-in local workaround for Zig 0.14.1's arm64e-only SDK stub lookup.
Copies only link metadata, never system files or executable libraries.
"""
import argparse, platform, subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if platform.system()!='Darwin' or platform.machine()!='arm64':raise SystemExit('Only for macOS arm64')
parser=argparse.ArgumentParser();parser.add_argument('--sdk',help='Explicit installed SDK path; default: xcrun selection');args=parser.parse_args()
sdk=Path(args.sdk or subprocess.check_output(['/usr/bin/xcrun','--sdk','macosx','--show-sdk-path'],text=True).strip()).resolve()
stub=(sdk/'usr/lib/libSystem.tbd').read_text()
top=next(l for l in stub.splitlines() if l.startswith('targets:'))
if ' arm64-macos' in top or '[arm64-macos' in top:raise SystemExit('SDK already advertises arm64; workaround not needed')
if 'arm64e-macos' not in stub:raise SystemExit('Unrecognized SDK; no files modified')
out=ROOT/'.venv/zig-sdk';lib=out/'usr/lib';lib.mkdir(parents=True,exist_ok=True)
for item in (sdk/'usr/lib').iterdir():
    dest=lib/item.name
    if item.name=='libSystem.tbd':dest.write_text(stub.replace('arm64e-macos','arm64-macos'))
    elif not dest.exists():dest.symlink_to(item)
for rel in ['usr/include','System']:
    dest=out/rel
    if not dest.exists():dest.symlink_to(sdk/rel)
shim=ROOT/'.venv/bin/xcrun'
shim.write_text('''#!/usr/bin/env bash
if [[ "$*" == '--sdk macosx --show-sdk-path' ]]; then
  cd "$(dirname "$0")/../zig-sdk" && pwd
else
  exec /usr/bin/xcrun "$@"
fi
''');shim.chmod(0o755)
print(f'Local SDK link-metadata overlay: {out}; activate .venv to use. Original SDK unchanged.')
