export type RiskStatus = "STABLE" | "VERIFY" | "PREPARE" | "ACT_NOW";

export type AnalysisStatus =
  | "QUEUED"
  | "RUNNING"
  | "SUCCEEDED"
  | "FAILED"
  | "SUPERSEDED";

export type InterpretationStatus =
  | "NOT_REQUESTED"
  | "QUEUED"
  | "RUNNING"
  | "SUCCEEDED"
  | "FALLBACK"
  | "FAILED"
  | "STALE";

export type AnalysisExecutionStage =
  | "QUEUED"
  | "SNAPSHOT_BUILDING"
  | "BASELINE_ANALYZING"
  | "AGENT_INVESTIGATING"
  | "PLAN_EVALUATING"
  | "REPORT_BUILDING"
  | "INTERPRETATION_REQUESTING"
  | "INTERPRETATION_VALIDATING"
  | "COMPLETED"
  | "FAILED";

export interface BindingConstraint {
  date?: string | null;
  event_id?: string | null;
  reason?: string | null;
}

export interface SafeToSpend {
  safe_to_spend: number;
  protection_level?: number;
  binding_constraint?: BindingConstraint | null;
  data_confidence?: number;
}

export interface RiskMetrics {
  shortfall_probability?: number;
  payment_account_shortfall_probability?: number;
  total_liquidity_shortfall_probability?: number;
  first_risk_date?: string | null;
  shortfall_type?: "PAYMENT_ACCOUNT" | "TOTAL_LIQUIDITY" | null;
  expected_gap_min?: number;
  expected_gap_max?: number;
  days_until_risk?: number | null;
  data_confidence?: number;
}

export interface RiskPresentation {
  status?: RiskStatus;
  status_label?: string;
  title?: string;
  impact?: string;
  cause?: string;
  recommended_action?: string;
  confidence_label?: string;
}

export interface DataQuality {
  missing_sources?: string[];
  stale_sources?: string[];
  unconfirmed_items?: unknown[];
}

export interface ActionDefinition {
  action_id?: string;
  type?: string;
  parameters?: Record<string, unknown>;
  requires_user_approval?: boolean;
  assumptions?: string[];
}

export interface RiskShift {
  detected?: boolean;
  source_date?: string | null;
  target_date?: string | null;
  message?: string;
  reasons?: string[];
}

export interface EvaluationState {
  status?: RiskStatus;
  status_label?: string;
  safe_to_spend?: number;
  risk_metrics?: RiskMetrics;
}

export interface RecommendationDerivation {
  amount: number;
  currency: "KRW";
  source_field: string;
  source_value: number;
  formula: string;
  evidence_ids: string[];
  snapshot_id: string;
  revision: string;
  tool_version: string | null;
  policy_version: string;
}

export interface RecommendationComparisonCandidate {
  candidate_id: string;
  type: string | null;
  amount: number | null;
  selected: boolean;
  feasible: boolean;
  after_expected_gap_max: number | null;
  risk_shift: RiskShift | null;
  policy_violations: PolicyViolation[];
  rejection_reason: string | null;
}

export interface Recommendation {
  recommendation_id?: string;
  id?: string;
  snapshot_id?: string;
  current_state_revision?: string;
  title?: string;
  summary?: string;
  rationale?: string;
  reason?: string;
  status?: string;
  rank?: number;
  action?: ActionDefinition;
  actions?: ActionDefinition[];
  before?: EvaluationState;
  after?: EvaluationState;
  evaluation?: {
    valid?: boolean;
    before?: EvaluationState;
    after?: EvaluationState;
    risk_shift?: RiskShift;
    policy_violations?: PolicyViolation[];
    tool_version?: string;
  };
  risk_shift?: RiskShift;
  policy_violations?: PolicyViolation[];
  policy_result?: {
    valid?: boolean;
    violations?: PolicyViolation[];
    requires_user_approval?: boolean;
    policy_version?: string;
  };
  requires_user_approval?: boolean;
  alternatives?: Recommendation[];
  derivation?: RecommendationDerivation;
  comparison_candidates?: RecommendationComparisonCandidate[];
}

export interface PolicyViolation {
  code?: string;
  message?: string;
  action_id?: string;
}

export interface AgentTraceStep {
  sequence: number;
  kind: string;
  title: string;
  summary: string;
  status: string;
  source?: "AI" | "DETERMINISTIC";
  phase?: number | null;
  reason?: string | null;
  tool_name?: string | null;
  candidate_id?: string | null;
  evidence_ids?: string[];
}

export type AgentDecisionMode =
  | "DETERMINISTIC"
  | "AI_INVESTIGATED"
  | "AI_PARTIAL";

