import type { DashboardHealthDocument } from "../generated/dashboard";
import { healthStatusVisual, rollupHealthStatus } from "../lib/status";
import { StatusBadge } from "./StatusBadge";

type Props = {
  health: DashboardHealthDocument;
};

export const HealthBanner = ({ health }: Props) => {
  const statuses = health.components.map((c) => c.status);
  const overall = rollupHealthStatus(statuses);
  const overallVisual = healthStatusVisual(overall);
  const pipeline = health.components.find((c) => c.name === "pipeline");
  const data = health.components.find((c) => c.name === "data");

  return (
    <section
      className="rounded-lg border border-ink-300 bg-white p-4 shadow-sm"
      aria-labelledby="health-heading"
      aria-live="polite"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 id="health-heading" className="font-display text-lg font-semibold text-ink-900">
            System health
          </h2>
          <p className="mt-1 text-sm text-ink-600">
            Overall:{" "}
            <span className="font-semibold">
              <span aria-hidden="true">{overallVisual.icon}</span> {overallVisual.label}
            </span>
          </p>
        </div>
        <StatusBadge status={overall} />
      </div>

      <ul className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {pipeline ? (
          <li className="flex items-start justify-between gap-2 rounded border border-ink-200 bg-ink-50 px-3 py-2">
            <div>
              <p className="text-sm font-medium text-ink-900">Pipeline</p>
              {pipeline.detail ? (
                <p className="text-xs text-ink-600">{pipeline.detail}</p>
              ) : null}
            </div>
            <StatusBadge status={pipeline.status} />
          </li>
        ) : null}
        {data ? (
          <li className="flex items-start justify-between gap-2 rounded border border-ink-200 bg-ink-50 px-3 py-2">
            <div>
              <p className="text-sm font-medium text-ink-900">Data</p>
              {data.detail ? <p className="text-xs text-ink-600">{data.detail}</p> : null}
            </div>
            <StatusBadge status={data.status} />
          </li>
        ) : null}
        {health.components
          .filter((c) => c.name !== "pipeline" && c.name !== "data")
          .map((component) => (
            <li
              key={component.name}
              className="flex items-start justify-between gap-2 rounded border border-ink-200 bg-ink-50 px-3 py-2"
            >
              <div>
                <p className="text-sm font-medium capitalize text-ink-900">{component.name}</p>
                {component.detail ? (
                  <p className="text-xs text-ink-600">{component.detail}</p>
                ) : null}
              </div>
              <StatusBadge status={component.status} />
            </li>
          ))}
      </ul>
    </section>
  );
};
