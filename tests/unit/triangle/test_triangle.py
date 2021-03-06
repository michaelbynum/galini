import pytest
import numpy as np
import pyomo.environ as aml
from galini.pyomo import problem_from_pyomo_model
from galini.galini import Galini
from galini.branch_and_cut.algorithm import BranchAndCutAlgorithm
from galini.branch_and_bound.relaxations import LinearRelaxation
from galini.special_structure import propagate_special_structure, perform_fbbt
from galini.core import Constraint
from galini.triangle.cuts_generator import TriangleCutsGenerator
from galini.solvers.solution import OptimalObjective, OptimalVariable, Solution, Status
from galini.timelimit import start_timelimit


class FakeSolver:
    name = 'branch_and_cut'
    config = {
        'obbt_simplex_maxiter': 100,
    }

class FakeStatus(Status):
    def is_success(self):
        return True

    def is_infeasible(self):
        return False

    def is_unbounded(self):
        return FakeSolver

    def description(self):
        return ''


def _linear_relaxation(problem):
    bounds = perform_fbbt(
        problem,
        maxiter=10,
        timelimit=60,
    )

    bounds, monotonicity, convexity = \
        propagate_special_structure(problem, bounds)
    return LinearRelaxation(
        problem, bounds, monotonicity, convexity,
        disable_mccormick_midpoint=True
    )


@pytest.fixture()
def problem():
    Q = [[28.0, 23.0, 0.0, 0.0, 0.0, 2.0, 0.0, 24.0],
         [23.0, 0.0, -23.0, -44.0, 10.0, 0.0, 7.0, -7.0],
         [0.0, -23.0, 18.0, 41.0, 0.0, -3.0, -5.0, 2.0],
         [0.0, -44.0, 41.0, -5.0, 5.0, -1.0, 16.0, -50.0],
         [0.0, 10.0, 0.0, 5.0, 0.0, -2.0, -4.0, 21.0],
         [2.0, 0.0, -3.0, -1.0, -2.0, 34.0, -9.0, 20.0],
         [0.0, 7.0, -5.0, 16.0, -4.0, -9.0, 0.0, 0.0],
         [24.0, -7.0, 2.0, -50.0, 21.0, 20.0, 0.0, -45.0]]

    C = [-44, -48, 10, 45, 0, 2, 3, 4, 5]

    Qc = [
        [-28, 13, 5],
        [13, 0, 0],
        [0, 0, 0],
    ]

    m = aml.ConcreteModel("model_1")
    m.I = range(8)
    m.x = aml.Var(m.I, bounds=(0, 1))
    m.f = aml.Objective(
        expr=sum(-Q[i][j] * m.x[i] * m.x[j] for i in m.I for j in m.I) + sum(-C[i] * m.x[i] for i in m.I))
    m.c = aml.Constraint(expr=sum(Qc[i][j] * m.x[i] * m.x[j] for i in m.I[0:3] for j in m.I[0:3]) >= -10)

    return problem_from_pyomo_model(m)


@pytest.fixture
def galini():
    galini_ = Galini()
    galini_.update_configuration({
        'cuts_generator': {
            'generators': ['triangle'],
            'triangle': {
                'domain_eps': 1e-3,
                'thres_triangle_viol': 1e-7,
                'max_tri_cuts_per_round': 10e3,
                'selection_size': 2,
                'min_tri_cuts_per_round': 0,
            },
        }
    })
    return galini_


def test_adjacency_matrix(galini, problem):
    solver_ipopt = galini.instantiate_solver('ipopt')
    solver_mip = galini.instantiate_solver("mip")

    run_id = 'test_run'
    # Test adjacency matrix
    start_timelimit()
    triangle_cuts_gen = TriangleCutsGenerator(galini, galini._config.cuts_generator.triangle)
    triangle_cuts_gen.before_start_at_root(run_id, problem, None)
    assert (np.allclose(triangle_cuts_gen._get_adjacency_matrix(problem),
                        [
                            [1, 1, 1, 0, 0, 1, 0, 1],
                            [1, 0, 1, 1, 1, 0, 1, 1],
                            [1, 1, 1, 1, 0, 1, 1, 1],
                            [0, 1, 1, 1, 1, 1, 1, 1],
                            [0, 1, 0, 1, 0, 1, 1, 1],
                            [1, 0, 1, 1, 1, 1, 1, 1],
                            [0, 1, 1, 1, 1, 1, 0, 0],
                            [1, 1, 1, 1, 1, 1, 0, 1]
                        ]))


