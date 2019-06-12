# Copyright 2019 Francesco Ceccon
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

"""Contain relaxations used in the Branch & Cut algorithm."""
from galini.core import Domain, Objective, Constraint, Variable, LinearExpression, SumExpression
from galini.relaxations import Relaxation, RelaxationResult
from galini.underestimators import (
    McCormickUnderestimator,
    LinearUnderestimator,
    SumOfUnderestimators,
)

class ConvexRelaxationContext:
    metadata = {}


class _RelaxationBase(Relaxation):
    def __init__(self):
        super().__init__()
        self._ctx = ConvexRelaxationContext()
        self._underestimator = self._root_underestimator()

    def relaxed_problem_name(self, problem):
        return problem.name + '_convex'

    def before_relax(self, problem):
        self._ctx = ConvexRelaxationContext()
        self._before_relax(problem)

    def _before_relax(self, problem):
        pass

    def after_relax(self, problem, relaxed_problem):
        pass

    def relax_objective(self, problem, objective):
        result = self.relax_expression(problem, objective.root_expr)
        new_objective = Objective(objective.name, result.expression, objective.sense)
        return RelaxationResult(new_objective, result.constraints)

    def relax_constraint(self, problem, constraint):
        result = self.relax_expression(problem, constraint.root_expr)
        new_constraint = Constraint(
            constraint.name, result.expression, constraint.lower_bound, constraint.upper_bound
        )
        return RelaxationResult(new_constraint, result.constraints)

    def relax_expression(self, problem, expr):
        assert self._underestimator.can_underestimate(problem, expr, self._ctx)
        result = self._underestimator.underestimate(problem, expr, self._ctx)
        return result


class ConvexRelaxation(_RelaxationBase):
    def _root_underestimator(self):
        return SumOfUnderestimators([
            LinearUnderestimator(),
            McCormickUnderestimator(linear=False),
        ])


class LinearRelaxation(_RelaxationBase):
    def __init__(self):
        super().__init__()
        self._objective_count = 0

    def _before_relax(self, problem):
        self._objective_count = 0

    def _root_underestimator(self):
        return SumOfUnderestimators([
            LinearUnderestimator(),
            McCormickUnderestimator(linear=True),
        ])

    def relax_objective(self, problem, objective):
        self._objective_count += 1
        if self._objective_count > 1:
            raise ValueError('Apply LinearRelaxation to multi-objective problem')
        new_variable = Variable('_objvar', None, None, Domain.REAL)
        new_objective_expr = LinearExpression([new_variable], [1.0], 0.0)
        new_objective = Objective(objective.name, new_objective_expr, objective.sense)

        under_result = self.relax_expression(problem, objective.root_expr)

        new_cons_expr = SumExpression([
            under_result.expression,
            LinearExpression([new_variable], [-1.0], 0.0),
        ])

        new_cons = Constraint(
            '_obj_{}'.format(objective.name),
            new_cons_expr,
            None,
            0.0
        )

        under_result.constraints.append(new_cons)
        return RelaxationResult(new_objective, under_result.constraints)
