import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";
import { describe, expect, it, beforeEach } from "vitest";
import type {
  CurrentEventDocument,
  DashboardHealthDocument,
  HistoryDocument,
  MatchupRow,
  MatchupsDocument,
  PerformanceDocument,
} from "../generated/dashboard";
import { EventHeader } from "../components/EventHeader";
import { HealthBanner } from "../components/HealthBanner";
import { MatchupCard } from "../components/MatchupCard";
import { ErrorState, EmptyState, LoadingState } from "../components/StatePanels";
import { PerformancePage } from "../pages/PerformancePage";
import { WeeklyMatchupSections } from "../pages/WeeklyPage";
import currentEventFixture from "../../public/current-event.json";
import matchupsFixture from "../../public/matchups.json";
import performanceFixture from "../../public/performance.json";
import historyFixture from "../../public/history.json";
import healthFixture from "../../public/health.json";

const matchups = matchupsFixture as MatchupsDocument;
const currentEvent = currentEventFixture as CurrentEventDocument;
const performance = performanceFixture as PerformanceDocument;
const history = historyFixture as HistoryDocument;
const health = healthFixture as DashboardHealthDocument;

const findBout = (id: string): MatchupRow => {
  const row = matchups.matchups.find((m) => m.bout_id === id);
  if (!row) {
    throw new Error(`missing bout ${id}`);
  }
  return row;
};

const noopSave = () => true;
const noopClear = () => undefined;

describe("EventHeader OptionalStringField", () => {
  it("does not invent an event title when presence is unknown", () => {
    render(<EventHeader event={currentEvent} />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Unknown event title");
    expect(screen.getByText("Unknown event date")).toBeInTheDocument();
    expect(screen.queryByText(/DWCS Season/i)).not.toBeInTheDocument();
  });
});

describe("MatchupCard primary states", () => {
  it("shows EV, observed line, and or-better thresholds for confirmed value", () => {
    const row = findBout("bout-cv");
    render(
      <MatchupCard
        matchup={row}
        userPrice={undefined}
        onSaveUserPrice={noopSave}
        onClearUserPrice={noopClear}
      />,
    );
    expect(screen.getByRole("status", { name: /Confirmed value/i })).toBeInTheDocument();
    expect(screen.getByTestId("exact-ev-row")).toHaveTextContent(/Exact EV/);
    expect(screen.getByText(/fixture_book/)).toBeInTheDocument();
    expect(screen.getByText(/\+110 or better/)).toBeInTheDocument();
    expect(screen.getByText(/\+120 or better/)).toBeInTheDocument();
  });

  it("hides EV for price-target cards and shows watchlist thresholds", () => {
    const row = findBout("bout-pt");
    render(
      <MatchupCard
        matchup={row}
        userPrice={undefined}
        onSaveUserPrice={noopSave}
        onClearUserPrice={noopClear}
      />,
    );
    expect(
      screen.getByRole("status", { name: /Actionable price target/i }),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("exact-ev-row")).not.toBeInTheDocument();
    expect(screen.queryByText(/^EV:/)).not.toBeInTheDocument();
    expect(screen.getByText(/\+110 or better/)).toBeInTheDocument();
    expect(screen.queryByText(/ROI/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/CLV/i)).not.toBeInTheDocument();
  });

  it("renders no-bet primary badge", () => {
    const row = findBout("bout-nb");
    render(
      <MatchupCard
        matchup={row}
        userPrice={undefined}
        onSaveUserPrice={noopSave}
        onClearUserPrice={noopClear}
      />,
    );
    expect(screen.getByRole("status", { name: /No bet/i })).toBeInTheDocument();
    expect(screen.queryByTestId("exact-ev-row")).not.toBeInTheDocument();
  });

  it("shows stale price state", () => {
    const row = findBout("bout-stale");
    render(
      <MatchupCard
        matchup={row}
        userPrice={undefined}
        onSaveUserPrice={noopSave}
        onClearUserPrice={noopClear}
      />,
    );
    expect(screen.getByText(/Stale line/i)).toBeInTheDocument();
    expect(screen.getByText(/Price: Stale/i)).toBeInTheDocument();
  });

  it("shows unavailable price state", () => {
    const row = findBout("bout-unavail");
    render(
      <MatchupCard
        matchup={row}
        userPrice={undefined}
        onSaveUserPrice={noopSave}
        onClearUserPrice={noopClear}
      />,
    );
    expect(screen.getByText(/Automatic price unavailable/i)).toBeInTheDocument();
    expect(screen.getByText(/Price: Unavailable/i)).toBeInTheDocument();
  });

  it("shows replacement warning", () => {
    const row = findBout("bout-repl");
    render(
      <MatchupCard
        matchup={row}
        userPrice={undefined}
        onSaveUserPrice={noopSave}
        onClearUserPrice={noopClear}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/replacement on card/i);
  });

  it("expands markets with keyboard-accessible button", async () => {
    const user = userEvent.setup();
    const row = findBout("bout-cv");
    render(
      <MatchupCard
        matchup={row}
        userPrice={undefined}
        onSaveUserPrice={noopSave}
        onClearUserPrice={noopClear}
      />,
    );
    const button = screen.getByRole("button", { name: /Show markets/i });
    expect(button).toHaveAttribute("aria-expanded", "false");
    await user.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("table")).toBeInTheDocument();
  });
});

