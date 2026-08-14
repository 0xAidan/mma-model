import { useCallback, useEffect, useState } from "react";
import type { DashboardBundle } from "../lib/fetchDashboard";
import { loadDashboardBundle } from "../lib/fetchDashboard";

export type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: DashboardBundle };

export type DashboardDataResult = {
  state: LoadState;
  reload: () => void;
};

export const useDashboardData = (basePath = "./"): DashboardDataResult => {
  const [state, setState] = useState<LoadState>({ status: "loading" });

  const reload = useCallback(() => {
    setState({ status: "loading" });
    void loadDashboardBundle(basePath)
      .then((data) => {
        setState({ status: "ready", data });
      })
      .catch((err: unknown) => {
        const message = err instanceof Error ? err.message : "Failed to load dashboard data";
        setState({ status: "error", message });
      });
  }, [basePath]);

  useEffect(() => {
    reload();
  }, [reload]);

  return { state, reload };
};
