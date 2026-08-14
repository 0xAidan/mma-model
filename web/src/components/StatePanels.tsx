type Props = {
  label?: string;
};

export const LoadingState = ({ label = "Loading weekly dashboard…" }: Props) => (
  <div
    className="rounded-lg border border-ink-300 bg-white p-8 text-center text-ink-700"
    role="status"
    aria-live="polite"
    aria-busy="true"
  >
    <p className="font-medium">{label}</p>
  </div>
);

export const EmptyState = ({
  title = "No matchups published",
  detail = "The current release has no bout cards to show.",
}: {
  title?: string;
  detail?: string;
}) => (
  <div
    className="rounded-lg border border-ink-300 bg-white p-8 text-center"
    role="status"
  >
    <p className="font-display text-lg font-semibold text-ink-900">{title}</p>
    <p className="mt-2 text-sm text-ink-600">{detail}</p>
  </div>
);

export const ErrorState = ({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) => (
  <div
    className="rounded-lg border border-rose-700 bg-rose-50 p-8 text-center text-rose-950"
    role="alert"
  >
    <p className="font-display text-lg font-semibold">Could not load dashboard</p>
    <p className="mt-2 text-sm">{message}</p>
    {onRetry ? (
      <button
        type="button"
        onClick={onRetry}
        className="mt-4 rounded border border-rose-800 px-3 py-1.5 text-sm font-semibold hover:bg-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
      >
        Try again
      </button>
    ) : null}
  </div>
);
