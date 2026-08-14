import type {
  HealthStatusView,
  PerformanceLaneView,
  PriceAvailability,
  RecommendationStateView,
} from "../generated/dashboard";
import { assertNever } from "./assertNever";

export type StatusVisual = {
  label: string;
  icon: string;
  className: string;
};

export const primaryStateVisual = (state: RecommendationStateView): StatusVisual => {
  switch (state) {
    case "confirmed_value":
      return {
        label: "Confirmed value",
        icon: "✓",
        className: "bg-emerald-100 text-emerald-950 border-emerald-700",
      };
    case "price_target":
      return {
        label: "Actionable price target",
        icon: "◎",
        className: "bg-amber-100 text-amber-950 border-amber-700",
      };
    case "no_bet":
      return {
        label: "No bet",
        icon: "⊘",
        className: "bg-slate-200 text-slate-900 border-slate-600",
      };
    default: {
      const _exhaustive: never = state;
      return _exhaustive;
    }
  }
};

export const healthStatusVisual = (status: HealthStatusView): StatusVisual => {
  switch (status) {
    case "healthy":
      return {
        label: "Healthy",
        icon: "●",
        className: "bg-emerald-100 text-emerald-950 border-emerald-700",
      };
    case "missing":
      return {
        label: "Missing",
        icon: "○",
        className: "bg-slate-200 text-slate-900 border-slate-600",
      };
    case "stale":
      return {
        label: "Stale",
        icon: "◷",
        className: "bg-amber-100 text-amber-950 border-amber-700",
      };
    case "blocked":
      return {
        label: "Blocked",
        icon: "■",
        className: "bg-orange-100 text-orange-950 border-orange-700",
      };
    case "failed":
      return {
        label: "Failed",
        icon: "✕",
        className: "bg-rose-100 text-rose-950 border-rose-700",
      };
    default: {
      const _exhaustive: never = status;
      return _exhaustive;
    }
  }
};

const HEALTH_RANK: Record<HealthStatusView, number> = {
  healthy: 0,
  missing: 1,
  stale: 2,
  blocked: 3,
  failed: 4,
};

export const rollupHealthStatus = (
  statuses: ReadonlyArray<HealthStatusView>,
): HealthStatusView => {
  if (statuses.length === 0) {
    return "missing";
  }
  let worst: HealthStatusView = "healthy";
  for (const status of statuses) {
    if (HEALTH_RANK[status] > HEALTH_RANK[worst]) {
      worst = status;
    }
  }
  return worst;
};

export const maturityLabel = (lane: PerformanceLaneView): string => {
  switch (lane) {
    case "qualified":
      return "Qualified";
    case "paper":
      return "Paper";
    case "experimental":
      return "Experimental";
    default:
      return assertNever(lane);
  }
};

export const uncertaintyCopy = (lane: PerformanceLaneView): string => {
  switch (lane) {
    case "qualified":
      return "Production-qualified lane — treated as the trusted decision surface.";
    case "paper":
      return "Higher uncertainty — paper lane; treat as provisional guidance.";
    case "experimental":
      return "Higher uncertainty — experimental lane; do not treat as production-ready.";
    default:
      return assertNever(lane);
  }
};

export const priceAvailabilityLabel = (availability: PriceAvailability): string => {
  switch (availability) {
    case "available":
      return "Available";
    case "stale":
      return "Stale";
    case "unavailable":
      return "Unavailable";
    default:
      return assertNever(availability);
  }
};

export const shortenDigest = (value: string | null | undefined, chars = 12): string | null => {
  if (!value || value.trim() === "") {
    return null;
  }
  return value.length <= chars ? value : `${value.slice(0, chars)}…`;
};

export const resolveModelVersionLabel = (args: {
  releaseModelHash?: string | null | undefined;
  releaseArtifactHash?: string | null | undefined;
  matchupModelHash?: string | null | undefined;
  matchupArtifactHash?: string | null | undefined;
}): string => {
  const digest =
    shortenDigest(args.releaseModelHash) ??
    shortenDigest(args.releaseArtifactHash) ??
    shortenDigest(args.matchupModelHash) ??
    shortenDigest(args.matchupArtifactHash);
  return digest ?? "Unknown model version";
};
