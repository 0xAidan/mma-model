import { useEffect, useState } from "react";
import {
  buildUserObservedPrice,
  loadUserObservedPrices,
  saveUserObservedPrices,
  type UserObservedMap,
  type UserObservedPrice,
} from "../lib/userObservedStorage";

export const useUserObservedPrices = () => {
  const [map, setMap] = useState<UserObservedMap>({});

  useEffect(() => {
    setMap(loadUserObservedPrices());
  }, []);

  const setForBout = (boutId: string, sportsbook: string, oddsInput: string): boolean => {
    const built = buildUserObservedPrice(sportsbook, oddsInput);
    if (!built) {
      return false;
    }
    setMap((prev) => {
      const next = { ...prev, [boutId]: built };
      saveUserObservedPrices(next);
      return next;
    });
    return true;
  };

  const clearForBout = (boutId: string): void => {
    setMap((prev) => {
      const next = { ...prev };
      delete next[boutId];
      saveUserObservedPrices(next);
      return next;
    });
  };

  const getForBout = (boutId: string): UserObservedPrice | undefined => map[boutId];

  return { map, setForBout, clearForBout, getForBout };
};
