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
"""Cuts generators manager."""
from galini.registry import Registry


class CutsGeneratorsRegistry(Registry):
    """Registry of CutsGenerators."""
    def group_name(self):
        return 'galini.cuts_generators'


class CutsGeneratorsManager(object):
    def __init__(self, registry, config):
        self._generators = self._initialize_generators(registry, config)

    def _initialize_generators(self, registry, config):
        generators = []
        for _, generator_cls in registry.items():
            generators.append(generator_cls(config))
        return generators

    @property
    def generators(self):
        return self._generators

    def before_start_at_root(self, problem):
        for gen in self._generators:
            gen.before_start_at_root(problem)

    def after_end_at_root(self, problem, solution):
        for gen in self._generators:
            gen.after_end_at_root(problem, solution)

    def before_start_at_node(self, problem):
        for gen in self._generators:
            gen.before_start_at_node(problem)

    def after_end_at_node(self, problem, solution):
        for gen in self._generators:
            gen.after_end_at_node(problem, solution)

    def generate(self, problem, mip_solution, tree, node):
        all_cuts = []
        for gen in self._generators:
            cuts = gen.generate(problem, mip_solution, tree, node)
            if cuts is None:
                cuts = []
            if not isinstance(cuts, list):
                raise ValueError('CutsGenerator.generate must return a list of cuts.')
            for cut in cuts:
                all_cuts.append(cut)
        return all_cuts