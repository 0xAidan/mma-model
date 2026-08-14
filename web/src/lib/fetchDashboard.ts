import type {
  CurrentEventDocument,
  DashboardHealthDocument,
  HistoryDocument,
  ManifestDocument,
  MatchupsDocument,
  PerformanceDocument,
  ReleaseDocument,
} from "../generated/dashboard";

export type DashboardBundle = {
  currentEvent: CurrentEventDocument;
  matchups: MatchupsDocument;
  performance: PerformanceDocument;
  history: HistoryDocument;
  health: DashboardHealthDocument;
  release: ReleaseDocument;
  manifest: ManifestDocument;
};

const FILES = [
  "current-event.json",
  "matchups.json",
  "performance.json",
  "history.json",
  "health.json",
  "release.json",
  "manifest.json",
] as const;

export const fetchJson = async <T>(url: string): Promise<T> => {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`Failed to load ${url} (${response.status})`);
  }
  return (await response.json()) as T;
};

export const loadDashboardBundle = async (
  basePath = "./",
): Promise<DashboardBundle> => {
  const root = basePath.endsWith("/") ? basePath : `${basePath}/`;
  const [
    currentEvent,
    matchups,
    performance,
    history,
    health,
    release,
    manifest,
  ] = await Promise.all([
    fetchJson<CurrentEventDocument>(`${root}${FILES[0]}`),
    fetchJson<MatchupsDocument>(`${root}${FILES[1]}`),
    fetchJson<PerformanceDocument>(`${root}${FILES[2]}`),
    fetchJson<HistoryDocument>(`${root}${FILES[3]}`),
    fetchJson<DashboardHealthDocument>(`${root}${FILES[4]}`),
    fetchJson<ReleaseDocument>(`${root}${FILES[5]}`),
    fetchJson<ManifestDocument>(`${root}${FILES[6]}`),
  ]);

  return {
    currentEvent,
    matchups,
    performance,
    history,
    health,
    release,
    manifest,
  };
};
