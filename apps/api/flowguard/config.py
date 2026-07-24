"""Versioned deterministic defaults for the FlowGuard financial core."""

from __future__ import annotations

FINANCIAL_CORE_VERSION = "financial-core-1"
CASHFLOW_TOOL_VERSION = "cashflow-1"
SAFE_TO_SPEND_TOOL_VERSION = "safe-to-spend-1"
ACTION_EVALUATOR_TOOL_VERSION = "action-evaluator-1"
POLICY_VERSION = "policy-1"
DELAY_MODEL_VERSION = "receivable-delay-fixed-v1"
RISK_RULE_VERSION = "risk-presentation-rules-v1"

ANALYSIS_HORIZON_DAYS = 91
DEFAULT_SIMULATION_SEED = 42
DEFAULT_PROTECTION_LEVEL = 0.90

# SPECIFICATION.md section 27 leaves these values open. They are explicit and versioned here
# so identical inputs remain reproducible until a future version deliberately changes them.
MIN_COUNTERPARTY_HISTORY_COUNT = 3
DELAY_SCENARIO_DAYS = {
    "ON_TIME": 0,
    "DELAY_3_DAYS": 3,
    "DELAY_7_DAYS": 7,
    "DELAY_14_DAYS": 14,
}
DELAY_SCENARIO_WEIGHTS = {
    "ON_TIME": 0.25,
    "DELAY_3_DAYS": 0.25,
    "DELAY_7_DAYS": 0.25,
    "DELAY_14_DAYS": 0.25,
}

RISK_VERIFY_CONFIDENCE_THRESHOLD = 0.60
RISK_PREPARE_PROBABILITY_THRESHOLD = 0.25
RISK_ACT_NOW_PROBABILITY_THRESHOLD = 0.75
RISK_PREPARE_DAYS_THRESHOLD = 14
RISK_ACT_NOW_DAYS_THRESHOLD = 3
REBOUND_SAFE_TO_SPEND_DROP_WON = 10_000
MIN_ACTION_GAP_IMPROVEMENT_WON = 10_000

STALE_SOURCE_DAYS = 7
DATA_CONFIDENCE_MISSING_SOURCE_PENALTY = 0.20
DATA_CONFIDENCE_STALE_SOURCE_PENALTY = 0.15
DATA_CONFIDENCE_UNCONFIRMED_ITEM_PENALTY = 0.10
MINIMUM_DATA_CONFIDENCE = 0.10

# An installment without a fee schedule uses deterministic principal-only integer splitting.
INSTALLMENT_FEE_POLICY_VERSION = "principal-only-even-split-v1"
