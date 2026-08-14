import { describe, expect, it } from "vitest";
import {
  americanToDecimal,
  computeExactEv,
  decimalToAmerican,
  parseOddsInput,
} from "../lib/odds";
import { rollupHealthStatus } from "../lib/status";

describe("odds math", () => {
  it("converts american and decimal", () => {
    expect(americanToDecimal(100)).toBeCloseTo(2);
    expect(americanToDecimal(-110)).toBeCloseTo(1.909, 2);
    expect(decimalToAmerican(2)).toBe(100);
    expect(decimalToAmerican(1.5)).toBe(-200);
  });

  it("computes exact EV", () => {
    expect(computeExactEv(0.55, 2.4)).toBeCloseTo(0.32);
    expect(computeExactEv(null, 2.4)).toBeNull();
    expect(computeExactEv(0.55, null)).toBeNull();
  });

  it("parses odds input", () => {
    expect(parseOddsInput("+140")).toEqual({ kind: "american", value: 140 });
    expect(parseOddsInput("2.40")).toEqual({ kind: "decimal", value: 2.4 });
  });
});

describe("health rollup", () => {
  it("picks worst status", () => {
    expect(rollupHealthStatus(["healthy", "missing", "stale"])).toBe("stale");
    expect(rollupHealthStatus(["healthy", "blocked"])).toBe("blocked");
    expect(rollupHealthStatus(["failed", "blocked"])).toBe("failed");
  });
});
