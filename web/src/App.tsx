import { useState } from "react";
import { EventHeader } from "./components/EventHeader";
import { HealthBanner } from "./components/HealthBanner";
import { ErrorState, LoadingState } from "./components/StatePanels";
import { useDashboardData } from "./hooks/useDashboardData";
import { useUserObservedPrices } from "./hooks/useUserObservedPrices";
import { assertNever } from "./lib/assertNever";
import { resolveModelVersionLabel } from "./lib/status";
import { PerformancePage } from "./pages/PerformancePage";
import { WeeklyMatchupSections } from "./pages/WeeklyPage";

type Tab = "weekly" | "performance";

const App = () => {
  const { state: loadState, reload } = useDashboardData("./");
  const { getForBout, setForBout, clearForBout } = useUserObservedPrices();
  const [tab, setTab] = useState<Tab>("weekly");

  const handleSelectWeekly = () => setTab("weekly");
  const handleSelectPerformance = () => setTab("performance");
  const handleRetry = () => {
    reload();
  };

  const renderMain = () => {
    switch (loadState.status) {
      case "loading":
        return <LoadingState />;
      case "error":
        return <ErrorState message={loadState.message} onRetry={handleRetry} />;
      case "ready": {
        const modelVersionLabel = resolveModelVersionLabel({
          releaseModelHash: loadState.data.release.hashes?.model_hash,
          releaseArtifactHash: loadState.data.release.hashes?.artifact_hash,
          matchupModelHash: loadState.data.matchups.matchups[0]?.hashes?.model_hash,
          matchupArtifactHash: loadState.data.matchups.matchups[0]?.hashes?.artifact_hash,
        });

        switch (tab) {
          case "weekly":
            return (
              <>
                <EventHeader
                  event={loadState.data.currentEvent}
                  modelVersionLabel={modelVersionLabel}
                />
                <HealthBanner health={loadState.data.health} />
                <WeeklyMatchupSections
                  matchups={loadState.data.matchups}
                  getUserPrice={getForBout}
                  onSaveUserPrice={setForBout}
                  onClearUserPrice={clearForBout}
                />
                <footer className="border-t border-ink-200 pt-4 text-xs text-ink-500">
                  Release {loadState.data.release.release_id} · static JSON only · no sportsbook
                  credentials
                </footer>
              </>
            );
          case "performance":
            return (
              <>
                <PerformancePage
                  performance={loadState.data.performance}
                  history={loadState.data.history}
                />
                <footer className="border-t border-ink-200 pt-4 text-xs text-ink-500">
                  Release {loadState.data.release.release_id} · static JSON only · no sportsbook
                  credentials
                </footer>
              </>
            );
          default:
            return assertNever(tab);
        }
      }
      default:
        return assertNever(loadState);
    }
  };

  return (
    <div className="min-h-screen bg-gradient-to-b from-ink-100 via-ink-50 to-white">
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded focus:bg-white focus:px-3 focus:py-2 focus:shadow"
      >
        Skip to main content
      </a>

      <header className="border-b border-ink-200 bg-ink-950 text-ink-50">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-4 sm:px-6">
          <div>
            <p className="font-display text-xl font-bold tracking-tight">DWCS Value</p>
            <p className="text-xs text-ink-300">
              What should I bet this week, and at what price?
            </p>
          </div>
          <nav aria-label="Primary">
            <ul className="flex gap-2">
              <li>
                <button
                  type="button"
                  onClick={handleSelectWeekly}
                  aria-current={tab === "weekly" ? "page" : undefined}
                  className={`rounded px-3 py-1.5 text-sm font-semibold focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white ${
                    tab === "weekly"
                      ? "bg-accent text-white"
                      : "bg-ink-800 text-ink-100 hover:bg-ink-700"
                  }`}
                >
                  This week
                </button>
              </li>
              <li>
                <button
                  type="button"
                  onClick={handleSelectPerformance}
                  aria-current={tab === "performance" ? "page" : undefined}
                  className={`rounded px-3 py-1.5 text-sm font-semibold focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white ${
                    tab === "performance"
                      ? "bg-accent text-white"
                      : "bg-ink-800 text-ink-100 hover:bg-ink-700"
                  }`}
                >
                  Performance
                </button>
              </li>
            </ul>
          </nav>
        </div>
      </header>

      <main id="main-content" className="mx-auto max-w-6xl space-y-6 px-4 py-6 sm:px-6">
        {renderMain()}
      </main>
    </div>
  );
};

export default App;
