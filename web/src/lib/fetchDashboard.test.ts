import { afterEach, describe, expect, it, vi } from "vitest";
import { loadDashboardBundle } from "./fetchDashboard";

const FILES = [
  "current-event.json",
  "matchups.json",
  "performance.json",
  "history.json",
  "health.json",
  "release.json",
  "manifest.json",
] as const;

const makeDoc = (name: string, tag: string) => ({
  schema_version: 1,
  file: name,
  tag,
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("loadDashboardBundle", () => {
  it("prefers ./live/ when present", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      const href = String(url);
      if (!href.includes("/live/")) {
        return new Response("missing", { status: 404 });
      }
      const name = FILES.find((file) => href.endsWith(file));
      if (!name) {
        return new Response("missing", { status: 404 });
      }
      return new Response(JSON.stringify(makeDoc(name, "live")), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const bundle = await loadDashboardBundle("./");
    expect(bundle.currentEvent).toMatchObject({ tag: "live" });
    expect(fetchMock.mock.calls.some((call) => String(call[0]).includes("/live/"))).toBe(
      true,
    );
  });

  it("falls back to ./ when live/ is unavailable", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      const href = String(url);
      if (href.includes("/live/")) {
        return new Response("missing", { status: 404 });
      }
      const name = FILES.find((file) => href.endsWith(`/${file}`) || href.endsWith(file));
      if (!name) {
        return new Response("missing", { status: 404 });
      }
      return new Response(JSON.stringify(makeDoc(name, "root")), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const bundle = await loadDashboardBundle("./");
    expect(bundle.matchups).toMatchObject({ tag: "root" });
  });
});
