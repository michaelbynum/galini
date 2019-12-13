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

"""Branch & Bound tree."""
from collections import namedtuple
import numpy as np
from galini.branch_and_bound.node import Node, NodeSolution
from galini.math import is_inf
from galini.solvers.solution import SolutionPool


TreeState = namedtuple('TreeState', ['lower_bound', 'upper_bound', 'nodes_visited'])


class BabTree:
    """Branch & Bound Tree.

    Parameters
    ----------
    storage : object
        node storage at root node
    branching_strategy : BranchingStrategy
        branching strategy
    selection_strategy : NodeSelectionStrategy
        node selection strategy
    """
    def __init__(self, storage, branching_strategy, selection_strategy):
        self.root = Node(storage, tree=self, coordinate=[0])
        self.branching_strategy = branching_strategy
        self.selection_strategy = selection_strategy
        self.state = TreeState(
            lower_bound=-np.inf,
            upper_bound=np.inf,
            nodes_visited=0,
        )
        self.open_nodes = {}
        self.fathomed_nodes = []
        self.solution_pool = SolutionPool(5)

        self._add_node(self.root)

    def branch_at_node(self, node):
        assert node.has_solution
        children, point = node.branch()
        if children is None:
            return None, None
        for child in children:
            self._add_node(child)
        return children, point

    def add_initial_solution(self, solution):
        assert is_inf(self.lower_bound)
        assert is_inf(self.upper_bound)
        self._update_node(
            self.root,
            NodeSolution(None, solution),
            is_root_node=True,
        )
        self.root.initial_feasible_solution = solution

    def update_root(self, solution):
        self._update_node(self.root, solution, True)

    def has_nodes(self):
        return self.selection_strategy.has_nodes()

    def next_node(self):
        return self.selection_strategy.next_node()

    def _add_node(self, node):
        assert not node.has_solution
        self.open_nodes[node.coordinate_hash] = node
        self.selection_strategy.insert_node(node)
        if node != self.root:
            new_lower_bound = self._open_nodes_lower_bound()
            new_state = TreeState(
                lower_bound=new_lower_bound,
                upper_bound=self.state.upper_bound,
                nodes_visited=self.state.nodes_visited,
            )
            self.state = new_state

    def update_node(self, node, solution):
        self._update_node(node, solution, False)

    def _update_node(self, node, solution, is_root_node):
        assert isinstance(solution, NodeSolution)
        node.update(solution, can_override=is_root_node)
        if node.coordinate_hash in self.open_nodes:
            del self.open_nodes[node.coordinate_hash]
        self._update_state(solution, is_root_node)

    @property
    def lower_bound(self):
        return self.state.lower_bound

    @property
    def upper_bound(self):
        return self.state.upper_bound

    @property
    def nodes_visited(self):
        return self.state.nodes_visited

    def fathom_node(self, node):
        self.fathomed_nodes.append(node)
        self._update_lower_bound()

    def node(self, coord):
        if not isinstance(coord, list):
            raise TypeError('BabTree coord must be a list')

        if coord[0] != 0:
            raise ValueError('First node must be root with index 0')

        if self.root is None:
            raise ValueError('Must add root node to tree.')

        coord = coord[1:]
        current = self.root
        for i, c in enumerate(coord):
            if current.children is None or c >= len(current.children):
                raise IndexError('Node index out of bounds at {}', coord[:i])
            current = current.children[c]
        return current

    def _update_state(self, solution, is_root_node):
        lower_bound_solution = solution.lower_bound_solution
        upper_bound_solution = solution.upper_bound_solution

        if (upper_bound_solution is not None and
                upper_bound_solution.status.is_success()):
            self.solution_pool.add(upper_bound_solution)
            upper_bound_solution = upper_bound_solution
            new_upper_bound = min(
                upper_bound_solution.objective_value(),
                self.state.upper_bound,
            )
        else:
            # Don't consider upper bound solution if it's none
            upper_bound_solution = None
            new_upper_bound = None

        if lower_bound_solution is None:
            return self._set_new_state(None, new_upper_bound)

        if not lower_bound_solution.status.is_success():
            return self._set_new_state(None, new_upper_bound)

        if is_root_node:
            new_lower_bound = lower_bound_solution.objective_value()

            if upper_bound_solution is None:
                return self._set_new_state(new_lower_bound, None)

            if not upper_bound_solution.status.is_success():
                return self._set_new_state(new_lower_bound, None)

            return self._set_new_state(
                new_lower_bound,
                upper_bound_solution.objective_value(),
            )

        # If there are open nodes, then the lower bound is the lowest
        # of their lower bounds.
        # If there are no open nodes, then the lower bound is the lowest
        # of the fathomed nodes lower bounds.
        if (upper_bound_solution is None or
                not upper_bound_solution.status.is_success()):
            new_upper_bound = None
        else:
            new_upper_bound = min(
                upper_bound_solution.objective_value(),
                self.state.upper_bound,
            )

        if self.open_nodes:
            new_lower_bound = self._open_nodes_lower_bound(new_upper_bound)
            return self._set_new_state(new_lower_bound, new_upper_bound)

        if self.fathomed_nodes:
            new_lower_bound = self._fathomed_nodes_lower_bound(new_upper_bound)
            return self._set_new_state(new_lower_bound, new_upper_bound)

        return self._set_new_state(None, new_upper_bound)

    def _update_lower_bound(self):
        if self.open_nodes:
            new_lower_bound = self._open_nodes_lower_bound()
            return self._set_new_state(new_lower_bound, None)

        if self.fathomed_nodes:
            new_lower_bound = self._fathomed_nodes_lower_bound()
            return self._set_new_state(new_lower_bound, None)

    def _set_new_state(self, new_lower_bound, new_upper_bound):
        if new_lower_bound is None:
            new_lower_bound = self.state.lower_bound

        if new_upper_bound is None:
            new_upper_bound = self.state.upper_bound

        new_nodes_visited = self.state.nodes_visited + 1
        self.state = \
            TreeState(new_lower_bound, new_upper_bound, new_nodes_visited)

    def _open_nodes_lower_bound(self, upper_bound=None):
        return self._nodes_minimum_lower_bound(
            self.open_nodes.values(),
            upper_bound,
        )

    def _fathomed_nodes_lower_bound(self, upper_bound=None):
        return self._nodes_minimum_lower_bound(
            self.fathomed_nodes,
            upper_bound,
        )

    def _nodes_minimum_lower_bound(self, nodes, upper_bound=None):
        if upper_bound is None:
            new_lower_bound = self.state.upper_bound
        else:
            new_lower_bound = upper_bound

        for node in nodes:
            if node.has_solution:
                lower_bound = node.lower_bound
            else:
                lower_bound = node.parent.lower_bound

            if lower_bound < new_lower_bound:
                new_lower_bound = node.parent.lower_bound
        return new_lower_bound
