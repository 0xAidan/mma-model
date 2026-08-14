import type { RecommendationStateView } from "../generated/dashboard";
import { primaryStateVisual } from "../lib/status";

type Props = {
  state: RecommendationStateView;
};

export const PrimaryStateBadge = ({ state }: Props) => {
  const visual = primaryStateVisual(state);
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded border px-2.5 py-1 text-sm font-semibold ${visual.className}`}
      role="status"
      aria-label={`Primary recommendation: ${visual.label}`}
    >
      <span aria-hidden="true">{visual.icon}</span>
      <span>{visual.label}</span>
    </span>
  );
};
