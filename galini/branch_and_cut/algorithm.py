#  Copyright 2019 Francesco Ceccon
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.


"""Branch & Cut algorithm."""

import datetime

import numpy as np
import pyomo.environ as pe
import coramin.domain_reduction.obbt as coramin_obbt
from coramin.relaxations.auto_relax import relax
from pyomo.core.expr.current import identify_variables
from pyomo.core.kernel.component_set import ComponentSet
from suspect.expression import ExpressionType
from suspect.interval import Interval

from galini.branch_and_bound.node import NodeSolution
from galini.branch_and_bound.relaxations import (
    ConvexRelaxation, LinearRelaxation
)
from galini.branch_and_bound.selection import BestLowerBoundSelectionStrategy
from galini.branch_and_bound.strategy import KSectionBranchingStrategy
from galini.branch_and_cut.primal import (
    solve_primal, solve_primal_with_starting_point
)
from galini.config import (
    OptionsGroup,
    IntegerOption,
    BoolOption,
)
import galini.core as core
from galini.logging import get_logger, DEBUG
from galini.math import mc, is_close, almost_ge, almost_le, is_inf
from galini.quantities import relative_gap, absolute_gap
from galini.relaxations.relaxed_problem import RelaxedProblem
from galini.cuts.generator import Cut, CutType
from galini.special_structure import (
    propagate_special_structure,
    perform_fbbt,
)
from galini.timelimit import (
    seconds_left,
    current_time,
    seconds_elapsed_since,
    timeout,
)
from galini.util import expr_to_str

logger = get_logger(__name__)

coramin_logger = coramin_obbt.logger # pylint: disable=invalid-name
coramin_logger.disabled = True


class CutsState:
    """Cut loop state."""
    def __init__(self):
        self.round = 0
        self.lower_bound = -np.inf
        self.previous_previous_solution = None
        self.latest_solution = None
        self.previous_solution = None

    def update(self, solution, paranoid=False, atol=None, rtol=None):
        """Update cut state with `solution`."""
        self.round += 1
        current_objective = solution.objective.value
        if paranoid:
            close = is_close(
                current_objective, self.lower_bound, atol=atol, rtol=rtol
            )
            increased = (
                current_objective >= self.lower_bound or
                close
            )
            if not increased:
                msg = 'Lower bound in cuts phase decreased: {} to {}'
                raise RuntimeError(
                    msg.format(self.lower_bound, current_objective)
                )

        self.lower_bound = current_objective
        self.previous_previous_solution = self.previous_solution
        self.previous_solution = self.latest_solution
        self.latest_solution = current_objective

    def __str__(self):
        return 'CutsState(round={}, lower_bound={})'.format(
            self.round, self.lower_bound
        )

    def __repr__(self):
        return '<{} at {}>'.format(str(self), hex(id(self)))


