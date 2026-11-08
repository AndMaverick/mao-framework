"""MAO: Model, Action, Orchestration. A small, dependency-free framework for governed AI systems."""

from .action import Action, Decision, Policy
from .audit import AuditLog
from .diagnostic import QUESTIONS, report, score
from .model import ModelRegistry, ModelSpec, NoModelError
from .orchestrator import Orchestrator, RunResult, Step

__version__ = "0.1.0"

__all__ = [
    "Action", "AuditLog", "Decision", "ModelRegistry", "ModelSpec", "NoModelError",
    "Orchestrator", "Policy", "QUESTIONS", "RunResult", "Step", "report", "score",
]
