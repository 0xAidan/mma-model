type MetricBar = {
  id: string;
  label: string;
  display: string;
  widthPct: number | null;
};

type Props = {
  title: string;
  summary: string;
  bars: ReadonlyArray<MetricBar>;
  testId: string;
};

export const MetricBarChart = ({ title, summary, bars, testId }: Props) => (
  <figure className="mt-4 rounded border border-ink-200 bg-ink-50 p-3" data-testid={testId}>
    <figcaption className="text-sm font-semibold text-ink-900">{title}</figcaption>
    <p className="mt-1 text-xs text-ink-600">{summary}</p>
    <ul className="mt-3 space-y-3" aria-label={title}>
      {bars.map((bar) => (
        <li key={bar.id}>
          <div className="mb-1 flex items-center justify-between gap-2 text-xs">
            <span className="font-medium text-ink-800">{bar.label}</span>
            <span className="tabular-nums text-ink-700">{bar.display}</span>
          </div>
          <div
            className="h-3 w-full overflow-hidden rounded bg-ink-200"
            role="img"
            aria-label={`${bar.label}: ${bar.display}`}
          >
            {bar.widthPct == null ? (
              <span className="sr-only">No value</span>
            ) : (
              <div
                className="h-full rounded bg-accent motion-reduce:transition-none"
                style={{ width: `${Math.max(0, Math.min(100, bar.widthPct))}%` }}
              />
            )}
          </div>
        </li>
      ))}
    </ul>
  </figure>
);