# pylint: disable=too-many-instance-attributes
class BranchAndCutAlgorithm:
    """Branch and Cut algorithm."""
    name = 'branch_and_cut'

    def __init__(self, galini, solver, telemetry):
        self.galini = galini
        self.solver = solver
        self._nlp_solver = galini.instantiate_solver('ipopt')
        self._mip_solver = galini.instantiate_solver('mip')
        self._cuts_generators_manager = galini.cuts_generators_manager
        self._bac_telemetry = telemetry

        bab_config = galini.get_configuration_group(solver.name)

        self.tolerance = bab_config['tolerance']
        self.relative_tolerance = bab_config['relative_tolerance']
        self.node_limit = bab_config['node_limit']
        self.fbbt_maxiter = bab_config['fbbt_maxiter']
        self.fbbt_timelimit = bab_config['fbbt_timelimit']
        self.root_node_feasible_solution_seed = \
            bab_config['root_node_feasible_solution_seed']

        self.root_node_feasible_solution_search_timelimit = \
            bab_config['root_node_feasible_solution_search_timelimit']

        bac_config = galini.get_configuration_group('branch_and_cut.cuts')
        self.cuts_maxiter = bac_config['maxiter']

        self._use_lp_cut_phase = bac_config['use_lp_cut_phase']
        self._use_milp_cut_phase = bac_config['use_milp_cut_phase']

        if not self._use_lp_cut_phase and not self._use_milp_cut_phase:
            raise RuntimeError('One of LP or MILP cut phase must be active')

        self.branching_strategy = KSectionBranchingStrategy(2)
        self.node_selection_strategy = BestLowerBoundSelectionStrategy()

        self._user_model = None
        self._bounds = None
        self._monotonicity = None
        self._convexity = None

    # pylint: disable=line-too-long
    @staticmethod
    def algorithm_options():
        """Return options for BranchAndCutAlgorithm"""
        return OptionsGroup('cuts', [
            IntegerOption('maxiter', default=20, description='Number of cut rounds'),
            BoolOption('use_lp_cut_phase', default=True, description='Solve LP in cut loop'),
            BoolOption('use_milp_cut_phase', default=False, description='Add additional cut loop solving MILP')
        ])

    def _perform_obbt(self, model, problem):
        obbt_timelimit = self.solver.config['obbt_timelimit']
        obbt_start_time = current_time()

        for var in model.component_data_objects(ctype=pe.Var):
            var.domain = pe.Reals

            if not (var.lb is None or np.isfinite(var.lb)):
                var.setlb(None)

            if not (var.ub is None or np.isfinite(var.ub)):
                var.setub(None)

        relaxed_model = relax(model)

        for obj in relaxed_model.component_data_objects(ctype=pe.Objective):
            relaxed_model.del_component(obj)

        solver = pe.SolverFactory('cplex_persistent')
        solver.set_instance(relaxed_model)
        obbt_simplex_maxiter = self.solver.config['obbt_simplex_maxiter']
        # TODO(fra): make this non-cplex specific
        simplex_limits = solver._solver_model.parameters.simplex.limits # pylint: disable=protected-access
        simplex_limits.iterations.set(obbt_simplex_maxiter)
        # collect variables in nonlinear constraints
        nonlinear_variables = ComponentSet()
        for constraint in model.component_data_objects(ctype=pe.Constraint):
            # skip linear constraint
            if constraint.body.polynomial_degree() == 1:
                continue

            for var in identify_variables(constraint.body,
                                          include_fixed=False):
                # Coramin will complain about variables that are fixed
                # Note: Coramin uses an hard-coded 1e-6 tolerance
                if var.lb is None or var.ub is None:
                    nonlinear_variables.add(var)
                else:
                    if not var.ub - var.lb < 1e-6:
                        nonlinear_variables.add(var)

        relaxed_vars = [
            getattr(relaxed_model, v.name)
            for v in nonlinear_variables
        ]

        logger.info(0, 'Performing OBBT on {} variables', len(relaxed_vars))

        time_left = obbt_timelimit - seconds_elapsed_since(obbt_start_time)
        with timeout(time_left, 'Timeout in OBBT'):
            result = coramin_obbt.perform_obbt(
                relaxed_model, solver, relaxed_vars
            )

        if result is None:
            return

        logger.debug(0, 'New Bounds')
        for v, new_lb, new_ub in zip(relaxed_vars, *result):
            vv = problem.variable_view(v.name)
            if new_lb is None or new_ub is None:
                logger.warning(0, 'Could not tighten variable {}', v.name)
            old_lb = vv.lower_bound()
            old_ub = vv.upper_bound()
            new_lb = _safe_lb(vv.domain, new_lb, old_lb)
            new_ub = _safe_ub(vv.domain, new_ub, old_ub)
            if not new_lb is None and not new_ub is None:
                if is_close(new_lb, new_ub, atol=mc.epsilon):
                    if old_lb is not None and \
                            is_close(new_lb, old_lb, atol=mc.epsilon):
                        new_ub = new_lb
                    else:
                        new_lb = new_ub
            vv.set_lower_bound(new_lb)
            vv.set_upper_bound(new_ub)
            logger.debug(
                0, '  {}: [{}, {}]',
                v.name, vv.lower_bound(), vv.upper_bound()
            )

    def _before_root_node(self, problem):
        if self._user_model is None:
            raise RuntimeError("No user model. Did you call 'before_solve'?")

        model = self._user_model
        obbt_start_time = current_time()
        try:
            self._perform_obbt(model, problem)
            self._bac_telemetry.increment_obbt_time(
                seconds_elapsed_since(obbt_start_time)
            )
        except TimeoutError:
            logger.info(0, 'OBBT timed out')
            self._bac_telemetry.increment_obbt_time(
                seconds_elapsed_since(obbt_start_time)
            )
            return

        except Exception as ex:
            logger.warning(0, 'Error performing OBBT: {}', ex)
            self._bac_telemetry.increment_obbt_time(
                seconds_elapsed_since(obbt_start_time)
            )
            raise

    # pylint: disable=too-many-branches
    def before_solve(self, model, problem):
        """Callback called before solve."""
        self._user_model = model

    def _has_converged(self, state):
        rel_gap = relative_gap(state.lower_bound, state.upper_bound)
        abs_gap = absolute_gap(state.lower_bound, state.upper_bound)

        bounds_close = is_close(
            state.lower_bound,
            state.upper_bound,
            rtol=self.relative_tolerance,
            atol=self.tolerance,
        )

        if self.galini.paranoid_mode:
            assert (state.lower_bound <= state.upper_bound or bounds_close)

        return (
            rel_gap <= self.relative_tolerance or
            abs_gap <= self.tolerance
        )

    def _cuts_converged(self, state):
        return self._cuts_generators_manager.has_converged(state)

    def _cuts_iterations_exceeded(self, state):
        return state.round > self.cuts_maxiter

    def cut_loop_should_terminate(self, state):
        return (
            self._cuts_converged(state) or
            self._cuts_iterations_exceeded(state) or
            self._timeout()
        )

    def _node_limit_exceeded(self, state):
        return state.nodes_visited > self.node_limit

    def _timeout(self):
        return seconds_left() <= 0

    def should_terminate(self, state):
        return (
            self._has_converged(state) or
            self._node_limit_exceeded(state) or
            self._timeout()
        )

    # pylint: disable=too-many-branches
    def _solve_problem_at_node(self, run_id, problem, relaxed_problem,
                               tree, node):
        logger.info(
            run_id,
            'Starting Cut generation iterations. Maximum iterations={}',
            self.cuts_maxiter)
        generators_name = [
            g.name for g in self._cuts_generators_manager.generators
        ]
        logger.info(
            run_id,
            'Using cuts generators: {}',
            ', '.join(generators_name)
        )

        logger.debug(run_id, 'Variables bounds of problem')
        for v in problem.variables:
            vv = problem.variable_view(v)
            logger.debug(run_id, '\t{}\t({}, {})', v.name,
                         vv.lower_bound(), vv.upper_bound())

        # Check if problem is convex in current domain, in that case
        # use IPOPT to solve it (if all variables are reals)
        if self._convexity and _is_convex(problem, self._convexity):
            all_reals = all(
                problem.variable_view(v).domain.is_real()
                for v in problem.variables
            )
            if all_reals:
                return self._solve_convex_problem(problem)

        if not node.has_parent:
            # It's root node, try to find a feasible integer solution
            feasible_solution = \
                self._find_root_node_feasible_solution(run_id, problem)
            logger.info(
                run_id, 'Initial feasible solution: {}', feasible_solution
            )
        else:
            feasible_solution = None

        if logger.level <= DEBUG:
            logger.debug(run_id, 'Relaxed Problem')
            logger.debug(run_id, 'Variables:')
            relaxed = relaxed_problem

            for v in relaxed.variables:
                vv = relaxed.variable_view(v)
                logger.debug(
                    run_id, '\t{}: [{}, {}] c {}',
                    v.name, vv.lower_bound(), vv.upper_bound(), vv.domain
                )
            logger.debug(
                run_id, 'Objective: {}',
                expr_to_str(relaxed.objective.root_expr)
            )
            logger.debug(run_id, 'Constraints:')
            for constraint in relaxed.constraints:
                logger.debug(
                    run_id,
                    '{}: {} <= {} <= {}',
                    constraint.name,
                    constraint.lower_bound,
                    expr_to_str(constraint.root_expr),
                    constraint.upper_bound,
                )

        linear_problem = self._build_linear_relaxation(relaxed_problem)

        cuts_state = None
        lower_bound_search_start_time = current_time()
        if self._use_lp_cut_phase:
            originally_integer = []
            if not self._use_milp_cut_phase:
                for var in linear_problem.relaxed.variables:
                    vv = linear_problem.relaxed.variable_view(var)
                    if vv.domain.is_integer():
                        originally_integer.append(var)
                        linear_problem.relaxed.set_domain(var, core.Domain.REAL)

            feasible, cuts_state, mip_solution = self._perform_cut_loop(
                run_id, tree, node, problem, relaxed_problem, linear_problem,
            )

            for var in originally_integer:
                linear_problem.relaxed.set_domain(var, core.Domain.INTEGER)

            if not feasible:
                self._bac_telemetry.increment_lower_bound_time(
                    seconds_elapsed_since(lower_bound_search_start_time)
                )
                return NodeSolution(mip_solution, feasible_solution)

            # Solve MILP to obtain MILP solution
            mip_solution = self._mip_solver.solve(linear_problem.relaxed)

        if self._use_milp_cut_phase:
            feasible, cuts_state, mip_solution = self._perform_cut_loop(
                run_id, tree, node, problem, relaxed_problem, linear_problem,
            )

            if not feasible:
                self._bac_telemetry.increment_lower_bound_time(
                    seconds_elapsed_since(lower_bound_search_start_time)
                )
                return NodeSolution(mip_solution, feasible_solution)

        assert cuts_state is not None
        self._bac_telemetry.increment_lower_bound_time(
            seconds_elapsed_since(lower_bound_search_start_time)
        )

        if cuts_state.lower_bound >= tree.upper_bound and \
                not is_close(cuts_state.lower_bound, tree.upper_bound,
                             atol=mc.epsilon):
            # No improvement
            return NodeSolution(mip_solution, None)

        if self._timeout():
            # No time for finding primal solution
            return NodeSolution(mip_solution, None)

        upper_bound_search_start_time = current_time()

        starting_point = [v.value for v in mip_solution.variables]
        primal_solution = solve_primal_with_starting_point(
            run_id, problem, starting_point, self._nlp_solver, fix_all=True
        )
        new_primal_solution = solve_primal(
            run_id, problem, mip_solution, self._nlp_solver
        )
        if new_primal_solution is not None:
            primal_solution = new_primal_solution

        self._bac_telemetry.increment_upper_bound_time(
            seconds_elapsed_since(upper_bound_search_start_time)
        )

        if not primal_solution.status.is_success() and \
                feasible_solution is not None:
            # Could not get primal solution, but have a feasible solution
            return NodeSolution(mip_solution, feasible_solution)

        return NodeSolution(mip_solution, primal_solution)

    def _perform_cut_loop(self, run_id, tree, node, problem, relaxed_problem,
                          linear_problem):
        cuts_state = CutsState()
        mip_solution = None

        if node.parent:
            parent_cuts_count, mip_solution = self._add_cuts_from_parent(
                run_id, node, relaxed_problem, linear_problem
            )
            if self._bac_telemetry:
                self._bac_telemetry.increment_inherited_cuts(parent_cuts_count)

        while not self.cut_loop_should_terminate(cuts_state):
            feasible, new_cuts, mip_solution = self._perform_cut_round(
                run_id, problem, relaxed_problem,
                linear_problem.relaxed, cuts_state, tree, node
            )

            if not feasible:
                return False, cuts_state, mip_solution

            # Add cuts as constraints
            for cut in new_cuts:
                node.storage.cut_pool.add_cut(cut)
                node.storage.cut_node_storage.add_cut(cut)
                _add_cut_to_problem(linear_problem, cut)

            logger.debug(
                run_id, 'Updating CutState: State={}, Solution={}',
                cuts_state, mip_solution
            )

            cuts_state.update(
                mip_solution,
                paranoid=self.galini.paranoid_mode,
                atol=self.tolerance,
                rtol=self.relative_tolerance,
            )

            if not new_cuts:
                break

        return True, cuts_state, mip_solution

    def find_initial_solution(self, run_id, problem, tree, node):
        try:
            self._perform_fbbt(run_id, problem, tree, node, maxiter=1)
            relaxed_problem = self._build_convex_relaxation(problem)
            self._cuts_generators_manager.before_start_at_root(
                run_id, problem, relaxed_problem.relaxed
            )

            solution = self._solve_problem_at_node(
                run_id, problem, relaxed_problem.relaxed, tree, node
            )
            self._cuts_generators_manager.after_end_at_root(
                run_id, problem, relaxed_problem.relaxed, solution
            )
            return solution
        except Exception as ex:
            logger.info(run_id, 'Exception in find_initial_solution: {}', ex)
            return None

    def solve_problem_at_root(self, run_id, problem, tree, node):
        """Solve problem at root node."""
        self._before_root_node(problem)
        self._perform_fbbt(run_id, problem, tree, node)
        relaxed_problem = self._build_convex_relaxation(problem)
        self._cuts_generators_manager.before_start_at_root(
            run_id, problem, relaxed_problem.relaxed
        )

        solution = self._solve_problem_at_node(
            run_id, problem, relaxed_problem.relaxed, tree, node
        )
        self._cuts_generators_manager.after_end_at_root(
            run_id, problem, relaxed_problem.relaxed, solution
        )
        self._bounds = None
        self._convexity = None
        self._monotonicity = None
        return solution

    def solve_problem_at_node(self, run_id, problem, tree, node):
        """Solve problem at non root node."""
        self._perform_fbbt(run_id, problem, tree, node)
        relaxed_problem = self._build_convex_relaxation(problem)

        self._cuts_generators_manager.before_start_at_node(
            run_id, problem, relaxed_problem.relaxed
        )
        solution = self._solve_problem_at_node(
            run_id, problem, relaxed_problem.relaxed, tree, node
        )
        self._cuts_generators_manager.after_end_at_node(
            run_id, problem, relaxed_problem.relaxed, solution
        )
        self._bounds = None
        self._convexity = None
        self._monotonicity = None
        return solution

    def _build_convex_relaxation(self, problem):
        relaxation = ConvexRelaxation(
            problem,
            self._bounds,
            self._monotonicity,
            self._convexity,
        )
        return RelaxedProblem(relaxation, problem)

    def _build_linear_relaxation(self, problem):
        relaxation = LinearRelaxation(
            problem,
            self._bounds,
            self._monotonicity,
            self._convexity,
        )
        return RelaxedProblem(relaxation, problem)

    def _perform_cut_round(self, run_id, problem, relaxed_problem,
                           linear_problem, cuts_state, tree, node):
        logger.debug(run_id, 'Round {}. Solving linearized problem.', cuts_state.round)

        mip_solution = self._mip_solver.solve(linear_problem)

        logger.debug(
            run_id,
            'Round {}. Linearized problem solution is {}',
            cuts_state.round, mip_solution.status.description())
        logger.debug(run_id, 'Objective is {}'.format(mip_solution.objective))
        logger.debug(run_id, 'Variables are {}'.format(mip_solution.variables))
        if not mip_solution.status.is_success():
            return False, None, mip_solution
        # assert mip_solution.status.is_success()

        # Generate new cuts
        new_cuts = self._cuts_generators_manager.generate(
            run_id, problem, relaxed_problem, linear_problem, mip_solution,
            tree, node
        )
        logger.debug(
            run_id, 'Round {}. Adding {} cuts.',
            cuts_state.round, len(new_cuts)
        )
        return True, new_cuts, mip_solution

    def _find_root_node_feasible_solution(self, run_id, problem):
        logger.info(run_id, 'Finding feasible solution at root node')

        if self.root_node_feasible_solution_seed is not None:
            seed = self.root_node_feasible_solution_seed
            logger.info(run_id, 'Use numpy seed {}', seed)
            np.random.seed(seed)

        starting_point = [0.0] * problem.num_variables
        for i, v in enumerate(problem.variables):
            if problem.has_starting_point(v):
                starting_point[i] = problem.starting_point(v)
            elif problem.domain(v).is_integer():
                # Variable is integer and will be fixed, but we don't have a
                # starting point for it. Use lower or upper bound.
                has_lb = is_inf(problem.lower_bound(v))
                has_ub = is_inf(problem.upper_bound(v))
                if has_lb:
                    starting_point[i] = problem.lower_bound(v)
                elif has_ub:
                    starting_point[i] = problem.upper_bound(v)
                else:
                    starting_point[i] = 0
        return solve_primal_with_starting_point(
            run_id, problem, starting_point, self._nlp_solver
        )

    def _add_cuts_from_parent(self, run_id, node, relaxed_problem, linear_problem):
        logger.info(run_id, 'Adding inherit cuts.')
        first_loop = True
        num_violated_cuts = 0
        inherit_cuts_count = 0
        lp_solution = None
        while first_loop or num_violated_cuts > 0:
            first_loop = False
            num_violated_cuts = 0

            lp_solution = self._mip_solver.solve(linear_problem.relaxed)
            variables_x = [
                v.value
                for v in lp_solution.variables[:relaxed_problem.num_variables]
            ]

            # If the LP does not contain a variable, it's solution will be
            # None. Fix to (valid) numerical value so that we can evaluate
            # the expression.
            for i, var in enumerate(relaxed_problem.variables):
                if variables_x[i] is None:
                    lb = relaxed_problem.lower_bound(var)
                    if lb is not None:
                        variables_x[i] = lb
                        continue
                    ub = relaxed_problem.upper_bound(var)
                    if ub is not None:
                        variables_x[i] = ub
                        continue
                    variables_x[i] = 0.0

            if not lp_solution.status.is_success():
                break

            for cut in node.storage.cut_node_storage.cuts:
                is_duplicate = False
                for constraint in linear_problem.relaxed.constraints:
                    if constraint.name == cut.name:
                        is_duplicate = True
                if is_duplicate:
                    continue
                expr_tree_data = cut.expr.expression_tree_data(
                    relaxed_problem.num_variables
                )
                fg = expr_tree_data.eval(variables_x)
                fg_x = fg.forward(0, variables_x)[0]
                violated = False
                violation_lb = None
                violation_ub = None

                if cut.lower_bound is not None:
                    if not almost_ge(fg_x, cut.lower_bound, atol=mc.epsilon):
                        violation_lb = fg_x - cut.lower_bound
                        violated = True
                if cut.upper_bound is not None:
                    if not almost_le(fg_x, cut.upper_bound, atol=mc.epsilon):
                        violation_ub = fg_x - cut.upper_bound
                        violated = True

                if violated:
                    logger.debug(
                        run_id,
                        'Cut {} was violated. Adding back to problem. Violation: LB={}; UB={}',
                        cut.name,
                        violation_lb,
                        violation_ub,
                    )
                    num_violated_cuts += 1
                    inherit_cuts_count += 1
                    _add_cut_from_pool_to_problem(linear_problem, cut)

            logger.info(
                run_id,
                'Number of violated cuts at end of loop: {}',
                num_violated_cuts,
            )
        return inherit_cuts_count, lp_solution

    def _solve_convex_problem(self, problem):
        solution = self._nlp_solver.solve(problem)
        return NodeSolution(solution, solution)

    def _perform_fbbt(self, run_id, problem, tree, node, maxiter=None):
        fbbt_start_time = current_time()
        logger.debug(run_id, 'Performing FBBT')

        objective_upper_bound = None
        if tree.upper_bound is not None:
            objective_upper_bound = tree.upper_bound

        fbbt_maxiter = self.fbbt_maxiter
        if maxiter is not None:
            fbbt_maxiter = maxiter
        bounds = perform_fbbt(
            problem,
            maxiter=fbbt_maxiter,
            timelimit=self.fbbt_timelimit,
            objective_upper_bound=objective_upper_bound,
        )

        self._bounds, self._monotonicity, self._convexity = \
            propagate_special_structure(problem, bounds)

        logger.debug(run_id, 'Set FBBT Bounds')
        cause_infeasibility = None
        for v in problem.variables:
            vv = problem.variable_view(v)
            new_bound = bounds[v]
            if new_bound is None:
                new_bound = Interval(None, None)

            new_lb = _safe_lb(
                v.domain,
                new_bound.lower_bound,
                vv.lower_bound()
            )

            new_ub = _safe_ub(
                v.domain,
                new_bound.upper_bound,
                vv.upper_bound()
            )

            if new_lb > new_ub:
                cause_infeasibility = v

        if cause_infeasibility is not None:
            logger.info(
                run_id, 'Bounds on variable {} cause infeasibility',
                cause_infeasibility.name
            )
        else:
            for v in problem.variables:
                vv = problem.variable_view(v)
                new_bound = bounds[v]

                if new_bound is None:
                    new_bound = Interval(None, None)

                new_lb = _safe_lb(
                    v.domain,
                    new_bound.lower_bound,
                    vv.lower_bound()
                )

                new_ub = _safe_ub(
                    v.domain,
                    new_bound.upper_bound,
                    vv.upper_bound()
                )

                if np.isinf(new_lb):
                    new_lb = -np.inf

                if np.isinf(new_ub):
                    new_ub = np.inf

                if np.abs(new_ub - new_lb) < mc.epsilon:
                    new_lb = new_ub

                logger.debug(run_id, '  {}: [{}, {}]', v.name, new_lb, new_ub)
                vv.set_lower_bound(new_lb)
                vv.set_upper_bound(new_ub)

        group_name = '_'.join([str(c) for c in node.coordinate])
        logger.tensor(run_id, group_name, 'lb', problem.lower_bounds)
        logger.tensor(run_id, group_name, 'ub', problem.upper_bounds)
        self._bac_telemetry.increment_fbbt_time(
            seconds_elapsed_since(fbbt_start_time)
        )


