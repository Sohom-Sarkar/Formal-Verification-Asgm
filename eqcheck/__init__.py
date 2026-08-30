"""A combinational equivalence checker for Verilog, with SAT and BDD backends."""

from .equiv import (build_miter, check_sat, check_bdd, simulate, analyze_outputs,
                    failing_outputs, random_simulation, minimize_counterexample,
                    variable_order, best_static_order, sift_order,
                    STATIC_ORDERS, PortMismatch)
from .sweep import sat_sweep
from .simulate import ParallelSim
from .sim import Simulator
from .localize import localize, diagnose, verify_fix_set
from .algebraic import verify_multiplier, prove_equivalent_algebraic
from .elaborate import ElaborationError, CombinationalLoop
from .lexer import VerilogSyntaxError
from . import export

__all__ = [
    "build_miter", "check_sat", "check_bdd", "simulate", "analyze_outputs",
    "failing_outputs", "random_simulation", "minimize_counterexample",
    "variable_order", "best_static_order", "sift_order", "STATIC_ORDERS",
    "sat_sweep", "ParallelSim", "Simulator", "export",
    "localize", "diagnose", "verify_fix_set",
    "verify_multiplier", "prove_equivalent_algebraic",
    "PortMismatch", "ElaborationError", "CombinationalLoop",
    "VerilogSyntaxError",
]

__version__ = "2.0"
