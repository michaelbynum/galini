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
"""AlphaBB Solver."""
from galini.logging import Logger
from galini.special_structure import detect_special_structure
from galini.solvers import Solver, SolversRegistry
from galini.abb.algorithm import AlphaBBAlgorithm


class AlphaBBSolver(Solver):
    name = 'alpha_bb'

    description = 'AlphaBB for nonconvex MINLP.'

    def actual_solve(self, problem, **kwargs):
        logger = Logger.from_kwargs(kwargs)
        nlp_solver = self.instantiate_solver('ipopt')
        minlp_solver = self.instantiate_solver('oa')

        ctx = detect_special_structure(problem)
        for v in problem.variables:
            vv = problem.variable_view(v)
            new_bound = ctx.bounds[v]
            vv.set_lower_bound(new_bound.lower_bound)
            vv.set_upper_bound(new_bound.upper_bound)

        algo = AlphaBBAlgorithm(nlp_solver, minlp_solver, self.config.alpha_bb)
        return algo.solve(problem, logger=logger)
