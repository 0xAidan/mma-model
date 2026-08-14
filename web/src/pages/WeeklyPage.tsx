import type { MatchupRow, MatchupsDocument, RecommendationStateView } from "../generated/dashboard";
import { assertNever } from "../lib/assertNever";
import type { UserObservedPrice } from "../lib/userObservedStorage";
import { EmptyState } from "../components/StatePanels";
import { MatchupCard } from "../components/MatchupCard";
import { ThresholdHelp } from "../components/ThresholdHelp";

type Props = {
  matchups: MatchupsDocument;
  getUserPrice: (boutId: string) => UserObservedPrice | undefined;
  onSaveUserPrice: (boutId: string, sportsbook: string, oddsInput: string) => boolean;
  onClearUserPrice: (boutId: string) => void;
};

const resolveOrdered = (
  preferredIds: ReadonlyArray<string> | undefined,
  rows: MatchupRow[],
): MatchupRow[] => {
  const byId = new Map(rows.map((row) => [row.bout_id, row]));
  const ordered: MatchupRow[] = [];
  const seen = new Set<string>();

  for (const id of preferredIds ?? []) {
    const row = byId.get(id);
    if (!row || seen.has(id)) {
      continue;
    }
    ordered.push(row);
    seen.add(id);
  }

  for (const row of rows) {
    if (seen.has(row.bout_id)) {
      continue;
    }
    ordered.push(row);
  }

  return ordered;
};

const bucketByPrimaryState = (
  matchups: ReadonlyArray<MatchupRow>,
): Record<RecommendationStateView, MatchupRow[]> => {
  const buckets: Record<RecommendationStateView, MatchupRow[]> = {
    confirmed_value: [],
    price_target: [],
    no_bet: [],
  };

  for (const row of matchups) {
    switch (row.primary_state) {
      case "confirmed_value":
        buckets.confirmed_value.push(row);
        break;
      case "price_target":
        buckets.price_target.push(row);
        break;
      case "no_bet":
        buckets.no_bet.push(row);
        break;
      default:
        return assertNever(row.primary_state);
    }
  }

  return buckets;
};

export const WeeklyMatchupSections = ({
  matchups,
  getUserPrice,
  onSaveUserPrice,
  onClearUserPrice,
}: Props) => {
  if (matchups.matchups.length === 0) {
    return <EmptyState />;
  }

  const buckets = bucketByPrimaryState(matchups.matchups);
  const confirmed = resolveOrdered(matchups.confirmed_value_ranked, buckets.confirmed_value);
  const watchlist = resolveOrdered(matchups.price_target_watchlist, buckets.price_target);
  const noBet = resolveOrdered(matchups.no_bet_ids, buckets.no_bet);

  const renderList = (rows: MatchupRow[], heading: string, headingId: string) => {
    if (rows.length === 0) {
      return null;
    }
    return (
      <section aria-labelledby={headingId} className="space-y-3">
        <h2 id={headingId} className="font-display text-xl font-semibold text-ink-950">
          {heading}
        </h2>
        <ul className="space-y-4">
          {rows.map((row) => (
            <li key={row.bout_id}>
              <MatchupCard
                matchup={row}
                userPrice={getUserPrice(row.bout_id)}
                onSaveUserPrice={onSaveUserPrice}
                onClearUserPrice={onClearUserPrice}
              />
            </li>
          ))}
        </ul>
      </section>
    );
  };

  return (
    <div className="space-y-8">
      <ThresholdHelp />
      {renderList(confirmed, "Confirmed-value recommendations", "confirmed-heading")}
      {renderList(watchlist, "Actionable-price watchlist", "watchlist-heading")}
      {renderList(noBet, "No bet", "nobet-heading")}
    </div>
  );
};
