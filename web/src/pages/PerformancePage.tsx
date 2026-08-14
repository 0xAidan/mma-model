import { useMemo, useState } from "react";
import type { ChangeEvent } from "react";
import type {
  HistoryDocument,
  HistoryPoint,
  PerformanceDocument,
  PerformanceFilters,
} from "../generated/dashboard";
import { MetricBarChart } from "../components/MetricBarChart";
import { calibrationClosenessWidth, ratioToBarWidth } from "../lib/chartScale";
import { formatPercent } from "../lib/odds";

type Props = {
  performance: PerformanceDocument;
  history: HistoryDocument;
};

type FilterKey = keyof PerformanceFilters;

const FILTER_KEYS: FilterKey[] = ["season", "market", "model", "source", "data_quality"];

const hasAnyFilterValue = (filters: PerformanceFilters | undefined): boolean => {
  if (!filters) {
    return false;
  }
  return FILTER_KEYS.some((key) => {
    const value = filters[key];
    return value != null && String(value).trim() !== "";
  });
};

const metricOrDash = (value: number | null | undefined, asPercent = false): string => {
  if (value == null || !Number.isFinite(value)) {
    return "—";
  }
  return asPercent ? formatPercent(value) : value.toFixed(4);
};

export const PerformancePage = ({ performance, history }: Props) => {
  const [season, setSeason] = useState("");
  const [market, setMarket] = useState("");
  const [model, setModel] = useState("");
  const [source, setSource] = useState("");
  const [dataQuality, setDataQuality] = useState("");

  const publishedFilters = performance.filters ?? history.filters;
  const noPublishedFilters = !hasAnyFilterValue(publishedFilters);

  const filteredPoints = useMemo(() => {
    const points = history.points ?? [];
    return points.filter((point) => {
      const hay = `${point.label} ${point.lane ?? ""} ${point.bucket}`.toLowerCase();
      if (season && !hay.includes(season.toLowerCase())) return false;
      if (market && !hay.includes(market.toLowerCase())) return false;
      if (model && !hay.includes(model.toLowerCase())) return false;
      if (source && !hay.includes(source.toLowerCase())) return false;
      if (dataQuality && !hay.includes(dataQuality.toLowerCase())) return false;
      return true;
    });
  }, [history.points, season, market, model, source, dataQuality]);

  const predictivePoints = filteredPoints.filter((p) => p.bucket === "predictive");
  const confirmedPoints = filteredPoints.filter((p) => p.bucket === "confirmed_price");
  const priceTargetPoints = filteredPoints.filter((p) => p.bucket === "price_target_only");

  const handleClearFilters = () => {
    setSeason("");
    setMarket("");
    setModel("");
    setSource("");
    setDataQuality("");
  };

  const handleSeasonChange = (event: ChangeEvent<HTMLInputElement>) => {
    setSeason(event.target.value);
  };
  const handleMarketChange = (event: ChangeEvent<HTMLInputElement>) => {
    setMarket(event.target.value);
  };
  const handleModelChange = (event: ChangeEvent<HTMLInputElement>) => {
    setModel(event.target.value);
  };
  const handleSourceChange = (event: ChangeEvent<HTMLInputElement>) => {
    setSource(event.target.value);
  };
  const handleDataQualityChange = (event: ChangeEvent<HTMLInputElement>) => {
    setDataQuality(event.target.value);
  };

  const calibration = calibrationClosenessWidth(
    performance.predictive?.calibration_slope,
    performance.predictive?.calibration_intercept,
  );

  const confirmedBars = [
    {
      id: "units",
      label: "Pick count (units risked proxy)",
      display: String(performance.confirmed_price?.pick_count ?? 0),
      widthPct: ratioToBarWidth((performance.confirmed_price?.pick_count ?? 0) / 20, 1),
    },
    {
      id: "roi",
      label: "Flat-unit ROI",
      display: metricOrDash(performance.confirmed_price?.flat_unit_roi, true),
      widthPct: ratioToBarWidth(performance.confirmed_price?.flat_unit_roi, 1),
    },
    {
      id: "clv",
      label: "CLV",
      display: metricOrDash(performance.confirmed_price?.clv, true),
      widthPct: ratioToBarWidth(performance.confirmed_price?.clv, 1),
    },
    {
      id: "drawdown",
      label: "Drawdown",
      display: metricOrDash(performance.confirmed_price?.drawdown, true),
      widthPct: ratioToBarWidth(performance.confirmed_price?.drawdown, 1),
    },
  ];

  const calibrationBars = [
    {
      id: "slope",
      label: "Calibration slope closeness to 1.0",
      display: metricOrDash(performance.predictive?.calibration_slope),
      widthPct: calibration.slopeWidth,
    },
    {
      id: "intercept",
      label: "Calibration intercept closeness to 0",
      display: metricOrDash(performance.predictive?.calibration_intercept),
      widthPct: calibration.interceptWidth,
    },
  ];

  return (
    <div className="space-y-8">
      <section aria-labelledby="perf-filters-heading" className="rounded-lg border border-ink-300 bg-white p-4 shadow-sm">
        <h2 id="perf-filters-heading" className="font-display text-xl font-semibold">
          Filters
        </h2>
        {noPublishedFilters ? (
          <p className="mt-2 text-sm text-ink-600" role="status">
            No filter applied
          </p>
        ) : (
          <p className="mt-2 text-sm text-ink-600">
            Published filter snapshot: season={publishedFilters?.season ?? "—"}, market=
            {publishedFilters?.market ?? "—"}, model={publishedFilters?.model ?? "—"}, source=
            {publishedFilters?.source ?? "—"}, data_quality=
            {publishedFilters?.data_quality ?? "—"}
          </p>
        )}
        <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <label className="text-xs font-medium text-ink-700">
            Season
            <input
              className="mt-1 w-full rounded border border-ink-300 px-2 py-1.5 text-sm"
              value={season}
              onChange={handleSeasonChange}
              aria-label="Filter by season"
            />
          </label>
          <label className="text-xs font-medium text-ink-700">
            Market
            <input
              className="mt-1 w-full rounded border border-ink-300 px-2 py-1.5 text-sm"
              value={market}
              onChange={handleMarketChange}
              aria-label="Filter by market"
            />
          </label>
          <label className="text-xs font-medium text-ink-700">
            Model
            <input
              className="mt-1 w-full rounded border border-ink-300 px-2 py-1.5 text-sm"
              value={model}
              onChange={handleModelChange}
              aria-label="Filter by model"
            />
          </label>
          <label className="text-xs font-medium text-ink-700">
            Source
            <input
              className="mt-1 w-full rounded border border-ink-300 px-2 py-1.5 text-sm"
              value={source}
              onChange={handleSourceChange}
              aria-label="Filter by source"
            />
          </label>
          <label className="text-xs font-medium text-ink-700">
            Data quality
            <input
              className="mt-1 w-full rounded border border-ink-300 px-2 py-1.5 text-sm"
              value={dataQuality}
              onChange={handleDataQualityChange}
              aria-label="Filter by data quality"
            />
          </label>
        </div>
        <button
          type="button"
          onClick={handleClearFilters}
          className="mt-3 rounded border border-ink-400 px-3 py-1.5 text-sm font-medium hover:bg-ink-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        >
          Clear filters
        </button>
      </section>

      <section
        aria-labelledby="predictive-heading"
        className="rounded-lg border border-ink-300 bg-white p-4 shadow-sm"
        data-testid="predictive-section"
      >
        <h2 id="predictive-heading" className="font-display text-xl font-semibold">
          Predictive outcomes
        </h2>
        <p className="mt-1 text-sm text-ink-600">
          Log loss, Brier, and calibration — separate from betting ROI.
        </p>
        <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-5">
          <div>
            <dt className="text-ink-500">Sample count</dt>
            <dd className="font-semibold">{performance.predictive?.sample_count ?? 0}</dd>
          </div>
          <div>
            <dt className="text-ink-500">Log loss</dt>
            <dd className="font-semibold">{metricOrDash(performance.predictive?.log_loss)}</dd>
          </div>
          <div>
            <dt className="text-ink-500">Brier</dt>
            <dd className="font-semibold">{metricOrDash(performance.predictive?.brier)}</dd>
          </div>
          <div>
            <dt className="text-ink-500">Calibration slope</dt>
            <dd className="font-semibold">
              {metricOrDash(performance.predictive?.calibration_slope)}
            </dd>
          </div>
          <div>
            <dt className="text-ink-500">Calibration intercept</dt>
            <dd className="font-semibold">
              {metricOrDash(performance.predictive?.calibration_intercept)}
            </dd>
          </div>
        </dl>
        <MetricBarChart
          title="Predictive calibration chart"
          summary="Bars show how close calibration slope is to 1.0 and intercept to 0. Empty bars mean the metric is missing. No animation is required."
          bars={calibrationBars}
          testId="calibration-chart"
        />
        <HistoryTable points={predictivePoints} caption="Predictive history points" showRoi={false} />
      </section>

      <section
        aria-labelledby="confirmed-price-heading"
        className="rounded-lg border border-ink-300 bg-white p-4 shadow-sm"
        data-testid="confirmed-price-section"
      >
        <h2 id="confirmed-price-heading" className="font-display text-xl font-semibold">
          Confirmed-price betting results
        </h2>
        <p className="mt-1 text-sm text-ink-600">
          Flat-unit ROI and CLV only for picks with an observed price.
        </p>
        <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-5">
          <div>
            <dt className="text-ink-500">Pick count</dt>
            <dd className="font-semibold">{performance.confirmed_price?.pick_count ?? 0}</dd>
          </div>
          <div>
            <dt className="text-ink-500">Hit rate</dt>
            <dd className="font-semibold">
              {metricOrDash(performance.confirmed_price?.hit_rate, true)}
            </dd>
          </div>
          <div>
            <dt className="text-ink-500">Flat-unit ROI</dt>
            <dd className="font-semibold" data-testid="confirmed-roi">
              {metricOrDash(performance.confirmed_price?.flat_unit_roi, true)}
            </dd>
          </div>
          <div>
            <dt className="text-ink-500">CLV</dt>
            <dd className="font-semibold" data-testid="confirmed-clv">
              {metricOrDash(performance.confirmed_price?.clv, true)}
            </dd>
          </div>
          <div>
            <dt className="text-ink-500">Drawdown</dt>
            <dd className="font-semibold">
              {metricOrDash(performance.confirmed_price?.drawdown, true)}
            </dd>
          </div>
        </dl>
        <MetricBarChart
          title="Confirmed-price units, ROI, CLV, and drawdown"
          summary="Accessible bar summary of confirmed-price pick count, flat-unit ROI, CLV, and drawdown. Missing values show an empty bar. Price-target-only results are never included here."
          bars={confirmedBars}
          testId="confirmed-price-chart"
        />
        <HistoryTable points={confirmedPoints} caption="Confirmed-price history" showRoi />
      </section>

      <section
        aria-labelledby="price-target-only-heading"
        className="rounded-lg border border-ink-300 bg-white p-4 shadow-sm"
        data-testid="price-target-only-section"
      >
        <h2 id="price-target-only-heading" className="font-display text-xl font-semibold">
          Price-target-only
        </h2>
        <p className="mt-1 text-sm text-ink-600">
          Sporting grades only — no ROI, no CLV, no EV in this denominator.
        </p>
        <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2">
          <div>
            <dt className="text-ink-500">Pick count</dt>
            <dd className="font-semibold">{performance.price_target_only?.pick_count ?? 0}</dd>
          </div>
          <div>
            <dt className="text-ink-500">Sporting grade count</dt>
            <dd className="font-semibold">
              {performance.price_target_only?.sporting_grade_count ?? 0}
            </dd>
          </div>
        </dl>
        <HistoryTable
          points={priceTargetPoints}
          caption="Price-target-only history"
          showRoi={false}
          forceHideRoiClv
        />
      </section>

      <section aria-labelledby="by-lane-heading" className="rounded-lg border border-ink-300 bg-white p-4 shadow-sm">
        <h2 id="by-lane-heading" className="font-display text-xl font-semibold">
          By lane (qualified / paper / experimental)
        </h2>
        <div className="mt-4 overflow-x-auto">
          <table className="min-w-full text-left text-sm">
            <caption className="sr-only">Performance metrics split by maturity lane</caption>
            <thead className="border-b border-ink-200 text-xs uppercase text-ink-500">
              <tr>
                <th scope="col" className="py-2 pr-3">
                  Lane
                </th>
                <th scope="col" className="py-2 pr-3">
                  Predictive samples
                </th>
                <th scope="col" className="py-2 pr-3">
                  Confirmed picks
                </th>
                <th scope="col" className="py-2 pr-3">
                  Confirmed ROI
                </th>
                <th scope="col" className="py-2">
                  Price-target picks
                </th>
              </tr>
            </thead>
            <tbody>
              {(performance.by_lane ?? []).map((lane) => (
                <tr key={lane.lane} className="border-b border-ink-100">
                  <td className="py-2 pr-3 capitalize">{lane.lane}</td>
                  <td className="py-2 pr-3">{lane.predictive?.sample_count ?? 0}</td>
                  <td className="py-2 pr-3">{lane.confirmed_price?.pick_count ?? 0}</td>
                  <td className="py-2 pr-3">
                    {metricOrDash(lane.confirmed_price?.flat_unit_roi, true)}
                  </td>
                  <td className="py-2">{lane.price_target_only?.pick_count ?? 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
};

const HistoryTable = ({
  points,
  caption,
  showRoi,
  forceHideRoiClv = false,
}: {
  points: ReadonlyArray<HistoryPoint>;
  caption: string;
  showRoi: boolean;
  forceHideRoiClv?: boolean;
}) => {
  const allowRoi = showRoi && !forceHideRoiClv;

  return (
    <div className="mt-4 overflow-x-auto">
      <p className="mb-2 text-xs text-ink-600">
        {points.length === 0
          ? "No history points in this bucket for the current filters."
          : `${points.length} history point(s) in this bucket.`}
      </p>
      <table className="min-w-full text-left text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead className="border-b border-ink-200 text-xs uppercase text-ink-500">
          <tr>
            <th scope="col" className="py-2 pr-3">
              When
            </th>
            <th scope="col" className="py-2 pr-3">
              Label
            </th>
            <th scope="col" className="py-2 pr-3">
              Lane
            </th>
            <th scope="col" className="py-2 pr-3">
              Value
            </th>
            {allowRoi ? (
              <>
                <th scope="col" className="py-2 pr-3">
                  ROI
                </th>
                <th scope="col" className="py-2">
                  CLV
                </th>
              </>
            ) : null}
          </tr>
        </thead>
        <tbody>
          {points.map((point) => (
            <tr key={`${point.at}-${point.label}-${point.bucket}`} className="border-b border-ink-100">
              <td className="py-2 pr-3">{point.at}</td>
              <td className="py-2 pr-3">{point.label}</td>
              <td className="py-2 pr-3">{point.lane ?? "—"}</td>
              <td className="py-2 pr-3">{metricOrDash(point.value)}</td>
              {allowRoi ? (
                <>
                  <td className="py-2 pr-3">{metricOrDash(point.flat_unit_roi, true)}</td>
                  <td className="py-2">{metricOrDash(point.clv, true)}</td>
                </>
              ) : null}
            </tr>
          ))}
        </tbody>
      </table>
      {forceHideRoiClv ? (
        <p className="mt-2 text-xs font-medium text-ink-700" data-testid="no-roi-clv-notice">
          ROI and CLV are intentionally omitted for price-target-only rows.
        </p>
      ) : null}
    </div>
  );
};
