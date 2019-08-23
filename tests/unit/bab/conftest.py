# pylint: skip-file
import pytest
import pyomo.environ as pe
from galini.pyomo.convert import problem_from_pyomo_model
from galini.solvers.solution import OptimalObjective, Solution
from galini.branch_and_bound.node import NodeSolution


def create_problem():
    m = pe.ConcreteModel()
    m.I = range(5)
    m.x = pe.Var(m.I, bounds=(-1, 2))
    m.obj = pe.Objective(expr=sum(m.x[i] for i in m.I))
    return problem_from_pyomo_model(m)


@pytest.fixture()
def problem():
    return create_problem()


def create_solution(lb, ub):
    return NodeSolution(MockSolution(lb), MockSolution(ub))


class MockStatus(object):
    def is_success(self):
        return True

    def description(self):
        return 'Success'


class MockSolution(Solution):
    def __init__(self, obj):
        self.status = MockStatus()
        self.objective = OptimalObjective(name='obj', value=obj)
        self.variables = []


class MockSelectionStrategy:
    def insert_node(self, node):
        pass
