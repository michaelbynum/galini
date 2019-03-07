# Copyright 2018 Francesco Ceccon
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Base class for B&B algorithms."""
import heapq
import abc
import numpy as np
from suspect.interval import Interval
from galini.logging import get_logger
from galini.quantities import relative_gap, absolute_gap
from galini.bab.strategy import KSectionBranchingStrategy
from galini.bab.tree import BabTree
from galini.special_structure import detect_special_structure


logger = get_logger(__name__)

class NodeSelectionStrategy(object):
    class _Node(object):
        def __init__(self, node):
            self.inner = node

        def __lt__(self, other):
            if not self.inner.solution.status.is_success():
                return False
            if not other.inner.solution.status.is_success():
                return True
            return self.inner.state.lower_bound < other.inner.state.lower_bound

    def __init__(self):
        self.nodes = []

    def insert_node(self, node):
        heapq.heappush(self.nodes, self._Node(node))

    def has_nodes(self):
        return len(self.nodes) > 0

    def next_node(self):
        if len(self.nodes) == 0:
            return None
        node = heapq.heappop(self.nodes)
        return node.inner


class BabAlgorithm(metaclass=abc.ABCMeta):
    name = 'bab'

    def initialize(self, config):
        self.tolerance = config['tolerance']
        self.relative_tolerance = config['relative_tolerance']
        self.node_limit = config['node_limit']
        self.fbbt_maxiter = config['fbbt_maxiter']

    def solve_problem_at_root(self, run_id, problem, relaxed_problem, tree, node, relaxation):
        return self.solve_problem_at_node(run_id, problem, relaxed_problem, tree, node, relaxation)

    @abc.abstractmethod
    def solve_problem_at_node(self, run_id, problem, relaxed_problem, tree, node, relaxation):
        raise NotImplementedError()

    def has_converged(self, state):
        rel_gap = relative_gap(state.lower_bound, state.upper_bound)
        abs_gap = absolute_gap(state.lower_bound, state.upper_bound)
        assert (state.lower_bound <= state.upper_bound or
                np.isclose(state.lower_bound, state.upper_bound))
        return (
            rel_gap <= self.relative_tolerance or
            abs_gap <= self.tolerance
        )

    def _node_limit_exceeded(self, state):
        return state.nodes_visited > self.node_limit

    def solve(self, problem, run_id, **kwargs):
        branching_strategy = KSectionBranchingStrategy(2)
        node_selection_strategy = NodeSelectionStrategy()
        tree = BabTree(problem, branching_strategy, node_selection_strategy)

        logger.info(run_id, 'Solving root problem')
        root_solution = self._solve_problem_at_root(run_id, problem, tree, tree.root)
        tree.update_root(root_solution)

        logger.info(run_id, 'Root problem solved, tree state {}', tree.state)
        logger.log_add_bab_node(
            run_id,
            coordinate=[0],
            lower_bound=root_solution.lower_bound,
            upper_bound=root_solution.upper_bound,
        )

        if self.has_converged(tree.state):
            # problem is convex so it has converged already
            return root_solution.solution

        while not self.has_converged(tree.state) and not self._node_limit_exceeded(tree.state):
            logger.info(run_id, 'Tree state at beginning of iteration: {}', tree.state)
            if not tree.has_nodes():
                return tree.best_solution.solution
            current_node = tree.next_node()

            logger.info(
                run_id,
                'Visiting node {}: state={}, solution={}',
                current_node.coordinate,
                current_node.state,
                current_node.solution,
            )

            if current_node.state.lower_bound >= tree.state.upper_bound:
                logger.info(
                    run_id,
                    "Skip node because it won't improve bound: node.lower_bound={}, tree.upper_bound={}",
                    current_node.state.lower_bound,
                    tree.state.upper_bound,
                )
                logger.log_prune_bab_node(run_id, current_node.coordinate)
                continue

            if not current_node.solution.status.is_success():
                logger.info(run_id, "Skip node because it was not feasible")
                logger.log_prune_bab_node(run_id, current_node.coordinate)
                continue

            node_children, branching_point = current_node.branch()
            logger.info(run_id, 'Branched at point {}', branching_point)
            for child in node_children:
                solution = self._solve_problem_at_node(run_id, child.problem, tree, child)
                tree.update_node(child, solution)

                self._log_problem_information_at_node(run_id, child.problem, solution, child)
                logger.info(run_id, 'Child {} has solution {}', child.coordinate, solution)
                logger.info(run_id, 'New tree state {}', tree.state)
                var_view = child.problem.variable_view(child.variable)
                logger.log_add_bab_node(
                    run_id,
                    coordinate=child.coordinate,
                    lower_bound=solution.lower_bound,
                    upper_bound=solution.upper_bound,
                    branching_variables=[(child.variable.name, var_view.lower_bound(), var_view.upper_bound())],
                )

        if tree.best_solution is not None:
            return tree.best_solution.solution
        else:
            return None

    def _solve_problem_at_root(self, run_id, problem, tree, node):
        self._perform_fbbt(problem)
        return self.solve_problem_at_root(run_id, problem, tree, node)

    def _solve_problem_at_node(self, run_id, problem, tree, node):
        self._perform_fbbt(problem)
        return self.solve_problem_at_node(run_id, problem, tree, node)

    def _perform_fbbt(self, problem):
        ctx = detect_special_structure(problem, max_iter=self.fbbt_maxiter)
        for v in problem.variables:
            vv = problem.variable_view(v)
            new_bound = ctx.bounds[v]
            if new_bound is None:
                new_bound = Interval(None, None)
            vv.set_lower_bound(_safe_lb(new_bound.lower_bound, vv.lower_bound()))
            vv.set_upper_bound(_safe_ub(new_bound.upper_bound, vv.upper_bound()))

    def _log_problem_information_at_node(self, run_id, problem, solution, node):
        group_name = '_'.join([str(c) for c in node.coordinate])
        logger.tensor(
            run_id,
            group=group_name,
            dataset='lower_bounds',
            data=np.array(problem.lower_bounds)
        )
        logger.tensor(
            run_id,
            group=group_name,
            dataset='upper_bounds',
            data=np.array(problem.upper_bounds)
        )
        if solution.solution.status.is_success():
            logger.tensor(
                run_id,
                group=group_name,
                dataset='solution',
                data=np.array([v.value for v in solution.solution.variables]),
            )


def _safe_lb(a, b):
    if b is None:
        return a
    return max(a, b)

def _safe_ub(a, b):
    if b is None:
        return a
    return min(a, b)
