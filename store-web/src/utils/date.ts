import { formatDistanceToNow, isValid, parseISO } from "date-fns";

/**
 * Format a UTC ISO timestamp in Indian Standard Time (IST, UTC+05:30)
 * with a friendly relative text suffix (e.g. "9 Nov 2026 (2 days ago)").
 */
export function formatISTWithRelative(
  dateString: string | null | undefined,
  includeTime = false,
): string {
  if (!dateString) return "—";

  const date = parseISO(dateString);
  if (!isValid(date)) return dateString;

  // Format in IST using Intl.DateTimeFormat
  const options: Intl.DateTimeFormatOptions = {
    timeZone: "Asia/Kolkata",
    day: "numeric",
    month: "short",
    year: "numeric",
    ...(includeTime
      ? {
          hour: "numeric",
          minute: "2-digit",
          hour12: true,
        }
      : {}),
  };

  const formattedDate = new Intl.DateTimeFormat("en-IN", options).format(date);
  const relativeText = formatDistanceToNow(date, { addSuffix: true });

  return `${formattedDate} (${relativeText})`;
}

/**
 * Format date in IST without relative text.
 */
export function formatIST(
  dateString: string | null | undefined,
  includeTime = true,
): string {
  if (!dateString) return "—";

  const date = parseISO(dateString);
  if (!isValid(date)) return dateString;

  const options: Intl.DateTimeFormatOptions = {
    timeZone: "Asia/Kolkata",
    day: "numeric",
    month: "short",
    year: "numeric",
    ...(includeTime
      ? {
          hour: "numeric",
          minute: "2-digit",
          hour12: true,
        }
      : {}),
  };

  return new Intl.DateTimeFormat("en-IN", options).format(date);
}

/**
 * Short time-only format in IST for status bars and clock tickers.
 */
export function formatISTShort(dateString: string | null | undefined): string {
  if (!dateString) return "—";
  const date = parseISO(dateString);
  if (!isValid(date)) return dateString;

  return new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
    hour12: true,
  }).format(date);
}
