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
"""Configuration Manager."""
import toml
from galini.config.configuration import GaliniConfig
from galini.config.options import (
    OptionsGroup,
    ExternalSolverOptions,
    EnumOption,
    NumericOption,
    StringOption,
    BoolOption,
)


class ConfigurationManager(object):
    def __init__(self):
        self._initialized = False
        self._configuration = None

    def initialize(self, solvers_reg, user_config_path=None):
        config = GaliniConfig()

        # add default sections
        logging_group = config.add_group('logging')
        _assign_options_to_group(_logging_group(), logging_group)

        galini_group = config.add_group('galini')
        _assign_options_to_group(_galini_group(), galini_group)

        # initialize configuration from solvers
        for _, solver in solvers_reg.items():
            solver_options = solver.solver_options()
            group = config.add_group(solver_options.name)
            _assign_options_to_group(solver_options, group)

        if user_config_path:
            # overwrite config from user config
            user_config = toml.load(user_config_path)
            config.update(user_config)
        self._configuration = config
        self._initialized = True

    @property
    def configuration(self):
        if not self._initialized:
            raise RuntimeError('ConfigurationManager was not initialized.')
        return self._configuration


def _logging_group():
    return OptionsGroup('logging', [
        EnumOption('level', ['NOTSET', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], 'INFO'),
        BoolOption('stdout', default=True),
        StringOption('directory', default=None),
    ])


def _galini_group():
    return OptionsGroup('galini', [
        NumericOption('timelimit', min_value=0, default=86400),
    ])


def _assign_options_to_group(options, group):
    for option in options.iter():
        if isinstance(option, ExternalSolverOptions):
            group.add_group(option.name, strict=False)
        elif isinstance(option, OptionsGroup):
            sub_group = group.add_group(option.name)
            _assign_options_to_group(option, sub_group)
        else:
            group.set(option.name, option.default)