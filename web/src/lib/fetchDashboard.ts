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

const normalizeRoot = (basePath: string): string =>
  basePath.endsWith("/") ? basePath : `${basePath}/`;

const loadBundleFromRoot = async (root: string): Promise<DashboardBundle> => {
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

/**
 * Load dashboard JSON.
 *
 * Production publishes complete sets under ``./live/`` (atomic dir swap).
 * Local Vite fixtures still live at ``./`` — fall back when ``live/`` is absent.
 */
export const loadDashboardBundle = async (
  basePath = "./",
): Promise<DashboardBundle> => {
  const root = normalizeRoot(basePath);
  const liveRoot = `${root}live/`;
  try {
    return await loadBundleFromRoot(liveRoot);
  } catch (liveErr: unknown) {
    try {
      return await loadBundleFromRoot(root);
    } catch (rootErr: unknown) {
      const liveMsg =
        liveErr instanceof Error ? liveErr.message : "live/ load failed";
      const rootMsg =
        rootErr instanceof Error ? rootErr.message : "root load failed";
      throw new Error(
        `Failed to load dashboard from ${liveRoot} (${liveMsg}); fallback ${root} (${rootMsg})`,
      );
    }
  }
};
