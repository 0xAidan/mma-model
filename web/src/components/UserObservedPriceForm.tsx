import type { ChangeEvent, FormEvent } from "react";
import { useState } from "react";
import { formatAmerican, formatDecimal, formatEv } from "../lib/odds";
import { localEvFromUserPrice } from "../lib/userObservedStorage";
import type { UserObservedPrice } from "../lib/userObservedStorage";

type Props = {
  boutId: string;
  fairProbability: number | null | undefined;
  existing: UserObservedPrice | undefined;
  onSave: (boutId: string, sportsbook: string, oddsInput: string) => boolean;
  onClear: (boutId: string) => void;
};

export const UserObservedPriceForm = ({
  boutId,
  fairProbability,
  existing,
  onSave,
  onClear,
}: Props) => {
  const [sportsbook, setSportsbook] = useState(existing?.sportsbook ?? "");
  const [oddsInput, setOddsInput] = useState(existing?.oddsInput ?? "");
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const ok = onSave(boutId, sportsbook, oddsInput);
    if (!ok) {
      setError("Enter a sportsbook name and valid decimal or American odds.");
      return;
    }
    setError(null);
  };

  const handleClear = () => {
    onClear(boutId);
    setSportsbook("");
    setOddsInput("");
    setError(null);
  };

  const handleSportsbookChange = (event: ChangeEvent<HTMLInputElement>) => {
    setSportsbook(event.target.value);
  };

  const handleOddsChange = (event: ChangeEvent<HTMLInputElement>) => {
    setOddsInput(event.target.value);
  };

  const localEv = localEvFromUserPrice(fairProbability, existing);

  return (
    <section
      className="mt-3 rounded border border-dashed border-ink-400 bg-ink-50 p-3"
      aria-labelledby={`user-price-${boutId}`}
    >
      <h4 id={`user-price-${boutId}`} className="text-sm font-semibold text-ink-900">
        You entered (any sportsbook)
      </h4>
      <p className="mt-1 text-xs text-ink-600">
        Optional price you saw elsewhere. Stored only in this browser. No login, password, or API
        key.
      </p>
      <form className="mt-3 grid gap-2 sm:grid-cols-3" onSubmit={handleSubmit} noValidate>
        <label className="block text-xs font-medium text-ink-700">
          Sportsbook name
          <input
            type="text"
            name="sportsbook"
            autoComplete="off"
            className="mt-1 w-full rounded border border-ink-300 bg-white px-2 py-1.5 text-sm text-ink-900 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
            value={sportsbook}
            onChange={handleSportsbookChange}
            aria-label="Sportsbook name you observed"
          />
        </label>
        <label className="block text-xs font-medium text-ink-700">
          Odds (decimal or American)
          <input
            type="text"
            name="odds"
            autoComplete="off"
            inputMode="decimal"
            className="mt-1 w-full rounded border border-ink-300 bg-white px-2 py-1.5 text-sm text-ink-900 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
            value={oddsInput}
            onChange={handleOddsChange}
            aria-label="Odds you observed as decimal or American"
            placeholder="2.40 or +140"
          />
        </label>
        <div className="flex items-end gap-2">
          <button
            type="submit"
            className="rounded bg-accent px-3 py-1.5 text-sm font-semibold text-white hover:bg-ink-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          >
            Save
          </button>
          {existing ? (
            <button
              type="button"
              onClick={handleClear}
              className="rounded border border-ink-400 px-3 py-1.5 text-sm font-medium text-ink-800 hover:bg-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
            >
              Clear
            </button>
          ) : null}
        </div>
      </form>
      {error ? (
        <p className="mt-2 text-xs font-medium text-rose-800" role="alert">
          {error}
        </p>
      ) : null}
      {existing ? (
        <dl className="mt-3 grid gap-1 text-sm sm:grid-cols-3">
          <div>
            <dt className="text-xs text-ink-500">You entered book</dt>
            <dd className="font-medium">{existing.sportsbook}</dd>
          </div>
          <div>
            <dt className="text-xs text-ink-500">You entered odds</dt>
            <dd className="font-medium">
              {formatDecimal(existing.decimalOdds)} / {formatAmerican(existing.americanOdds)}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-ink-500">Local display EV</dt>
            <dd className="font-medium">
              {localEv != null ? formatEv(localEv) : "Cannot compute"}
            </dd>
          </div>
        </dl>
      ) : null}
    </section>
  );
};
