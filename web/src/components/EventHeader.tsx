import { useEffect, useState } from "react";
import type { CurrentEventDocument } from "../generated/dashboard";
import { readOptionalString } from "../lib/optionalString";

type Props = {
  event: CurrentEventDocument;
};

const formatCountdown = (totalSeconds: number): string => {
  const abs = Math.abs(totalSeconds);
  const days = Math.floor(abs / 86400);
  const hours = Math.floor((abs % 86400) / 3600);
  const minutes = Math.floor((abs % 3600) / 60);
  const seconds = abs % 60;
  const parts = [
    days > 0 ? `${days}d` : null,
    `${hours}h`,
    `${minutes}m`,
    `${seconds}s`,
  ].filter(Boolean);
  return parts.join(" ");
};

export const EventHeader = ({ event }: Props) => {
  const title = readOptionalString(event.title, "Unknown event title");
  const date = readOptionalString(event.event_date, "Unknown event date");
  const lastUpdate = readOptionalString(
    event.last_successful_update_at,
    "Unknown last update",
  );
  const startAt = readOptionalString(event.countdown.event_start_at, "Unknown start time");

  const [secondsLeft, setSecondsLeft] = useState<number | null>(
    event.countdown.seconds_until_start ?? null,
  );

  useEffect(() => {
    if (event.countdown.seconds_until_start == null) {
      setSecondsLeft(null);
      return;
    }
    setSecondsLeft(event.countdown.seconds_until_start);
    const id = window.setInterval(() => {
      setSecondsLeft((prev) => (prev == null ? null : prev - 1));
    }, 1000);
    return () => window.clearInterval(id);
  }, [event.countdown.seconds_until_start]);

  const isPast = event.countdown.is_past === true || (secondsLeft != null && secondsLeft < 0);

  return (
    <header className="rounded-lg border border-ink-300 bg-white p-4 shadow-sm sm:p-6">
      <p className="text-xs font-semibold uppercase tracking-wide text-ink-500">
        This week&apos;s card
      </p>
      <h1 className="mt-1 font-display text-2xl font-bold text-ink-950 sm:text-3xl">
        {title.text}
      </h1>
      <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <dt className="text-ink-500">Event date</dt>
          <dd className="font-medium text-ink-900">{date.text}</dd>
        </div>
        <div>
          <dt className="text-ink-500">Countdown</dt>
          <dd className="font-medium text-ink-900" aria-live="polite">
            {secondsLeft == null ? (
              startAt.known ? (
                startAt.text
              ) : (
                "Unknown countdown"
              )
            ) : isPast ? (
              `Started ${formatCountdown(secondsLeft)} ago`
            ) : (
              formatCountdown(secondsLeft)
            )}
          </dd>
        </div>
        <div>
          <dt className="text-ink-500">Last successful update</dt>
          <dd className="font-medium text-ink-900">{lastUpdate.text}</dd>
        </div>
        <div>
          <dt className="text-ink-500">As of</dt>
          <dd className="font-medium text-ink-900">{event.as_of}</dd>
        </div>
      </dl>
    </header>
  );
};