describe("User-observed price", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("updates local EV only and has no credential fields", async () => {
    const user = userEvent.setup();
    const saves: Array<{ book: string; odds: string }> = [];
    const row = findBout("bout-pt");

    const handleSave = (boutId: string, sportsbook: string, oddsInput: string) => {
      expect(boutId).toBe("bout-pt");
      saves.push({ book: sportsbook, odds: oddsInput });
      return true;
    };

    const { rerender } = render(
      <MatchupCard
        matchup={row}
        userPrice={undefined}
        onSaveUserPrice={handleSave}
        onClearUserPrice={noopClear}
      />,
    );

    expect(document.querySelector('input[type="password"]')).toBeNull();
    expect(screen.queryByLabelText(/password|api key|login/i)).not.toBeInTheDocument();

    await user.type(screen.getByLabelText(/Sportsbook name/i), "MyBook");
    await user.type(screen.getByLabelText(/Odds you observed/i), "2.40");
    await user.click(screen.getByRole("button", { name: /^Save$/i }));

    expect(saves).toEqual([{ book: "MyBook", odds: "2.40" }]);

    rerender(
      <MatchupCard
        matchup={row}
        userPrice={{
          sportsbook: "MyBook",
          oddsInput: "2.40",
          decimalOdds: 2.4,
          americanOdds: 140,
          updatedAt: "2026-08-11T17:00:05Z",
        }}
        onSaveUserPrice={handleSave}
        onClearUserPrice={noopClear}
      />,
    );

    expect(screen.getByText(/You entered book/i)).toBeInTheDocument();
    expect(screen.getByText("MyBook")).toBeInTheDocument();
    // 0.55 * 2.4 - 1 = 0.32 -> +32.0%
    expect(screen.getByText(/Local display EV/i).parentElement).toHaveTextContent(/\+32\.0%/);
    expect(screen.queryByTestId("exact-ev-row")).not.toBeInTheDocument();
  });
});

describe("Health banner", () => {
  it("maps missing status with text and icon", () => {
    render(<HealthBanner health={health} />);
    expect(screen.getByRole("heading", { name: /System health/i })).toBeInTheDocument();
    expect(screen.getAllByText("Missing").length).toBeGreaterThan(0);
  });

  it("shows blocked overall rollup", () => {
    const blocked: DashboardHealthDocument = {
      ...health,
      components: health.components.map((c) =>
        c.name === "pipeline" || c.name === "data"
          ? { ...c, status: "blocked" as const, detail: "blocked for test" }
          : c,
      ),
    };
    render(<HealthBanner health={blocked} />);
    expect(screen.getByText(/Overall:/i).parentElement).toHaveTextContent(/Blocked/);
  });

  it("shows stale overall rollup", () => {
    const stale: DashboardHealthDocument = {
      ...health,
      components: health.components.map((c) =>
        c.name === "freshness"
          ? { ...c, status: "stale" as const, detail: "stale for test" }
          : { ...c, status: "healthy" as const },
      ),
    };
    render(<HealthBanner health={stale} />);
    expect(screen.getByText(/Overall:/i).parentElement).toHaveTextContent(/Stale/);
  });
});

