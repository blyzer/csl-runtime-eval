"""Shared plumbing for the Gate #1 persistence and mutation workloads (W5.S1, W8.S1)."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness.benchctl import environment, executable, source_digest
from harness.campaign import condition_failures, snapshot as machine_snapshot
from harness.generate import generate

SEED = 20260928           # the S corpus every earlier S pass used (digest 7ea1c788...)
CORPUS = ROOT / 'corpus/synthetic/S-campaign-mixed-20260928.json'
S_DIGEST = '7ea1c7884968206fff29bfd3f0a0102018df2e82cd6377061870b18ab369c4d5'


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def file_digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def ensure_corpus(preset='S'):
    """Regenerate the deterministic S corpus (same bytes as every earlier S pass)."""
    from harness.generate import PRESETS
    n, m = PRESETS[preset]
    path = CORPUS if preset == 'S' else ROOT / 'corpus/synthetic' / f'{preset}-campaign-mixed-{SEED}.json'
    if preset == 'S' and path.exists() and file_digest(path) == S_DIGEST:
        return path
    generate(path, n, m, SEED, 'mixed')
    if preset == 'S' and file_digest(path) != S_DIGEST:
        raise ValueError('regenerated S corpus does not match the digest used by every earlier S pass')
    return path


def preflight(exploratory, max_load, min_memory_gib):
    env = environment()
    machine = machine_snapshot()
    failures = condition_failures(machine, max_load, min_memory_gib * 1024**3)
    if env['sdk_workaround']:
        failures.append('project SDK overlay is present; build provenance is not native')
    classification = 'exploratory' if exploratory else 'controlled'
    return env, machine, failures, classification


def sample_conditions(max_load, min_memory_gib):
    """Machine state to bracket one sample: returns (snapshot, failures) computed by the caller later."""
    return machine_snapshot()


def failures_between(before, after, max_load, min_memory_gib):
    out = condition_failures(before, max_load, min_memory_gib * 1024**3) + condition_failures(after, max_load, min_memory_gib * 1024**3)
    if before['swapout_pages'] is not None and after['swapout_pages'] is not None and after['swapout_pages'] > before['swapout_pages']:
        out.append('swapout counter increased during sample')
    return sorted(set(out))


def rotated_orders(candidates, repeat, seed):
    """Deterministic order: seeded base order, rotated by one position per repeat."""
    rng = random.Random(seed)
    base = list(candidates)
    rng.shuffle(base)
    n = len(base)
    return [base[i % n:] + base[:i % n] for i in range(repeat)]


def median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def bootstrap_ratio_ci(numerators, denominators, seed=20260929, resamples=10_000):
    """Paired bootstrap of the median ratio numerators[i] / denominators[i] (preregistered method)."""
    ratios = [a / b for a, b in zip(numerators, denominators) if b]
    if len(ratios) < 2:
        return None
    rng = random.Random(seed)
    n = len(ratios)
    meds = sorted(statistics.median(ratios[rng.randrange(n)] for _ in range(n)) for _ in range(resamples))
    return {'median': statistics.median(ratios), 'ci95': [meds[int(.025 * resamples)], meds[int(.975 * resamples) - 1]], 'pairs': n}
