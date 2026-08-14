import type { HealthStatusView } from "../generated/dashboard";
import { healthStatusVisual } from "../lib/status";

type Props = {
  status: HealthStatusView;
  className?: string;
};

export const StatusBadge = ({ status, className = "" }: Props) => {
  const visual = healthStatusVisual(status);
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded border px-2 py-0.5 text-xs font-semibold ${visual.className} ${className}`}
      role="status"
    >
      <span aria-hidden="true">{visual.icon}</span>
      <span>{visual.label}</span>
    </span>
  );
};
