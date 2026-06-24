from __future__ import annotations

from dataclasses import dataclass

from .config import SafetyConfig


RISK_LEVELS = {"safe": 0, "read": 1, "power": 2, "dangerous": 3}


@dataclass
class SafetyDecision:
    allowed: bool
    reason: str
    requires_confirmation: bool = False


class DecisionGate:
    def __init__(self, config: SafetyConfig):
        self.config = config

    def evaluate(self, tool_name: str, risk: str) -> SafetyDecision:
        if tool_name in self.config.blocked_tools:
            return SafetyDecision(False, f"{tool_name} is blocked by safety settings.")
        if risk == "dangerous":
            return SafetyDecision(False, "Dangerous tools are hard-blocked in v1.")
        max_risk = RISK_LEVELS.get(self.config.max_tool_risk, 2)
        actual = RISK_LEVELS.get(risk, 3)
        if actual > max_risk:
            return SafetyDecision(False, f"{tool_name} risk level {risk} exceeds configured maximum.")
        if risk == "power" and self.config.require_confirmation_for_power:
            return SafetyDecision(True, "Power tool requires confirmation.", True)
        return SafetyDecision(True, "Allowed.")
