import pytest
import numpy as np
from galini.core import (
    Problem,
    Variable,
    Constant,
    Domain,
    SumExpression,
    ProductExpression,
    Constraint,
    JacobianEvaluator,
    HessianEvaluator,
)


@pytest.fixture
def problem():
    return Problem()

def test_problem_creation(problem):
    assert problem.size == 0
    assert problem.max_depth() == 0


def test_insert_variable(problem):
    problem.add_variable('x', None, None, Domain.REALS)
    assert problem.max_depth() == 0


def test_constants_minimum_depth(problem):
    problem.insert_vertex(Constant(1.234))
    assert problem.max_depth() == 1


def test_vertex_index_is_updated(problem):
    cs = [Constant(i) for i in range(10)]
    for c in cs:
        problem.insert_vertex(c)
    assert problem.max_depth() == 1
    assert problem.size == 10
    for i, c in enumerate(cs):
        assert c.idx == i


def test_children_are_updated(problem):
    c0 = Constant(1.234)
    v0 = problem.add_variable('x', None, None, Domain.REALS)
    problem.insert_vertex(c0)
    s0 = SumExpression([c0.idx, v0.idx])
    problem.insert_vertex(s0)
    assert s0.idx == 2
    assert s0.nth_children(0) == 1
    assert s0.nth_children(1) == 0
    problem.add_variable('y', None, None, Domain.REALS)
    assert s0.idx == 3
    assert s0.nth_children(0) == 2
    assert s0.nth_children(1) == 0
    assert problem.vertex_depth(s0.idx) == 2
    assert problem.vertex_depth(c0.idx) == 1
    assert problem.size == 4


def test_eval_at_x(problem):
    x = problem.add_variable('x', None, None, Domain.REALS)
    y = problem.add_variable('y', None, None, Domain.REALS)
    z = problem.add_variable('z', None, None, Domain.REALS)
    s0 = SumExpression([0, 1])
    problem.insert_vertex(s0)
    p1 = ProductExpression([s0.idx, z.idx])
    problem.insert_vertex(p1)
    c0 = problem.add_constraint('c0', p1, None, None)
    c1 = problem.add_constraint('c1', s0, None, None)
    h = HessianEvaluator(problem)
    h.eval_at_x(np.array([1.0, 2.0, 3.0]), True)
    print('jacobian')
    print(h.jacobian)
    print('hessian')
    print(h.hessian)
    assert False
