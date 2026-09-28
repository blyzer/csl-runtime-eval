"""Implementation-neutral structural and cross-reference validation."""
import json
from pathlib import Path
import jsonschema
ROOT = Path(__file__).resolve().parents[1]
VALIDATORS = {name: jsonschema.Draft202012Validator(json.loads((ROOT / f'schemas/{name}.schema.json').read_text())) for name in ('fixture', 'query', 'result', 'benchmark-record')}

def validate_fixture(fx):
    VALIDATORS['fixture'].validate(fx)
    ids = {e['id'] for e in fx['entities']}
    if len(ids) != len(fx['entities']):
        raise ValueError('duplicate entity id')
    for e in fx['entities']:
        if e['name_sid'] >= len(fx['strings']) or (e.get('container') is not None and e['container'] not in ids):
            raise ValueError('invalid entity reference')
    for e in fx['relations'] + fx['evidence']:
        if e['subject'] not in ids or e['object'] not in ids:
            raise ValueError('invalid entity reference')

def validate_query(q):
    VALIDATORS['query'].validate(q)
