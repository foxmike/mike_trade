/** Display formatting for the console. All money/quantity inputs are Decimal
 * strings serialized by the API; formatting happens only at the display
 * boundary and never feeds arithmetic back into application state. */

const ET = "America/New_York";

const usdFormat = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const numberFormat = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const quantityFormat = new Intl.NumberFormat("en-US", { maximumFractionDigits: 4 });

export const MISSING = "\u2014";

function toNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** USD money from a Decimal string; "+" sign included for signed values. */
export function money(value: string | number | null | undefined, signed = false): string {
  const parsed = toNumber(value);
  if (parsed === null) return MISSING;
  const text = usdFormat.format(parsed);
  return signed && parsed > 0 ? `+${text}` : text;
}

/** Plain decimal (prices, quantities with decimals). */
export function decimal(value: string | number | null | undefined, digits = 2): string {
  const parsed = toNumber(value);
  if (parsed === null) return MISSING;
  return new Intl.NumberFormat("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(parsed);
}

/** Quantity display (no forced decimals). */
export function quantity(value: string | number | null | undefined): string {
  const parsed = toNumber(value);
  if (parsed === null) return MISSING;
  return quantityFormat.format(parsed);
}

/** Percentage from an already-percent value (e.g. "12.345" -> "12.35%"). */
export function percent(value: string | number | null | undefined, digits = 2): string {
  const parsed = toNumber(value);
  if (parsed === null) return MISSING;
  return `${new Intl.NumberFormat("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(parsed)}%`;
}

const etTime = new Intl.DateTimeFormat("en-US", {
  timeZone: ET,
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

const etDateTime = new Intl.DateTimeFormat("en-US", {
  timeZone: ET,
  year: "numeric",
  month: "short",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

const etDate = new Intl.DateTimeFormat("en-US", {
  timeZone: ET,
  year: "numeric",
  month: "short",
  day: "2-digit",
});

const utcTime = new Intl.DateTimeFormat("en-US", {
  timeZone: "UTC",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

function parseDate(value: string | null | undefined): Date | null {
  if (value === null || value === undefined || value === "") return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** HH:mm:ss in exchange-local (America/New_York) time. */
export function timeEt(value: string | null | undefined): string {
  const date = parseDate(value);
  return date === null ? MISSING : etTime.format(date);
}

/** "Oct 11, 2026, 14:35:12" in exchange-local time. */
export function dateTimeEt(value: string | null | undefined): string {
  const date = parseDate(value);
  return date === null ? MISSING : etDateTime.format(date);
}

/** Calendar date in exchange-local time. */
export function dateEt(value: string | null | undefined): string {
  const date = parseDate(value);
  return date === null ? MISSING : etDate.format(date);
}

/** HH:mm:ss UTC. */
export function timeUtc(value: string | null | undefined): string {
  const date = parseDate(value);
  return date === null ? MISSING : utcTime.format(date);
}

/** Human duration from seconds ("2h 05m", "3m 12s"). */
export function durationSeconds(value: string | number | null | undefined): string {
  const total = toNumber(value);
  if (total === null) return MISSING;
  const whole = Math.max(0, Math.round(total));
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const seconds = whole % 60;
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (minutes > 0) return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  return `${seconds}s`;
}

/** Byte count in human units. */
export function bytes(value: number | null | undefined): string {
  const parsed = toNumber(value);
  if (parsed === null) return MISSING;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let scaled = parsed;
  let unit = units[0];
  for (const candidate of units) {
    unit = candidate;
    if (Math.abs(scaled) < 1024) break;
    scaled /= 1024;
  }
  return `${new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 }).format(scaled)} ${unit}`;
}

/** Copyable identifier truncated for display ("a1b2c3d4e5f6…"). */
export function shortId(value: string | null | undefined, keep = 12): string {
  if (value === null || value === undefined || value === "") return MISSING;
  return value.length <= keep ? value : `${value.slice(0, keep)}\u2026`;
}
