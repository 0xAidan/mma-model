/**
 * Generated from Python Pydantic dashboard contracts (DWCS-500).
 * Do not edit by hand — regenerate via `python -m mma_model.publish.codegen`.
 */

export const DASHBOARD_SCHEMA_VERSION = 1 as const;
export const DASHBOARD_CONTRACT_ID = "dwcs_dashboard" as const;
export const DASHBOARD_CONTRACT_VERSION = "1.0.0" as const;
export const DASHBOARD_TICKET = "DWCS-500" as const;

export type ArtifactHashes = {
  readonly model_hash?: string | null;
  readonly feature_hash?: string | null;
  readonly data_hash?: string | null;
  readonly config_hash?: string | null;
  readonly artifact_hash?: string | null;
  readonly policy_hash?: string | null;
  readonly thresholds_hash?: string | null;
};

export type ConfirmedPriceMetrics = {
  readonly pick_count?: number;
  readonly hit_rate?: number | null;
  readonly flat_unit_roi?: number | null;
  readonly clv?: number | null;
  readonly drawdown?: number | null;
};

export type CountdownFields = {
  readonly event_start_at: OptionalStringField;
  readonly seconds_until_start?: number | null;
  readonly is_past?: boolean | null;
};

export type CurrentEventDocument = {
  readonly schema_version?: 1;
  readonly contract_id?: "dwcs_dashboard";
  readonly contract_version?: string;
  readonly ticket?: "DWCS-500";
  readonly series?: "dwcs";
  readonly event_id: OptionalStringField;
  readonly title: OptionalStringField;
  readonly event_date: OptionalStringField;
  readonly countdown: CountdownFields;
  readonly last_successful_update_at: OptionalStringField;
  readonly as_of: string;
};

export type DashboardHealthComponent = {
  readonly name: "pipeline" | "data" | "identity" | "odds" | "model" | "grading" | "backup" | "quota" | "freshness";
  readonly status: HealthStatusView;
  readonly detail?: string;
  readonly as_of: string;
};

export type DashboardHealthDocument = {
  readonly schema_version?: 1;
  readonly contract_id?: "dwcs_dashboard";
  readonly contract_version?: string;
  readonly ticket?: "DWCS-500";
  readonly series?: "dwcs";
  readonly as_of: string;
  readonly components: ReadonlyArray<DashboardHealthComponent>;
};

export type FieldPresence = "known" | "missing" | "unknown";

export type FighterSummary = {
  readonly fighter_id: OptionalStringField;
  readonly display_name: OptionalStringField;
  readonly corner?: "a" | "b" | "unknown";
};

export type HealthStatusView = "healthy" | "missing" | "stale" | "blocked" | "failed";

export type HistoryDocument = {
  readonly schema_version?: 1;
  readonly contract_id?: "dwcs_dashboard";
  readonly contract_version?: string;
  readonly ticket?: "DWCS-500";
  readonly series?: "dwcs";
  readonly as_of: string;
  readonly filters?: PerformanceFilters;
  readonly points?: ReadonlyArray<HistoryPoint>;
};

export type HistoryPoint = {
  readonly at: string;
  readonly label: string;
  readonly bucket: "predictive" | "confirmed_price" | "price_target_only";
  readonly lane?: PerformanceLaneView | null;
  readonly value?: number | null;
  readonly flat_unit_roi?: number | null;
  readonly clv?: number | null;
};

export type LaneMetricsBucket = {
  readonly lane: PerformanceLaneView;
  readonly predictive?: PredictiveMetrics;
  readonly confirmed_price?: ConfirmedPriceMetrics;
  readonly price_target_only?: PriceTargetOnlyMetrics;
};

export type LineFreshness = "fresh" | "stale" | "unknown";

export type ManifestDocument = {
  readonly schema_version?: 1;
  readonly contract_id?: "dwcs_dashboard";
  readonly contract_version?: string;
  readonly ticket?: "DWCS-500";
  readonly release_id: string;
  readonly files: ReadonlyArray<string>;
  readonly descriptions?: Record<string, string>;
};

export type MatchupCardChangeWarning = {
  readonly code: string;
  readonly message: string;
  readonly event_type: string;
  readonly observed_at: string;
};

export type MatchupPrices = {
  readonly model_fair_probability?: number | null;
  readonly fair_decimal?: number | null;
  readonly fair_american?: number | null;
  readonly actionable_decimal?: number | null;
  readonly actionable_american?: number | null;
  readonly strong_value_decimal?: number | null;
  readonly strong_value_american?: number | null;
  readonly observed?: ObservedPriceView | null;
  readonly exact_ev?: number | null;
  readonly line_movement?: number | null;
  readonly price_availability?: PriceAvailability;
  readonly line_freshness?: LineFreshness;
};