def test_triange_cut_violations(galini, problem):
    run_id = 'test_run'
    start_timelimit()
    triangle_cuts_gen = TriangleCutsGenerator(galini, galini._config.cuts_generator.triangle)
    triangle_cuts_gen.before_start_at_root(run_id, problem, None)

    # Test triangle cut violations
    relaxation = _linear_relaxation(problem)
    relaxed_problem = relaxation.relax(problem)
    known_solution = {
        'x[0]': 0.5,
        'x[1]': 0.5,
        'x[2]': 0.5,
        'x[3]': 0.5,
        'x[4]': 0.5,
        'x[5]': 1.0,
        'x[6]': 0.5,
        'x[7]': 0.5,
        '_objvar': -2.00,
        '_aux_bilinear_x[4]_x[7]': 0.5,
        '_aux_bilinear_x[5]_x[6]': 0.5,
        '_aux_bilinear_x[3]_x[7]': 0.0,
        '_aux_bilinear_x[5]_x[7]': 0.5,
        '_aux_bilinear_x[3]_x[6]': 0.5,
        '_aux_bilinear_x[0]_x[7]': 0.5,
        '_aux_bilinear_x[1]_x[3]': 0.0,
        '_aux_bilinear_x[3]_x[4]': 0.5,
        '_aux_bilinear_x[1]_x[2]': 0.0,
        '_aux_bilinear_x[0]_x[5]': 0.5,
        '_aux_bilinear_x[0]_x[0]': 0.5,
        '_aux_bilinear_x[0]_x[1]': 0.5,
        '_aux_bilinear_x[1]_x[4]': 0.5,
        '_aux_bilinear_x[3]_x[3]': 0.0,
        '_aux_bilinear_x[7]_x[7]': 0.0,
        '_aux_bilinear_x[3]_x[5]': 0.5,
        '_aux_bilinear_x[1]_x[6]': 0.5,
        '_aux_bilinear_x[5]_x[5]': 1.0,
        '_aux_bilinear_x[1]_x[7]': 0.0,
        '_aux_bilinear_x[2]_x[2]': 0.5,
        '_aux_bilinear_x[2]_x[3]': 0.5,
        '_aux_bilinear_x[4]_x[6]': 0.0,
        '_aux_bilinear_x[2]_x[5]': 0.5,
        '_aux_bilinear_x[4]_x[5]': 0.5,
        '_aux_bilinear_x[2]_x[6]': 0.0,
        '_aux_bilinear_x[2]_x[7]': 0.5,
        '_aux_bilinear_x[0]_x[2]': 0.5,
    }
    mip_solution = Solution(
        status=FakeStatus(),
        optimal_obj=OptimalObjective('f', -200.0),
        optimal_vars=[OptimalVariable(v.name, known_solution[v.name]) for v in relaxed_problem.variables]
    )
    triangle_viol = triangle_cuts_gen._get_triangle_violations(relaxed_problem, mip_solution)
    expected_triangle_viol = [
        [0, 0, 0.5], [0, 1, -0.5], [0, 2, -0.5], [0, 3, -0.5], [1, 0, 0.5], [1, 1, -0.5], [1, 2, -0.5],
        [1, 3, -0.5], [2, 0, 0.0], [2, 1, 0.0], [2, 2, -0.5], [2, 3, -0.5], [3, 0, 0.0], [3, 1, 0.0],
        [3, 2, 0.0], [3, 3, -1.0], [4, 0, 0.0], [4, 1, -0.5], [4, 2, 0.0], [4, 3, -0.5], [5, 0, -1.0],
        [5, 1, 0.0], [5, 2, 0.0], [5, 3, 0.0], [6, 0, 0.0], [6, 1, -1.0], [6, 2, 0.0], [6, 3, 0.0],
        [7, 0, -1.0], [7, 1, 0.0], [7, 2, 0.0], [7, 3, 0.0], [8, 0, -0.5], [8, 1, -0.5], [8, 2, 0.5],
        [8, 3, -0.5], [9, 0, -0.5], [9, 1, -0.5], [9, 2, 0.5], [9, 3, -0.5], [10, 0, -0.5],
        [10, 1, -0.5], [10, 2, -0.5], [10, 3, 0.5], [11, 0, 0.5], [11, 1, -0.5], [11, 2, -0.5],
        [11, 3, -0.5], [12, 0, -0.5], [12, 1, 0.5], [12, 2, -0.5], [12, 3, -0.5], [13, 0, 0.0],
        [13, 1, 0.0], [13, 2, -0.5], [13, 3, -0.5], [14, 0, -0.5], [14, 1, 0.5], [14, 2, -0.5],
        [14, 3, -0.5], [15, 0, 0.5], [15, 1, -0.5], [15, 2, -0.5], [15, 3, -0.5], [16, 0, -0.5],
        [16, 1, 0.0], [16, 2, -0.5], [16, 3, 0.0], [17, 0, 0.0], [17, 1, -0.5], [17, 2, 0.0],
        [17, 3, -0.5], [18, 0, 0.0], [18, 1, 0.0], [18, 2, -0.5], [18, 3, -0.5], [19, 0, 0.5],
        [19, 1, -0.5], [19, 2, -0.5], [19, 3, -0.5], [20, 0, -0.5], [20, 1, 0.5], [20, 2, -0.5],
        [20, 3, -0.5], [21, 0, 0.0], [21, 1, -0.5], [21, 2, 0.0], [21, 3, -0.5], [22, 0, -0.5],
        [22, 1, 0.0], [22, 2, -0.5], [22, 3, 0.0], [23, 0, -0.5], [23, 1, 0.0], [23, 2, -0.5],
        [23, 3, 0.0], [24, 0, 0.0], [24, 1, -0.5], [24, 2, 0.0], [24, 3, -0.5]]
    assert len(triangle_viol) == len(expected_triangle_viol)
    for actual, expected in zip(triangle_viol, expected_triangle_viol):
        assert np.allclose(actual, expected)