def _convert_linear_expr(linear_problem, expr, objvar=None):
    stack = [expr]
    coefficients = {}
    const = 0.0
    while len(stack) > 0:
        expr = stack.pop()
        if expr.expression_type == ExpressionType.Sum:
            for ch in expr.children:
                stack.append(ch)
        elif expr.expression_type == ExpressionType.Linear:
            const += expr.constant_term
            for ch in expr.children:
                if ch.idx not in coefficients:
                    coefficients[ch.idx] = 0
                coefficients[ch.idx] += expr.coefficient(ch)
        else:
            raise ValueError(
                'Invalid ExpressionType {}'.format(expr.expression_type)
            )

    children = []
    coeffs = []
    for var, coef in coefficients.items():
        children.append(linear_problem.variable(var))
        coeffs.append(coef)

    if objvar is not None:
        children.append(objvar)
        coeffs.append(1.0)

    return core.LinearExpression(children, coeffs, const)





def _safe_lb(domain, a, b):
    if b is None:
        lb = a
    elif a is not None:
        lb = max(a, b)
    else:
        return None

    if domain.is_integer() and lb is not None:
        if is_close(np.floor(lb), lb, atol=mc.epsilon, rtol=0.0):
            return np.floor(lb)
        return np.ceil(lb)

    return lb


