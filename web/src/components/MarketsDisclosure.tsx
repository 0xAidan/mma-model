import { useId, useState } from "react";
import type { MatchupMarket } from "../generated/dashboard";
import {
  formatAmerican,
  formatDecimal,
  formatEv,
  formatPercent,
} from "../lib/odds";
import { maturityLabel } from "../lib/status";

type Props = {
  markets: ReadonlyArray<MatchupMarket>;
};

export const MarketsDisclosure = ({ markets }: Props) => {
  const [open, setOpen] = useState(false);
  const panelId = useId();

  if (markets.length === 0) {
    return null;
  }

  const handleClick = () => {
    setOpen((prev) => !prev);
  };

  return (
    <div className="mt-3 border-t border-ink-200 pt-3">
      <button
        type="button"
        className="inline-flex items-center gap-2 rounded border border-ink-300 bg-ink-50 px-3 py-1.5 text-sm font-medium text-ink-900 hover:bg-ink-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={handleClick}
      >
        <span aria-hidden="true">{open ? "▾" : "▸"}</span>
        {open ? "Hide markets" : `Show markets (${markets.length})`}
      </button>
      {open ? (
        <div id={panelId} className="mt-3 overflow-x-auto">
          <table className="min-w-full text-left text-sm">
            <caption className="sr-only">Expandable market rows for this matchup</caption>
            <thead className="border-b border-ink-200 text-xs uppercase text-ink-500">
              <tr>
                <th scope="col" className="py-2 pr-3">
                  Market
                </th>
                <th scope="col" className="py-2 pr-3">
                  Selection
                </th>
                <th scope="col" className="py-2 pr-3">
                  Fair
                </th>
                <th scope="col" className="py-2 pr-3">
                  Actionable
                </th>
                <th scope="col" className="py-2 pr-3">
                  Strong
                </th>
                <th scope="col" className="py-2 pr-3">
                  EV
                </th>
                <th scope="col" className="py-2 pr-3">
                  Maturity
                </th>
                <th scope="col" className="py-2">
                  Reasons
                </th>
              </tr>
            </thead>
            <tbody>
              {markets.map((market, index) => {
                const key =
                  market.selection_id ??
                  `${market.market_family ?? "m"}-${market.outcome_key ?? index}`;
                const hasObserved = market.prices.observed != null;
                const hasEv = hasObserved && market.prices.exact_ev != null;
                const reasonText =
                  market.reason_plain ??
                  (market.reasons && market.reasons.length > 0
                    ? market.reasons.map((r) => `${r.code}: ${r.message}`).join("; ")
                    : null);
                return (
                  <tr key={key} className="border-b border-ink-100 align-top">
                    <td className="py-2 pr-3">
                      {market.market_family ?? "—"}
                      {market.is_primary ? (
                        <span className="ml-2 text-xs font-semibold text-accent">Primary</span>
                      ) : null}
                    </td>
                    <td className="py-2 pr-3">
                      {market.outcome_key ?? "—"}
                      {market.line_point != null ? ` @ ${market.line_point}` : ""}
                    </td>
                    <td className="py-2 pr-3">
                      {formatPercent(market.prices.model_fair_probability)} /{" "}
                      {formatDecimal(market.prices.fair_decimal)} /{" "}
                      {formatAmerican(market.prices.fair_american)}
                    </td>
                    <td className="py-2 pr-3">
                      {market.prices.actionable_or_better ??
                        `${formatDecimal(market.prices.actionable_decimal)} / ${formatAmerican(market.prices.actionable_american)}`}
                    </td>
                    <td className="py-2 pr-3">
                      {market.prices.strong_value_or_better ??
                        `${formatDecimal(market.prices.strong_value_decimal)} / ${formatAmerican(market.prices.strong_value_american)}`}
                    </td>
                    <td className="py-2 pr-3">
                      {hasEv && market.prices.exact_ev != null
                        ? formatEv(market.prices.exact_ev)
                        : "Hidden (no observed price)"}
                    </td>
                    <td className="py-2 pr-3">{maturityLabel(market.maturity)}</td>
                    <td className="py-2 text-xs text-ink-700">
                      {reasonText ?? "—"}
                      {market.reasons && market.reasons.length > 0 && market.reason_plain ? (
                        <ul className="mt-1 list-disc pl-4">
                          {market.reasons.map((r) => (
                            <li key={`${r.code}-${r.message}`}>
                              {r.code}: {r.message}
                            </li>
                          ))}
                        </ul>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
};
