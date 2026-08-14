/** Map a signed ratio (e.g. ROI 0.12) into a 0–100 bar by magnitude clamp. */
export const ratioToBarWidth = (value: number | null | undefined, maxAbs = 1): number | null => {
  if (value == null || !Number.isFinite(value)) {
    return null;
  }
  const clamped = Math.min(Math.abs(value), maxAbs) / maxAbs;
  return Math.round(clamped * 100);
};

/** Map calibration slope/intercept into 0–100 closeness bars. */
export const calibrationClosenessWidth = (
  slope: number | null | undefined,
  intercept: number | null | undefined,
): { slopeWidth: number | null; interceptWidth: number | null } => {
  const slopeWidth =
    slope == null || !Number.isFinite(slope)
      ? null
      : Math.round(Math.max(0, 100 - Math.min(100, Math.abs(slope - 1) * 100)));
  const interceptWidth =
    intercept == null || !Number.isFinite(intercept)
      ? null
      : Math.round(Math.max(0, 100 - Math.min(100, Math.abs(intercept) * 100)));
  return { slopeWidth, interceptWidth };
};