export interface AgentDecisionTrace {
  mode: AgentDecisionMode;
  model?: string | null;
  status: string;
  fallback_reason?: string | null;
  disclosure: string;
  steps: AgentTraceStep[];
  evidence_summary?: string[];
  unresolved_questions?: string[];
  usage?: {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
  } | null;
}

export interface InterpretationRankedAction {
  actionId: string;
  priority: number;
  reason: string;
}

export interface InterpretationPayload {
  schemaVersion?: "1.1";
  contractVersion?: "1.1";
  promptVersion?: string;
  requestId?: string;
  idempotencyKey?: string;
  analysisId?: string;
  snapshotId?: string;
  snapshotRevision?: string;
  locale?: string;
  riskExplanation?: string;
  rankedActions?: InterpretationRankedAction[];
  userMessage?: string;
  source?: "AI" | "DETERMINISTIC_FALLBACK";
  fallbackReason?: string | null;
}

export interface DashboardResponse {
  analysis_id?: string;
  analysis_run_id?: string;
  snapshot_id?: string;
  as_of?: string;
  revision?: string;
  analysis_revision?: string;
  report_revision?: string | null;
  latest_data_revision?: string;
  is_stale?: boolean;
  analysis_required?: boolean;
  analysis_status?: AnalysisStatus;
  interpretation_status?: InterpretationStatus;
  execution_stage?: AnalysisExecutionStage;
  refresh_status?: AnalysisStatus;
  interpretation?: InterpretationPayload | null;
  is_virtual?: boolean;
  safe_to_spend?: SafeToSpend | number | null;
  risk_metrics?: RiskMetrics;
  presentation?: RiskPresentation;
  recommendation?: Recommendation | null;
  decision_trace?: AgentDecisionTrace | null;
  data_quality?: DataQuality;
  last_successful_analysis_at?: string | null;
}

export interface DailyPosition {
  date: string;
  account_balances?: Record<string, number>;
  total_balance?: number;
  available_balance?: number;
  liquidity_margin?: number;
  payment_account_margin?: number;
  safety_margin?: number;
  worst_case_safety_margin?: number;
  protected_balance?: number;
  triggering_event_ids?: string[];
  status?: RiskStatus;
}

export interface WeeklyPosition {
  week?: number;
  start_date?: string;
  end_date?: string;
  min_available_balance?: number;
  min_safety_margin?: number;
  status?: RiskStatus;
  causes?: string[];
}

export interface Scenario {
  id?: string;
  name?: string;
  label?: string;
  scenario?: string;
  scenario_label?: string;
  status?: RiskStatus;
}

export interface TimelineResponse {
  as_of?: string;
  horizon_days?: number;
  balance_basis?: string;
  balance_basis_label?: string;
  requires_reanalysis?: boolean;
  daily_positions?: DailyPosition[];
  weekly_positions?: WeeklyPosition[];
  scenarios?: Array<Scenario | string>;
}

export interface NextRiskResponse {
  as_of?: string;
  risk_metrics?: RiskMetrics;
  presentation?: RiskPresentation;
  triggering_events?: Array<Record<string, unknown>>;
  causes?: string[];
  data_confidence?: number;
}

export interface Receivable {
  receivable_id?: string;
  event_id?: string;
  counterparty_id?: string;
  counterparty_name?: string;
  amount?: number;
  expected_date?: string;
  status?: "CONFIRMED" | "ESTIMATED" | "OVERDUE" | "RECEIVED" | "CANCELLED";
  destination_account_id?: string;
  user_confirmed?: boolean;
  source?: string;
  updated_at?: string;
  average_delay_days?: number;
  payment_history_count?: number;
  recent_trend?: string;
  data_confidence?: number;
  counterparty?: {
    counterparty_id?: string;
    name?: string;
  };
  evidence?: {
    average_delay_days?: number;
    payment_history_count?: number;
    recent_trend?: string;
    data_confidence?: number;
  };
  counterparty_evidence?: {
    average_delay_days?: number;
    payment_history_count?: number;
    recent_trend?: string;
    data_confidence?: number;
  };
}

export interface Account {
  account_id?: string;
  name?: string;
  account_name?: string;
  account_type?: string;
  current_balance?: number;
  available_balance?: number;
}

export interface Card {
  card_id?: string;
  name?: string;
  payment_account_id?: string;
  payment_day?: number;
  current_billing_amount?: number;
  billing_date?: string;
  updated_at?: string;
}

export interface Counterparty {
  counterparty_id?: string;
  name?: string;
  counterparty_type?: string;
  is_recurring?: boolean;
  payment_history_count?: number;
}

