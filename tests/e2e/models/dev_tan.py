# pylint: skip-file
import pyomo.environ as aml
import math


def get_pyomo_model():
    m = aml.ConcreteModel()

    m.x = aml.Var(initialize=10.0)
    m.y = aml.Var(initialize=-10.0)

    m.c0 = aml.Constraint(expr=aml.tan(m.x) >= 0)
    m.c1= aml.Constraint(expr=aml.tan(m.y) >= 0)
    m.o = aml.Objective(expr=aml.tan(m.x))
    return m
