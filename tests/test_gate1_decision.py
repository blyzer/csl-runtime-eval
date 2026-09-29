"""The preregistered decision framework: verdict rules, minimum pairs and Pareto dominance."""
import unittest

from harness import gate1_decision as g


def cells(hybrid, zig, rust=None, dim='latency', n=40):
    by = {'hybrid': dict(enumerate(hybrid(n))), 'zig': dict(enumerate(zig(n)))}
    if rust:
        by['rust'] = dict(enumerate(rust(n)))
    return {(dim, 'cell'): by}


flat = lambda v: (lambda n: [v * (1 + 0.01 * (i % 5)) for i in range(n)])


class Decision(unittest.TestCase):
    def test_verdicts_follow_the_thresholds(self):
        self.assertEqual(g.compare(cells(flat(80), flat(100)))[0]['verdict'], 'better')       # ratio .80 <= .90
        self.assertEqual(g.compare(cells(flat(95), flat(100)))[0]['verdict'], 'equivalent')
        self.assertEqual(g.compare(cells(flat(120), flat(100)))[0]['verdict'], 'worse')
        self.assertEqual(g.compare(cells(flat(88), flat(100), dim='memory'))[0]['verdict'], 'equivalent')   # memory needs <= .85
        self.assertEqual(g.compare(cells(flat(84), flat(100), dim='memory'))[0]['verdict'], 'better')

    def test_too_few_pairs_is_never_judged(self):
        row = g.compare(cells(flat(50), flat(100), n=10))[0]
        self.assertEqual(row['verdict'], 'insufficient')
        self.assertIn('insufficient', row['note'])

    def test_comparator_is_the_best_other_candidate(self):
        row = g.compare(cells(flat(90), flat(100), rust=flat(70)))[0]
        self.assertEqual(row['best_other'], 'rust')
        self.assertEqual(row['verdict'], 'worse')

    def test_pareto_dominance_needs_no_worse_dimension(self):
        c = cells(flat(120), flat(100), rust=flat(80))
        table = g.pareto(c, {'boundary_tax': False, 'ownership': False, 'complexity': True})
        self.assertTrue(table['rust vs hybrid']['dominates'])
        self.assertFalse(table['hybrid vs rust']['dominates'])
        self.assertEqual(table['hybrid vs zig']['verdicts']['complexity'], 'worse')

    def test_hybrid_costs_are_reported_from_the_boundary_block(self):
        records = [{'candidate': 'hybrid', 'boundary': {'boundary_ns_total': 10, 'kernel_ns_total': 90, 'wrapper_ns_total': 0, 'copy_bytes': 5},
                    'heap': {'live_heap_bytes': 100}}]
        costs = g.boundary_costs(records)
        self.assertTrue(costs['boundary_tax_worse'])                 # 10% >= 5%
        self.assertFalse(costs['ownership_worse'])                   # 5% < 10%


if __name__ == '__main__':
    unittest.main()