def _safe_ub(domain, a, b):
    if b is None:
        ub = a
    elif a is not None:
        ub = min(a, b)
    else:
        return None

    if domain.is_integer() and ub is not None:
        if is_close(np.ceil(ub), ub, atol=mc.epsilon, rtol=0.0):
            return np.ceil(ub)
        return np.floor(ub)

    return ub


def _is_convex(problem, cvx_map):
    obj = problem.objective
    is_objective_cvx = cvx_map[obj.root_expr].is_convex()

    if not is_objective_cvx:
        return False

    return all(
        _constraint_is_convex(cvx_map, cons)
        for cons in problem.constraints
    )


def _constraint_is_convex(cvx_map, cons):
    cvx = cvx_map[cons.root_expr]
    # g(x) <= UB
    if cons.lower_bound is None:
        return cvx.is_convex()

    # g(x) >= LB
    if cons.upper_bound is None:
        return cvx.is_concave()

    # LB <= g(x) <= UB
    return cvx.is_linear()


def _add_cut_from_pool_to_problem(problem, cut):
    # Cuts from pool are weird because they belong to other problems. So we
    # duplicate the (linear) cuts and insert that instead.
    def _duplicate_expr(expr):
        if expr.is_variable():
            return problem.relaxed.variable(expr.name)
        if isinstance(expr, core.LinearExpression):
            vars = [_duplicate_expr(v) for v in expr.children]
            return core.LinearExpression(vars, expr.linear_coefs, expr.constant_term)
        if isinstance(expr, core.SumExpression):
            children = [_duplicate_expr(ch) for ch in expr.children]
            return core.SumExpression(children)
        if isinstance(expr, core.NegationExpression):
            children = [_duplicate_expr(ch) for ch in expr.children]
            return core.NegationExpression(children)
        if isinstance(expr, core.QuadraticExpression):
            vars1 = [_duplicate_expr(t.var1) for t in expr.terms]
            vars2 = [_duplicate_expr(t.var2) for t in expr.terms]
            coefs = [t.coefficient for t in expr.terms]
            return core.QuadraticExpression(vars1, vars2, coefs)
        raise RuntimeError('Cut contains expr of type {}', type(expr))

    new_expr = _duplicate_expr(cut.expr)
    new_cut = Cut(
        cut.type_,
        cut.name,
        new_expr,
        cut.lower_bound,
        cut.upper_bound,
        cut.is_objective
    )
    return _add_cut_to_problem(problem, new_cut)


def _add_cut_to_problem(problem, cut):
    if not cut.is_objective:
        return problem.add_constraint(
            cut.name,
            cut.expr,
            cut.lower_bound,
            cut.upper_bound,
        )
    else:
        objvar = problem.relaxed.variable('_objvar')
        assert cut.lower_bound is None
        assert cut.upper_bound is None
        new_root_expr = core.SumExpression([
            cut.expr,
            core.LinearExpression([objvar], [-1.0], 0.0)
        ])
        return problem.add_constraint(
            cut.name,
            new_root_expr,
            None,
            0.0
        )
