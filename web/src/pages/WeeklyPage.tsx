import type { MatchupRow, MatchupsDocument } from "../generated/dashboard";
import type { UserObservedPrice } from "../lib/userObservedStorage";
import { EmptyState } from "../components/StatePanels";
import { MatchupCard } from "../components/MatchupCard";

type Props = {
  matchups: MatchupsDocument;
  getUserPrice: (boutId: string) => UserObservedPrice | undefined;
  onSaveUserPrice: (boutId: string, sportsbook: string, oddsInput: string) => boolean;
  onClearUserPrice: (boutId: string) => void;
};

const resolveRows = (
  ids: ReadonlyArray<string> | undefined,
  byId: Map<string, MatchupRow>,
): MatchupRow[] => {
  if (!ids) {
    return [];
  }
  return ids.map((id) => byId.get(id)).filter((row): row is MatchupRow => row != null);
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

  const byId = new Map(matchups.matchups.map((m) => [m.bout_id, m]));
  const confirmed = resolveRows(matchups.confirmed_value_ranked, byId);
  const watchlist = resolveRows(matchups.price_target_watchlist, byId);
  const noBet = resolveRows(matchups.no_bet_ids, byId);
  const listed = new Set([
    ...confirmed.map((m) => m.bout_id),
    ...watchlist.map((m) => m.bout_id),
    ...noBet.map((m) => m.bout_id),
  ]);
  const remainder = matchups.matchups.filter((m) => !listed.has(m.bout_id));

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
      {renderList(confirmed, "Confirmed-value recommendations", "confirmed-heading")}
      {renderList(watchlist, "Actionable-price watchlist", "watchlist-heading")}
      {renderList(noBet, "No bet", "nobet-heading")}
      {renderList(remainder, "Other matchups", "other-heading")}
    </div>
  );
};
