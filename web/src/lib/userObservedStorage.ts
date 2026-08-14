import {
  americanToDecimal,
  computeExactEv,
  decimalToAmerican,
  parseOddsInput,
} from "./odds";

export type UserObservedPrice = {
  sportsbook: string;
  oddsInput: string;
  decimalOdds: number;
  americanOdds: number;
  updatedAt: string;
};

const STORAGE_KEY = "dwcs-user-observed-prices";

export type UserObservedMap = Record<string, UserObservedPrice>;

export const loadUserObservedPrices = (): UserObservedMap => {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return {};
    }
    const parsed = JSON.parse(raw) as UserObservedMap;
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
};

export const saveUserObservedPrices = (map: UserObservedMap): void => {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(map));
};

export const buildUserObservedPrice = (
  sportsbook: string,
  oddsInput: string,
): UserObservedPrice | null => {
  const book = sportsbook.trim();
  const parsed = parseOddsInput(oddsInput);
  if (!book || !parsed) {
    return null;
  }

  let decimalOdds: number | null;
  let americanOdds: number | null;
  if (parsed.kind === "decimal") {
    decimalOdds = parsed.value;
    americanOdds = decimalToAmerican(parsed.value);
  } else {
    americanOdds = parsed.value;
    decimalOdds = americanToDecimal(parsed.value);
  }

  if (decimalOdds == null || americanOdds == null || decimalOdds <= 1) {
    return null;
  }

  return {
    sportsbook: book,
    oddsInput: oddsInput.trim(),
    decimalOdds,
    americanOdds,
    updatedAt: new Date().toISOString(),
  };
};

export const localEvFromUserPrice = (
  fairProbability: number | null | undefined,
  userPrice: UserObservedPrice | null | undefined,
): number | null => {
  if (!userPrice) {
    return null;
  }
  return computeExactEv(fairProbability, userPrice.decimalOdds);
};
