export const ThresholdHelp = () => (
  <aside
    className="rounded-lg border border-ink-300 bg-white p-4 shadow-sm"
    aria-labelledby="threshold-help-heading"
    data-testid="threshold-help"
  >
    <h2 id="threshold-help-heading" className="font-display text-lg font-semibold text-ink-950">
      How to read prices and EV
    </h2>
    <ul className="mt-3 list-disc space-y-2 pl-5 text-sm text-ink-700">
      <li>
        <span className="font-semibold">“Or better”</span> means you need at least this price at any
        sportsbook (higher decimal / more plus-money American / less minus-money American). Compare
        the actionable and strong-value thresholds with whatever book you use.
      </li>
      <li>
        <span className="font-semibold">Exact EV</span> appears only when an observed automatic
        price exists. Example: 8% EV means about $8 expected profit per $100 risked at that observed
        price. Unpriced rows hide EV entirely — no zero placeholder.
      </li>
      <li>
        <span className="font-semibold">Price targets</span> have no automatic line. You must compare
        the actionable threshold with a sportsbook yourself before betting.
      </li>
    </ul>
  </aside>
);