def test_at_root_node(galini, problem):
    solver_ipopt = galini.instantiate_solver('ipopt')
    solver_mip = galini.instantiate_solver("mip")
    run_id = 'test_run'

    # Test at root node
    start_timelimit()
    algo = BranchAndCutAlgorithm(galini, FakeSolver(), telemetry=None)
    algo._cuts_generators_manager.before_start_at_root(run_id, problem, None)
    relaxation = _linear_relaxation(problem)
    relaxed_problem = relaxation.relax(problem)
    nbs_cuts = []
    mip_sols = []
    for iteration in range(5):
        mip_solution = solver_mip.solve(relaxed_problem, logger=None)
        assert mip_solution.status.is_success()
        mip_sols.append(mip_solution.objective.value)
        # Generate new cuts
        new_cuts = algo._cuts_generators_manager.generate(run_id, problem, None, relaxed_problem, mip_solution, None, None)
        # Add cuts as constraints
        nbs_cuts.append(len(list(new_cuts)))
        for cut in new_cuts:
            new_cons = Constraint(cut.name, cut.expr, cut.lower_bound, cut.upper_bound)
            relaxation._relax_constraint(problem, relaxed_problem, new_cons)
    assert (np.allclose(mip_sols, [-200.0, -196.85714285714283, -196.5, -196.0, -196.0]))
    assert (nbs_cuts == [2, 2, 2, 0, 0])

    # Test when branched on x0 in [0.5, 1]
    x0 = problem.variable_view(problem.variables[0])
    x0.set_lower_bound(0.5)
    relaxed_problem = relaxation.relax(problem)
    algo._cuts_generators_manager.before_start_at_node(run_id, problem, None)
    mip_sols = []
    mip_solution = None
    for iteration in range(5):
        mip_solution = solver_mip.solve(relaxed_problem, logger=None)
        assert mip_solution.status.is_success()
        mip_sols.append(mip_solution.objective.value)
        # Generate new cuts
        new_cuts = algo._cuts_generators_manager.generate(run_id, problem, None, relaxed_problem, mip_solution, None, None)
        # Add cuts as constraints
        for cut in new_cuts:
            new_cons = Constraint(cut.name, cut.expr, cut.lower_bound, cut.upper_bound)
            relaxation._relax_constraint(problem, relaxed_problem, new_cons)
    assert(np.allclose(mip_sols,
           [-193.88095238095238, -187.96808510638297, -187.42857142857147, -187.10869565217394, -187.10869565217394]))
