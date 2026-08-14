import type { OptionalStringField } from "../generated/dashboard";

export const readOptionalString = (
  field: OptionalStringField | undefined,
  unknownLabel: string,
): { known: boolean; text: string } => {
  if (!field || field.presence !== "known" || !field.value) {
    return { known: false, text: unknownLabel };
  }
  return { known: true, text: field.value };
};