export interface InstallmentPlan {
  installment_plan_id?: string;
  card_id?: string;
  card_name?: string;
  merchant_name?: string;
  description?: string;
  original_amount?: number;
  monthly_payment?: number;
  total_months?: number;
  remaining_months?: number;
  next_payment_date?: string;
  status?: string;
}

export type LabelEntityKind =
  | "CLIENT"
  | "MERCHANT"
  | "PLATFORM"
  | "CARD_PAYMENT"
  | "OTHER";

export type LabelCategoryHint =
  | "RECEIVABLE"
  | "CARD_BILL"
  | "INSTALLMENT_PAYMENT"
  | "RENT"
  | "INSURANCE"
  | "UTILITY"
  | "LOAN_PAYMENT"
  | "TAX"
  | "SAVINGS"
  | "DISCRETIONARY_EXPENSE"
  | "OTHER_INFLOW"
  | "OTHER_OUTFLOW";

export interface ImportCandidate {
  candidate_id?: string;
  id?: string;
  transaction_id?: string;
  type?: string;
  candidate_type?: string;
  description?: string;
  counterparty_name?: string;
  amount?: number;
  confidence?: number;
  status?: "PENDING" | "CONFIRMED" | "REJECTED" | "UNKNOWN";
  suggested_category?: string;
  occurrences?: number;
  evidence_transaction_ids?: string[];
  classification_group?: {
    source: "AI";
    group_id: string;
    normalized_name: string;
    entity_kind: LabelEntityKind;
    category_hint: LabelCategoryHint;
    essential_hint: boolean | null;
    confidence: "HIGH" | "MEDIUM" | "LOW";
    reason: string;
    labels: string[];
    can_split: boolean;
  };
  proposed_record?: {
    amount?: number;
    original_amount?: number;
    total_months?: number;
    card_id?: string;
    next_payment_date?: string;
    counterparty_id?: string;
    counterparty_name?: string;
    destination_account_id?: string;
    account_id?: string;
    expected_date?: string;
    event_type?: string;
    is_essential?: boolean;
    description?: string;
    source_transaction_id?: string;
    [key: string]: unknown;
  };
}

export interface ImportResponse {
  revision: string;
  analysis_required: boolean;
  is_demo?: boolean;
  analysis_as_of?: string;
  import_id?: string;
  imported_count?: number;
  imported_counts?: Record<string, number>;
  duplicate_count?: number;
  candidates?: ImportCandidate[];
  detected_candidates?: ImportCandidate[];
  recurring_income_candidates?: ImportCandidate[];
  fixed_expense_candidates?: ImportCandidate[];
  installment_candidates?: ImportCandidate[];
  classification_status?:
    | "SUCCEEDED"
    | "REJECTED"
    | "FAILED"
    | "NOT_REQUESTED";
  classification_summary?: {
    source: "AI" | "AI_SHADOW" | "DETERMINISTIC_FALLBACK";
    applied: boolean;
    original_label_count: number;
    grouped_entity_count: number;
    merged_label_count: number;
    user_split_group_count?: number;
  };
  message?: string;
}

export interface CandidateSplitResponse {
  split: true;
  candidate_id: string;
  candidates: ImportCandidate[];
  revision: string;
  analysis_required: true;
}

export interface AnalysisResponse {
  analysis_id?: string;
  analysis_run_id?: string;
  revision?: string;
  analysis_revision?: string;
  report_revision?: string | null;
  latest_data_revision?: string;
  is_stale?: boolean;
  analysis_required?: boolean;
  status?: string;
  analysis_status?: AnalysisStatus;
  interpretation_status?: InterpretationStatus;
  execution_stage?: AnalysisExecutionStage;
  refresh_status?: AnalysisStatus;
  interpretation?: InterpretationPayload | null;
  before?: EvaluationState;
  after?: EvaluationState;
  risk_shift?: RiskShift;
  presentation?: RiskPresentation;
  recommendation?: Recommendation;
}

export interface SetupCommitResponse {
  preferences: Record<string, unknown>;
  candidates: ImportCandidate[];
  revision: string;
  analysis_required: boolean;
}

export interface DemoResetResponse {
  reset: boolean;
  revision: string;
  analysis_required: false;
}

export interface ActionEvaluation {
  valid?: boolean;
  before?: EvaluationState;
  after?: EvaluationState;
  risk_shift?: RiskShift;
  policy_violations?: PolicyViolation[];
  requires_user_approval?: boolean;
  tool_version?: string;
}

export interface InstallmentPrecheckResponse {
  snapshot_id?: string;
  action?: ActionDefinition;
  evaluation?: ActionEvaluation;
  policy?: {
    valid?: boolean;
    violations?: PolicyViolation[];
    requires_user_approval?: boolean;
    policy_version?: string;
  };
  virtual_only?: boolean;
  external_actions_executed?: boolean;
}
