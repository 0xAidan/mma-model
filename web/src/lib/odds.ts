/** Convert American odds to decimal odds. */
export const americanToDecimal = (american: number): number | null => {
  if (!Number.isFinite(american) || american === 0) {
    return null;
  }
  if (american > 0) {
    return 1 + american / 100;
  }
  return 1 + 100 / Math.abs(american);
};

/** Convert decimal odds to American odds. */
export const decimalToAmerican = (decimal: number): number | null => {
  if (!Number.isFinite(decimal) || decimal <= 1) {
    return null;
  }
  if (decimal >= 2) {
    return Math.round((decimal - 1) * 100);
  }
  return Math.round(-100 / (decimal - 1));
};

/** Exact EV from fair probability and decimal odds: p * odds - 1. */
export const computeExactEv = (
  fairProbability: number | null | undefined,
  decimalOdds: number | null | undefined,
): number | null => {
  if (
    fairProbability == null ||
    decimalOdds == null ||
    !Number.isFinite(fairProbability) ||
    !Number.isFinite(decimalOdds) ||
    decimalOdds <= 1
  ) {
    return null;
  }
  return fairProbability * decimalOdds - 1;
};

export const formatPercent = (value: number | null | undefined, digits = 1): string => {
  if (value == null || !Number.isFinite(value)) {
    return "—";
  }
  return `${(value * 100).toFixed(digits)}%`;
};

export const formatEv = (value: number): string => {
  const pct = value * 100;
  const sign = pct > 0 ? "+" : "";
  return `${sign}${pct.toFixed(1)}%`;
};

export const formatAmerican = (value: number | null | undefined): string => {
  if (value == null || !Number.isFinite(value)) {
    return "—";
  }
  return value > 0 ? `+${Math.round(value)}` : `${Math.round(value)}`;
};

export const formatDecimal = (value: number | null | undefined): string => {
  if (value == null || !Number.isFinite(value)) {
    return "—";
  }
  return value.toFixed(2);
};

export const parseOddsInput = (
  raw: string,
): { kind: "decimal" | "american"; value: number } | null => {
  const trimmed = raw.trim();
  if (!trimmed) {
    return null;
  }
  const n = Number(trimmed);
  if (!Number.isFinite(n)) {
    return null;
  }
  // Decimal odds are typically > 1 and < ~50 with a fractional part or small positive.
  // Prefer explicit + / - as American; otherwise treat values >= 1.01 and < 100 without sign as decimal if they have a decimal point or are between 1.01 and 20.
  if (trimmed.startsWith("+") || trimmed.startsWith("-")) {
    return { kind: "american", value: n };
  }
  if (trimmed.includes(".") || (n > 1 && n < 50)) {
    return { kind: "decimal", value: n };
  }
  return { kind: "american", value: n };
};