describe("Loading empty error states", () => {
  it("renders loading, empty, and error panels", () => {
    const { rerender } = render(<LoadingState />);
    expect(screen.getByRole("status")).toHaveTextContent(/Loading/i);

    rerender(<EmptyState />);
    expect(screen.getByText(/No matchups published/i)).toBeInTheDocument();

    rerender(<ErrorState message="network down" />);
    expect(screen.getByRole("alert")).toHaveTextContent(/network down/i);
  });

  it("renders empty weekly section when matchups array is empty", () => {
    const empty: MatchupsDocument = {
      ...matchups,
      matchups: [],
      confirmed_value_ranked: [],
      price_target_watchlist: [],
      no_bet_ids: [],
    };
    render(
      <WeeklyMatchupSections
        matchups={empty}
        getUserPrice={() => undefined}
        onSaveUserPrice={noopSave}
        onClearUserPrice={noopClear}
      />,
    );
    expect(screen.getByText(/No matchups published/i)).toBeInTheDocument();
  });
});

describe("Performance page denominators", () => {
  it("does not show ROI or CLV on price-target-only bucket", () => {
    render(<PerformancePage performance={performance} history={history} />);
    const section = screen.getByTestId("price-target-only-section");
    expect(within(section).queryByText(/^ROI$/i)).not.toBeInTheDocument();
    expect(within(section).queryByText(/^CLV$/i)).not.toBeInTheDocument();
    expect(within(section).getByTestId("no-roi-clv-notice")).toBeInTheDocument();
    expect(within(section).getByText(/Pick count/i)).toBeInTheDocument();
    expect(within(section).queryByText(/Exact EV/i)).not.toBeInTheDocument();
  });

  it("shows confirmed-price ROI/CLV labels in their own section only", () => {
    render(<PerformancePage performance={performance} history={history} />);
    const confirmed = screen.getByTestId("confirmed-price-section");
    expect(within(confirmed).getByTestId("confirmed-roi")).toBeInTheDocument();
    expect(within(confirmed).getByTestId("confirmed-clv")).toBeInTheDocument();
  });

  it("shows No filter applied when filter metadata is empty", () => {
    render(<PerformancePage performance={performance} history={history} />);
    expect(screen.getByText(/No filter applied/i)).toBeInTheDocument();
  });
});

describe("accessibility smoke", () => {
  it("matchup card has no serious axe violations", async () => {
    const { container } = render(
      <MatchupCard
        matchup={findBout("bout-cv")}
        userPrice={undefined}
        onSaveUserPrice={noopSave}
        onClearUserPrice={noopClear}
      />,
    );
    const results = await axe(container);
    const serious = results.violations.filter(
      (v) => v.impact === "serious" || v.impact === "critical",
    );
    expect(serious).toEqual([]);
  });
});

describe("odds helpers used by local EV", () => {
  it("does not mutate published JSON when user price is shown", () => {
    const row = findBout("bout-pt");
    const before = structuredClone(row);
    render(
      <MatchupCard
        matchup={row}
        userPrice={{
          sportsbook: "X",
          oddsInput: "2.0",
          decimalOdds: 2,
          americanOdds: 100,
          updatedAt: "2026-08-11T17:00:05Z",
        }}
        onSaveUserPrice={noopSave}
        onClearUserPrice={noopClear}
      />,
    );
    expect(row).toEqual(before);
  });
});
