import type { MatchupRow, PriceAvailability } from "../generated/dashboard";
import { assertNever } from "../lib/assertNever";
import {
  formatAmerican,
  formatDecimal,
  formatEv,
  formatPercent,
} from "../lib/odds";
import { readOptionalString } from "../lib/optionalString";
import {
  maturityLabel,
  priceAvailabilityLabel,
  uncertaintyCopy,
} from "../lib/status";
import type { UserObservedPrice } from "../lib/userObservedStorage";
import { MarketsDisclosure } from "./MarketsDisclosure";
import { PrimaryStateBadge } from "./PrimaryStateBadge";
import { UserObservedPriceForm } from "./UserObservedPriceForm";

type Props = {
  matchup: MatchupRow;
  userPrice: UserObservedPrice | undefined;
  onSaveUserPrice: (boutId: string, sportsbook: string, oddsInput: string) => boolean;
  onClearUserPrice: (boutId: string) => void;
};

const formatLineMovement = (value: number): string => {
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(3)}`;
};

const describeAvailability = (
  availability: PriceAvailability | undefined,
): { badge: string | null } => {
  if (availability == null) {
    return { badge: null };
  }
  switch (availability) {
    case "available":
    case "stale":
    case "unavailable":
      return { badge: priceAvailabilityLabel(availability) };
    default:
      return assertNever(availability);
  }
};

export const MatchupCard = ({
  matchup,
  userPrice,
  onSaveUserPrice,
  onClearUserPrice,
}: Props) => {
  const fighters =
    matchup.fighters
      ?.map((f) => readOptionalString(f.display_name, "Unknown fighter").text)
      .join(" vs ") ?? "Unknown fighters";

  const prices = matchup.prices;
  const observed = prices.observed;
  const hasPublishedEv = observed != null && prices.exact_ev != null;
  const availability = prices.price_availability;
  const freshness = prices.line_freshness;
  const availabilityUi = describeAvailability(availability);
  const isPriceTarget = matchup.primary_state === "price_target";

  return (
    <article
      className="rounded-lg border border-ink-300 bg-white p-4 shadow-sm"
      aria-labelledby={`matchup-${matchup.bout_id}`}
      data-bout-id={matchup.bout_id}
      data-primary-state={matchup.primary_state}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 id={`matchup-${matchup.bout_id}`} className="font-display text-lg font-semibold">
            {fighters}
          </h3>
          <p className="text-xs text-ink-500">
            {matchup.market_family ?? "market"} · {matchup.outcome_key ?? "selection"}
          </p>
        </div>
        <PrimaryStateBadge state={matchup.primary_state} />
      </div>

      <div className="mt-3 flex flex-wrap gap-2 text-xs">
        <span className="rounded border border-ink-300 bg-ink-50 px-2 py-0.5 font-medium">
          Maturity: {maturityLabel(matchup.maturity)}
        </span>
        <span className="rounded border border-ink-300 bg-ink-50 px-2 py-0.5 font-medium">
          Lane: {maturityLabel(matchup.performance_lane)}
        </span>
        {availabilityUi.badge ? (
          <span
            className="rounded border border-ink-300 bg-ink-50 px-2 py-0.5 font-medium"
            role="status"
          >
            Price: {availabilityUi.badge}
            {freshness ? ` · line ${freshness}` : ""}
          </span>
        ) : null}
      </div>

      <p className="mt-2 text-xs text-ink-600" data-testid="uncertainty-copy">
        {uncertaintyCopy(matchup.maturity)}
      </p>

      {isPriceTarget ? (
        <p
          className="mt-3 rounded border border-amber-700 bg-amber-50 px-3 py-2 text-sm text-amber-950"
          role="status"
          data-testid="compare-sportsbook-copy"
        >
          <span aria-hidden="true">◎ </span>
          Automatic lines are unavailable for this pick. Compare the actionable threshold with a
          sportsbook before betting.
        </p>
      ) : null}

      {matchup.card_change_warnings && matchup.card_change_warnings.length > 0 ? (
        <ul className="mt-3 space-y-1" aria-label="Card change warnings">
          {matchup.card_change_warnings.map((warning) => (
            <li
              key={`${warning.code}-${warning.observed_at}`}
              className="rounded border border-amber-700 bg-amber-50 px-3 py-2 text-sm text-amber-950"
              role="alert"
            >
              <span aria-hidden="true">⚠ </span>
              Replacement / card change: {warning.message} ({warning.event_type})
            </li>
          ))}
        </ul>
      ) : null}

      {availability === "stale" || freshness === "stale" ? (
        <p
          className="mt-3 rounded border border-amber-700 bg-amber-50 px-3 py-2 text-sm text-amber-950"
          role="status"
        >
          <span aria-hidden="true">◷ </span>
          Stale line — treat automatic quotes carefully.
        </p>
      ) : null}

      {availability === "unavailable" ? (
        <p
          className="mt-3 rounded border border-slate-600 bg-slate-100 px-3 py-2 text-sm text-slate-900"
          role="status"
        >
          <span aria-hidden="true">○ </span>
          Automatic price unavailable — use fair / actionable thresholds below.
        </p>
      ) : null}

      <p className="mt-3 text-xs text-ink-600">
        “Or better” thresholds are the minimum price you need at any sportsbook.
      </p>

      <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-3">
        <div>
          <dt className="text-ink-500">Fair probability</dt>
          <dd className="font-semibold">{formatPercent(prices.model_fair_probability)}</dd>
        </div>
        <div>
          <dt className="text-ink-500">Fair odds</dt>
          <dd className="font-semibold">
            {formatDecimal(prices.fair_decimal)} decimal / {formatAmerican(prices.fair_american)}{" "}
            American
            {prices.fair_or_better ? (
              <span className="mt-0.5 block text-xs font-normal text-ink-600">
                {prices.fair_or_better}
              </span>
            ) : null}
          </dd>
        </div>
        <div>
          <dt className="text-ink-500">Actionable threshold</dt>
          <dd className="font-semibold">
            {prices.actionable_or_better ??
              `${formatDecimal(prices.actionable_decimal)} / ${formatAmerican(prices.actionable_american)}`}
            {prices.actionable_decimal != null || prices.actionable_american != null ? (
              <span className="mt-0.5 block text-xs font-normal text-ink-600">
                {formatDecimal(prices.actionable_decimal)} /{" "}
                {formatAmerican(prices.actionable_american)}
              </span>
            ) : null}
          </dd>
        </div>
        <div>
          <dt className="text-ink-500">Strong-value threshold</dt>
          <dd className="font-semibold">
            {prices.strong_value_or_better ??
              `${formatDecimal(prices.strong_value_decimal)} / ${formatAmerican(prices.strong_value_american)}`}
            {prices.strong_value_decimal != null || prices.strong_value_american != null ? (
              <span className="mt-0.5 block text-xs font-normal text-ink-600">
                {formatDecimal(prices.strong_value_decimal)} /{" "}
                {formatAmerican(prices.strong_value_american)}
              </span>
            ) : null}
          </dd>
        </div>
        {observed ? (
          <div>
            <dt className="text-ink-500">Observed line (automatic)</dt>
            <dd className="font-semibold">
              {formatDecimal(observed.decimal_odds)} / {formatAmerican(observed.american_odds)}
              <span className="mt-0.5 block text-xs font-normal text-ink-600">
                {observed.sportsbook} · {observed.source_label} · {observed.timestamp}
              </span>
            </dd>
          </div>
        ) : null}
        {prices.line_movement != null ? (
          <div data-testid="line-movement-row">
            <dt className="text-ink-500">Line movement</dt>
            <dd className="font-semibold">{formatLineMovement(prices.line_movement)}</dd>
          </div>
        ) : null}
        {hasPublishedEv && prices.exact_ev != null ? (
          <div data-testid="exact-ev-row">
            <dt className="text-ink-500">Exact EV (published)</dt>
            <dd className="font-semibold">
              {formatEv(prices.exact_ev)}
              <span className="mt-0.5 block text-xs font-normal text-ink-600" data-testid="ev-help">
                {`${(prices.exact_ev * 100).toFixed(0)}% EV means about $${(prices.exact_ev * 100).toFixed(0)} expected profit per $100 risked at this observed price.`}
              </span>
            </dd>
          </div>
        ) : null}
      </dl>

      {matchup.reason_plain ? (
        <p className="mt-4 text-sm text-ink-800">
          <span className="font-semibold">Why: </span>
          {matchup.reason_plain}
        </p>
      ) : null}

      {matchup.reasons && matchup.reasons.length > 0 ? (
        <ul className="mt-2 list-disc pl-5 text-xs text-ink-600" aria-label="Machine reasons">
          {matchup.reasons.map((r) => (
            <li key={`${r.code}-${r.message}`}>
              {r.code}: {r.message}
            </li>
          ))}
        </ul>
      ) : null}

      {matchup.blockers && matchup.blockers.length > 0 ? (
        <ul className="mt-2 list-disc pl-5 text-xs text-rose-800" aria-label="Blockers">
          {matchup.blockers.map((b) => (
            <li key={`${b.code}-${b.message}`}>
              Blocker {b.code}: {b.message}
            </li>
          ))}
        </ul>
      ) : null}

      <MarketsDisclosure markets={matchup.markets ?? []} />

      <UserObservedPriceForm
        boutId={matchup.bout_id}
        fairProbability={prices.model_fair_probability}
        existing={userPrice}
        onSave={onSaveUserPrice}
        onClear={onClearUserPrice}
      />
    </article>
  );
};
