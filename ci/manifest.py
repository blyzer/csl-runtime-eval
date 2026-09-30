#!/usr/bin/env python3
"""Evaluation-source manifest. Deliberately excludes generated evidence/builds."""
import argparse, hashlib, os
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
DIRECTORIES=('schemas','oracle','harness','tests','ci','prototypes','adr','docs','.github/workflows')
SKIP={'.git','.venv','.zig-cache','zig-out','target','__pycache__'}
SUFFIXES={'.py','.rs','.zig','.json','.sh','.md','.toml','.lock','.h','.c','.yml'}
def entries():
    files={ROOT/p for p in ['.gitignore','README.md','STATUS.md','TOOLCHAINS.md','justfile','requirements.txt','corpus/synthetic/SMOKE.json']}
    for directory in DIRECTORIES:
        for base,dirs,names in os.walk(ROOT/directory):
            dirs[:]=sorted(d for d in dirs if d not in SKIP)
            files.update(Path(base)/n for n in names if Path(n).suffix in SUFFIXES)
    return ''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(ROOT).as_posix()}\n' for p in sorted(files))
def main():
    p=argparse.ArgumentParser();p.add_argument('--write',action='store_true');a=p.parse_args();manifest=ROOT/'MANIFEST.sha256';expected=entries()
    if a.write:manifest.write_text(expected);print('Manifest regenerated');return
    if not manifest.exists() or manifest.read_text()!=expected:raise SystemExit('FAIL manifest: source file set or content changed; regenerate with python3 ci/manifest.py --write')
    print(f'PASS manifest: {len(expected.splitlines())} files')
if __name__=='__main__':main()
