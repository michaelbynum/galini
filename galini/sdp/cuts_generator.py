# Copyright 2019 Radu Baltean-Lugojan
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

"""SDP Cuts generator."""

import platform
from ctypes import cdll, c_double
from itertools import combinations_with_replacement, chain
from operator import itemgetter
from os import path

import numpy as np
from networkx import enumerate_all_cliques, from_numpy_matrix
from suspect.expression import ExpressionType

from galini.abb.relaxation import AlphaBBRelaxation
from galini.config import CutsGeneratorOptions, NumericOption, EnumOption
from galini.core import Constraint
from galini.core import LinearExpression, SumExpression, QuadraticExpression
from galini.cuts import CutType, Cut, CutsGenerator
from galini.logging import get_logger
from galini.math import mc, is_close
from galini.quantities import relative_bound_improvement


logger = get_logger(__name__)


class SdpCutsGenerator(CutsGenerator):
    """
    Implements the low-dimensional semidefinite (sdp) cuts developed in
    'Selecting cutting planes for quadratic semidefinite outer-approximation via trained neural networks',
    Baltean-Lugojan, Radu and Bonami, Pierre and Misener, Ruth and Tramontani, Andrea, 2018,
    http://www.optimization-online.org/DB_HTML/2018/11/6943.html

    Scaling for the purpose of neural net evaluation is done at each B&B node according to Appendix A
    from 'Globally solving nonconvex quadratic programming problems with box
    constraints via integer programming methods',
    Bonami, Pierre and Gunluk, Oktay and Linderoth, Jeff,
    Mathematical Programming Computation, 1-50, 2018, Springer).
    """
    name = 'sdp'

    def __init__(self, galini, config):
        self._domain_eps = config['domain_eps']
        self._sel_size = config['selection_size']
        self._thres_sdp_viol = config['thres_sdp_viol']
        self._max_sdp_cuts = config['max_sdp_cuts_per_round']
        self._min_sdp_cuts = config['min_sdp_cuts_per_round']
        self._dim = config['dim']
        self._thres_min_opt_sel = config['thres_min_opt_sel']
        self._cut_sel = config['cut_sel_strategy']
        self._big_m = config['big_m']
        self._thres_min_coeff_comb = config['thres_min_coeff_comb']
        self._cuts_relative_tolerance = config['convergence_relative_tolerance']

        assert (0 <= self._sel_size), \
            "Selection size (how many cuts) needs to be a positive proportion/absolute number!"
        self._possible_dim = [2, 3, 4, 5]
        assert (self._dim in self._possible_dim), \
            "The dimensionality of SDP cuts evaluated by neural networks is between 2 and 5!"

        # *** Problem info related to SDP cuts associated with the entire B&B tree
        self._nb_vars = 0  # number of variables in problem
        # Flag for neural nets/ optimality cut selection being used
        self._nn_used = self._cut_sel not in ["FEAS", "RANDOM"]
        # Hold trained neural networks functions
        self._nns = None
        # Global variables to speed up eigen-decomposition by preforming matrix to be decomposed
        self._Mat = None
        self._inds = None
        # Data structure holding SDP decomposition (involving objectives and constraints)
        self._agg_list = None

        # *** Problem info related to SDP cuts associated with every node of B&B
        self._agg_list_rescaled = None
        self._cut_round = 0
        self._lbs = None    # lower bounds
        self._ubs = None    # upper bounds
        self._dbs = None    # difference in bounds

    @staticmethod
    def cuts_generator_options():
        return CutsGeneratorOptions(SdpCutsGenerator.name, [
            NumericOption('domain_eps',
                          default=1e-3,
                          description="Minimum domain length for each variable to consider cut on"),
            NumericOption('selection_size',
                          default=0.1,
                          description="Cut selection size as a % of all cuts or as absolute number of cuts"),
            NumericOption('thres_sdp_viol',
                          default=-1e-15,
                          description="Violation (negative eigenvalue) threshold for separation of SDP cuts"),
            NumericOption('min_sdp_cuts_per_round',
                          default=1e1,
                          description="Min number of SDP cuts to be added to relaxation at each cut round"),
            NumericOption('max_sdp_cuts_per_round',
                          default=5e3,
                          description="Max number of SDP cuts to be added to relaxation at each cut round"),
            NumericOption('dim',
                          default=3,
                          description="Dimension of SDP decomposition/cuts - min=2, max=5"),
            EnumOption('cut_sel_strategy',
                       default="COMB_ONE_CON",
                       values=["OPT", "FEAS", "RANDOM", "COMB_ONE_CON", "COMB_ALL_CON"]),
            NumericOption('big_m',
                          default=10e3,
                          description="Big M constant value for combined optimality/feasibility cut selection strategy"),
            NumericOption('thres_min_opt_sel',
                          default=0,
                          description="Threshold of minimum optimality measure to select cut in "
                                      "combined optimality/feasibility cut selection strategy"),
            NumericOption('thres_min_coeff_comb',
                          default=1e-5,
                          description="Threshold for the min absolute value of a valid coefficient when combining "
                                      "constraints based on Lagrange multipliers for optimality cut selection"),
            NumericOption('convergence_relative_tolerance',
                          default=1e-3,
                          description='Termination criteria on lower bound improvement between '
                                      'two consecutive cut rounds <= relative_tolerance % of '
                                      'lower bound improvement from cut round'),
        ])

    def before_start_at_root(self, run_id, problem, relaxed_problem):
        self._nb_vars = problem.num_variables
        # self._add_missing_squares(problem)
        if self._nn_used:
            self._nns = self._load_neural_nets()
        if self._agg_list is None:
            self._agg_list = self._get_sdp_decomposition(problem)

        self._Mat = [np.zeros((i+1, i+1)) for i in self._possible_dim]
        for i in range(len(self._possible_dim)):
            self._Mat[i][0, 0] = 1
        self._inds = [(np.array([1 + x for x in np.triu_indices(dim, 0, dim)[0]]),
                       np.array([1 + x for x in np.triu_indices(dim, 0, dim)[1]]))
                      for dim in self._possible_dim]

        self.before_start_at_node(run_id, problem, relaxed_problem)

    def after_end_at_root(self, run_id, problem, relaxed_problem, solution):
        self.after_end_at_node(run_id, problem, relaxed_problem, solution)

    def before_start_at_node(self, run_id, problem, relaxed_problem):
        self._lbs = problem.lower_bounds
        self._ubs = problem.upper_bounds
        self._dbs = [u - l for l, u in zip(self._lbs, self._ubs)]
        self._agg_list_rescaled = self._rescale_coeffs_for_cut_selection() if self._nn_used else self._agg_list
        self._cut_round = 0

    def after_end_at_node(self, run_id, problem, relaxed_problem, solution):
        self._lbs = None
        self._ubs = None
        self._dbs = None
        self._agg_list_rescaled = None

    def has_converged(self, state):
        """Termination criteria for cut generation loop."""
        if (state.first_solution is None or
            state.previous_solution is None or
            state.latest_solution is None):
            return False

        return relative_bound_improvement(
            state.first_solution,
            state.previous_solution,
            state.latest_solution
        ) <= self._cuts_relative_tolerance

    def generate(self, run_id, problem, relaxed_problem, linear_problem, solution, tree, node):
        if not np.all(np.isfinite(self._dbs)):
            return
        cuts = list(self._generate(run_id, problem, relaxed_problem, linear_problem, solution, tree, node))
        self._cut_round += 1
        return cuts

    def _generate(self, run_id, problem, _relaxed_problem, linear_problem, solution, tree, node):
        rank_list = self._get_sdp_selection(run_id, linear_problem, solution)
        agg_list = self._agg_list_rescaled
        nb_sdp_cuts = 0

        # Interpret selection size as % or absolute number and threshold the maximum number of SDP cuts per round
        nb_cuts = int(np.floor(self._sel_size * len(rank_list))) \
            if self._sel_size <= 1 else int(np.floor(self._sel_size))
        max_sdp_cuts = int(min(
            max(self._min_sdp_cuts, nb_cuts),
            min(self._max_sdp_cuts, len(rank_list))))

        # Generate and add selected cuts up to (sel_size) in number
        for ix in range(0, max_sdp_cuts):
            (idx, obj_improve, x_vals, X_slice, dim_act) = rank_list[ix]
            dim_act = len(x_vals)
            eigvals, evecs = self._get_eigendecomp(dim_act, x_vals, X_slice, True)
            if eigvals[0] < self._thres_sdp_viol:
                evect = evecs.T[0]
                evect = np.where(abs(evect) <= -self._thres_sdp_viol, 0, evect)
                evect_arr = [evect[idx1] * evect[idx2] * 2 if idx1 != idx2 else evect[idx1] * evect[idx2]
                             for idx1 in range(dim_act + 1) for idx2 in range(max(idx1, 1), dim_act + 1)]
                x_vars = [problem.variables[i] for i in agg_list[idx][0]]
                # Construct SDP cut involving only auxiliary variables in the upper triangular matrix of a slice
                sum_expr = SumExpression([
                    QuadraticExpression(
                        list(chain.from_iterable([[x_var for _ in range(dim_act - x_idx)]
                                                  for x_idx, x_var in enumerate(x_vars)])),
                        list(chain.from_iterable([x_vars[i:] for i in range(dim_act)])),
                        evect_arr[dim_act:]),
                    LinearExpression(x_vars, evect_arr[0:dim_act], evect[0] * evect[0])
                ])
                nb_sdp_cuts += 1
                cut_name = 'sdp_cut_{}_{}'.format(self._cut_round, nb_sdp_cuts)
                yield Cut(CutType.LOCAL, cut_name, sum_expr, 0, None)

    def _add_missing_squares(self, problem):
        # Add Xii>=0 constraints to introduce auxiliary variables Xii where their coefficient is 0 in the problem
        # since such aux vars can be part of an SDP cut and need to be defined
        relaxation = AlphaBBRelaxation()
        relaxed_problem = relaxation.relax(problem)
        for var_nb in range(self._nb_vars):
            xi = problem.variables[var_nb]
            sq_cut = Constraint('sq_' + str(var_nb), QuadraticExpression([xi], [xi], [1.0]), 0, None)
            relaxation._relax_constraint(problem, relaxed_problem, sq_cut)

    def _get_lifted_mat_values(self, problem, solution):
        # Build matrix of lifted X values
        nb_vars = self._nb_vars
        lifted_mat = np.zeros((nb_vars, nb_vars))
        for var_sol in solution.variables[nb_vars:]:
            var = problem.variable(var_sol.name)
            if not var.is_auxiliary:
                continue
            var1 = var.reference.var1
            var2 = var.reference.var2
            lifted_mat[var1.idx, var2.idx] = var_sol.value
            lifted_mat[var2.idx, var1.idx] = var_sol.value
        return lifted_mat

    def _get_sdp_selection(self, run_id, problem, solution):
        lifted_mat = self._get_lifted_mat_values(problem, solution)
        agg_list = self._agg_list_rescaled
        nns = self._nns
        l = self._lbs
        d = self._dbs
        rank_list = []

        if solution.dual_values is None:
            # pylint: disable=line-too-long
            logger.warning(run_id, 'SDP Cuts Generator requires solution dual values but solver did not return them.')
            return rank_list

        # For each sub-problem rho
        for idx, (clique, inputNNs) in enumerate(agg_list):
            obj_improve = 0
            dim_act = len(clique)
            x_vals = [solution.variables[var_idx].value for var_idx in clique]
            if any(v is None for v in x_vals):
                continue
            cl_idxs = list(combinations_with_replacement(clique, 2))
            X_slice = np.asarray(itemgetter(*cl_idxs)(lifted_mat))

            # Combined selections with optimality measure (=neural net estimation of objective improvement)
            # computed taking into account quadratic objective & constraints.
            # Can be implemented considering 1, 2, 3, ..., all subsets of quad objective & constraints at a time.
            if self._nn_used:
                # If the domain of any variable involved in the cut is very small, don't consider cut
                if any(d[i] <= self._domain_eps for i in clique):
                    continue
                # If neural net evaluations are used, rescale solution to [0, 1] using bounds
                X_slice_rs = X_slice + np.asarray([
                    (l[i] * solution.variables[j].value + l[j] * solution.variables[i].value - l[i] * l[j])/d[i]/d[j]
                    for (i, j) in cl_idxs])
                x_vals_rs = [(x_vals[x_idx] - l[i]) / d[i] for x_idx, i in enumerate(clique)]
                # Get eigenvalues if not optimality-only cut selection
                eigval = self._get_eigendecomp(dim_act, x_vals, X_slice, False)[0] if self._cut_sel != "OPT" \
                    else self._thres_sdp_viol
                if eigval <= self._thres_sdp_viol:
                    # Flag indicating whether valid cut can be selected by the optimality measure
                    sel_by_opt = False
                    # One constraint at a time (includes optimality-only cut selection which is guaranteed to converge
                    # only for instances with no quadratic constraints) to get coefficients/input for neural nets
                    if self._cut_sel in ["OPT", "COMB_ONE_CON"]:
                        for idx2, (input_nn, max_elem, con_idx) in enumerate(inputNNs):
                            # Lagrange multiplier (1 if objective)
                            mu = solution.dual_values[con_idx] if con_idx >= 0 else 1
                            # If mu negative reverse sense of constraint coefficients inputted in a neural net
                            # since an improvement in the objective is linked to positive mu
                            if mu > 0:
                                input_nn = np.sign(mu) * input_nn
                                estim = nns[dim_act - 2][0](nns[dim_act - 2][1](*x_vals_rs, *input_nn.tolist())) - \
                                        np.matmul(input_nn, X_slice_rs)
                                if estim > self._thres_min_opt_sel or self._cut_sel == "OPT":
                                    obj_improve += estim * max_elem * abs(mu)
                                    sel_by_opt = True
                    # Combine all constraints to get coefficients/input for neural nets
                    elif self._cut_sel == "COMB_ALL_CON":
                        input_nn_sum = np.zeros(len(cl_idxs))
                        # Sum up coefficient of X_rho variables from all relevant constraints
                        # (accounting for mu and rescaling)
                        for idx2, (input_nn, max_elem, con_idx) in enumerate(inputNNs):
                            # Lagrange multiplier (1 if objective)
                            mu = solution.dual_values[con_idx] if con_idx >= 0 else 1
                            input_nn_sum += input_nn * max_elem * mu
                        # Bound domains of eigenvalues/coefficients to [-1,1] via Lemma 4.1.2
                        max_elem = len(clique) * abs(max(input_nn_sum, key=abs))
                        if max_elem > self._thres_min_coeff_comb:
                            input_nn_sum = input_nn_sum / max_elem
                            estim = nns[dim_act - 2][0](nns[dim_act - 2][1](*x_vals_rs, *input_nn_sum.tolist())) - \
                                    np.matmul(input_nn, X_slice_rs)
                            if estim > self._thres_min_opt_sel:
                                obj_improve += estim * max_elem
                                sel_by_opt = True
                    # In combined optimality+feasibility selection,
                    # prioritize cuts selected by optimality then those selected by feasibility
                    if self._cut_sel != "OPT":
                        if sel_by_opt:  # If strong cut is selected by an optimality measure
                            obj_improve += self._big_m
                        else:  # If cut not strong but valid
                            obj_improve = -eigval

            # Feasibility selection by absolute value of largest negative eigenvalue
            elif self._cut_sel == "FEAS":
                obj_improve = - self._get_eigendecomp(dim_act, x_vals, X_slice, False)[0]

            # Random selection
            elif self._cut_sel == "RANDOM":
                obj_improve = np.random.random_sample()

            rank_list.append((idx, obj_improve, x_vals, X_slice, dim_act))
        # Sort sub-problems by measure for selection
        rank_list.sort(key=lambda tup: tup[1], reverse=True)
        return rank_list

    def _rescale_coeffs_for_cut_selection(self):
        agg_list = self._agg_list
        agg_list_rescaled = [0] * len(agg_list)
        d = self._dbs
        for idx, (clique, inputNNs) in enumerate(agg_list):
            clique_size = len(clique)
            rescale_vec = np.asarray([d[el1] * d[el2] for (el1, el2) in combinations_with_replacement(clique, 2)])
            inputNNs_rescaled = [0] * len(inputNNs)
            for idx2, (input_nn, max_elem, con_idx) in enumerate(inputNNs):
                # Rescale input coefficients according to bounds
                input_nn = input_nn / rescale_vec
                # Rescale coefficients to be in [0,1]
                max_elem = clique_size * abs(max(input_nn, key=abs))
                inputNNs_rescaled[idx2] = (input_nn / max_elem, max_elem, con_idx)
            agg_list_rescaled[idx] = (clique, inputNNs_rescaled)
        return agg_list_rescaled

    def _load_neural_nets(self):
        """Load trained neural networks (from /neural_nets/NNs.dll) up to the sub-problem dimension needed for an SDP
        decomposition. These neural networks estimate the expected objective improvement for a
        particular sub-problem at the current solution point.
        """
        self._nns = []
        dirname = path.dirname(__file__)
        if platform.uname()[0] == "Windows":
            nn_library = path.join(dirname, 'NNs.dll')
        elif platform.uname()[0] == "Linux":
            nn_library = path.join(dirname, 'NNs.so')
        else:  # Mac OSX
            raise ValueError('The neural net library for SDP cuts is compiled only for '
                             'Linux/Win! (OSX needs compiling)')
            #   nn_library = 'neural_nets/NNs.dylib' - Not compiled for OSX, will throw error
        nn_library = cdll.LoadLibrary(nn_library)
        for d in range(2, self._dim + 1):  # (d=|rho|) - each subproblem rho has a neural net depending on its size
            func_dim = getattr(nn_library, "neural_net_%dD" % d)  # load each neural net
            func_dim.restype = c_double  # return type from each neural net is a c_double
            # c_double array input: x_rho (the current point) and Q_rho (upper triangular part since symmetric)
            type_dim = (c_double * (d * (d + 3) // 2))
            self._nns.append((func_dim, type_dim))
        return self._nns

    def _get_sdp_decomposition(self, problem):
        nb_vars = problem.num_variables
        dim = self._dim
        agg_list = []
        quad_terms_per_con = [
            []
            for _ in range(1 + len(problem.constraints))
        ]

        # Find all quadratic terms (across all objectives + constraints) and form an adjacency matrix for their indices
        adj_mat = np.zeros((nb_vars, nb_vars))
        vars_dict = dict([(v.name, v_idx) for v_idx, v in enumerate(problem.variables)])
        for con_idx, constraint in enumerate([problem.objective, *problem.constraints]):
            root_expr = constraint.root_expr
            quadratic_expr = None
            if root_expr.expression_type == ExpressionType.Quadratic:
                quadratic_expr = root_expr
            elif root_expr.expression_type == ExpressionType.Sum:
                assert len(root_expr.children) == 2
                a, b = root_expr.children
                if a.expression_type == ExpressionType.Quadratic:
                    assert b.expression_type == ExpressionType.Linear
                    quadratic_expr = a
                if b.expression_type == ExpressionType.Quadratic:
                    assert a.expression_type == ExpressionType.Linear
                    quadratic_expr = b
            if quadratic_expr is not None:
                for term in quadratic_expr.terms:
                    if term.coefficient != 0:
                        quad_idxs = (vars_dict[term.var1.name], vars_dict[term.var2.name])
                        adj_mat[quad_idxs[0], quad_idxs[1]] = 1
                        adj_mat[quad_idxs[1], quad_idxs[0]] = 1
                        quad_terms_per_con[con_idx].append((quad_idxs, term.coefficient))

        # Get only cliques up the the dimension of the SDP decomposition
        all_cliques_iterator = enumerate_all_cliques(from_numpy_matrix(adj_mat))
        for clique in all_cliques_iterator:
            if len(clique) < 2:
                continue
            elif len(clique) <= dim:
                agg_list.append(set(clique))
            else:
                break
        # Eliminate cliques that are subsets of other cliques
        agg_list = [(x, []) for x in agg_list if not any(x <= y for y in agg_list if x is not y)]

        # Look in each constraint at a time for cliques up to dim in size
        nb_objs = 1
        for con_idx, constraint in enumerate([problem.objective, *problem.constraints]):
            adj_mat_con = np.zeros((nb_vars, nb_vars))
            coeff_mat_con = np.zeros((nb_vars, nb_vars))
            for (quad_idxs, term_coeff) in quad_terms_per_con[con_idx]:
                adj_mat_con[quad_idxs[0], quad_idxs[1]] = 1
                adj_mat_con[quad_idxs[1], quad_idxs[0]] = 1
                coeff_mat_con[quad_idxs[0], quad_idxs[1]] = term_coeff
                coeff_mat_con[quad_idxs[1], quad_idxs[0]] = term_coeff
            # Get only cliques up the the dimension of the SDP decomposition
            agg_list_con = []
            for clique in enumerate_all_cliques(from_numpy_matrix(adj_mat_con)):
                if len(clique) < 2:
                    continue
                elif len(clique) <= dim:
                    agg_list_con.append(set(clique))
                else:
                    break
            # Eliminate cliques that are subsets of other cliques
            agg_list_con = [x for x in agg_list_con if not any(x <= y for y in agg_list_con if x is not y)]
            # Aggregate coefficient info (input_nn) used as input for neural networks for each constraint
            for agg_idx, (clique, _) in enumerate(agg_list):
                for clique_con in agg_list_con:
                    if clique_con <= clique and len(clique_con.intersection(clique)) > 1:
                        mat_idxs = list(combinations_with_replacement(sorted(clique), 2))
                        input_nn = itemgetter(*mat_idxs)(coeff_mat_con)
                        agg_list[agg_idx][1].append((np.asarray(input_nn), 1, con_idx-nb_objs))
        # Sort clique elements after done with them as sets (since neural networks are not invariant on order)
        agg_list = [(sorted(clique), _) for (clique, _) in agg_list]
        return agg_list

    def _get_eigendecomp(self, dim_subpr, x_vals, X_slice, ev_yes):
        """Get eigen-decomposition of a matrix of type [1, x^T; x, X] where x=(x_vals), X=(X_slice),
        with/(out) eigenvectors (ev_yes)
        """
        mat = self._Mat
        mat[dim_subpr - 2][0, 1:] = x_vals
        mat[dim_subpr - 2][self._inds[dim_subpr - 2]] = X_slice
        # Eigenvalues are returned in ascending order
        return np.linalg.eigh(mat[dim_subpr - 2], "U") if ev_yes \
            else np.linalg.eigvalsh(mat[dim_subpr - 2], "U")