export type MatchupRow = {
  readonly bout_id: string;
  readonly event_id: string;
  readonly publication_id?: string | null;
  readonly primary_state: RecommendationStateView;
  readonly performance_lane: PerformanceLaneView;
  readonly market_family?: string | null;
  readonly outcome_key?: string | null;
  readonly line_point?: number | null;
  readonly selection_id?: string | null;
  readonly fighters?: ReadonlyArray<FighterSummary>;
  readonly prices: MatchupPrices;
  readonly primary_reason?: string | null;
  readonly reasons?: ReadonlyArray<ReasonBlocker>;
  readonly blockers?: ReadonlyArray<ReasonBlocker>;
  readonly card_change_warnings?: ReadonlyArray<MatchupCardChangeWarning>;
  readonly hashes?: ArtifactHashes;
  readonly detail?: string;
};

export type MatchupsDocument = {
  readonly schema_version?: 1;
  readonly contract_id?: "dwcs_dashboard";
  readonly contract_version?: string;
  readonly ticket?: "DWCS-500";
  readonly series?: "dwcs";
  readonly event_id: OptionalStringField;
  readonly as_of: string;
  readonly matchups: ReadonlyArray<MatchupRow>;
  readonly confirmed_value_ranked?: ReadonlyArray<string>;
  readonly price_target_watchlist?: ReadonlyArray<string>;
  readonly no_bet_ids?: ReadonlyArray<string>;
};

export type ObservedPriceView = {
  readonly decimal_odds: number;
  readonly american_odds: number;
  readonly sportsbook: string;
  readonly source_type: QuoteSourceTypeView;
  readonly timestamp: string;
};

export type OptionalStringField = {
  readonly presence: FieldPresence;
  readonly value?: string | null;
};

export type PerformanceDocument = {
  readonly schema_version?: 1;
  readonly contract_id?: "dwcs_dashboard";
  readonly contract_version?: string;
  readonly ticket?: "DWCS-500";
  readonly series?: "dwcs";
  readonly as_of: string;
  readonly filters?: PerformanceFilters;
  readonly predictive?: PredictiveMetrics;
  readonly confirmed_price?: ConfirmedPriceMetrics;
  readonly price_target_only?: PriceTargetOnlyMetrics;
  readonly by_lane?: ReadonlyArray<LaneMetricsBucket>;
};

export type PerformanceFilters = {
  readonly season?: string | null;
  readonly market?: string | null;
  readonly model?: string | null;
  readonly source?: string | null;
  readonly data_quality?: string | null;
};

export type PerformanceLaneView = "qualified" | "paper" | "experimental";

export type PredictiveMetrics = {
  readonly sample_count?: number;
  readonly log_loss?: number | null;
  readonly brier?: number | null;
  readonly calibration_slope?: number | null;
  readonly calibration_intercept?: number | null;
};

export type PriceAvailability = "available" | "stale" | "unavailable";

export type PriceTargetOnlyMetrics = {
  readonly pick_count?: number;
  readonly sporting_grade_count?: number;
};

export type QuoteSourceTypeView = "automatic" | "user_observed";

export type ReasonBlocker = {
  readonly code: string;
  readonly message: string;
};

export type RecommendationStateView = "confirmed_value" | "price_target" | "no_bet";

export type ReleaseDocument = {
  readonly schema_version?: 1;
  readonly contract_id?: "dwcs_dashboard";
  readonly contract_version?: string;
  readonly ticket?: "DWCS-500";
  readonly series?: "dwcs";
  readonly release_id: string;
  readonly event_id?: string | null;
  readonly window_slot?: string | null;
  readonly publications?: number;
  readonly as_of: string;
  readonly files: ReadonlyArray<ReleaseFileEntry>;
  readonly hashes?: ArtifactHashes;
};

export type ReleaseFileEntry = {
  readonly name: string;
  readonly sha256: string;
};

export type DashboardDocumentName =
  | "release.json"
  | "manifest.json"
  | "current-event.json"
  | "matchups.json"
  | "performance.json"
  | "history.json"
  | "health.json"
;

export interface DashboardReleaseFiles {
  readonly "release.json": ReleaseDocument;
  readonly "manifest.json": ManifestDocument;
  readonly "current-event.json": CurrentEventDocument;
  readonly "matchups.json": MatchupsDocument;
  readonly "performance.json": PerformanceDocument;
  readonly "history.json": HistoryDocument;
  readonly "health.json": DashboardHealthDocument;
}
