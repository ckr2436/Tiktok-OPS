const BROWSER_TIMEZONE = (() => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch (error) {
    return 'UTC';
  }
})();

function getFormatter(timeZone) {
  return new Intl.DateTimeFormat('en-US', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

function getDateParts(date, timeZone) {
  const formatter = getFormatter(timeZone);
  const parts = formatter.formatToParts(date);
  const result = {};
  parts.forEach((part) => {
    if (part.type !== 'literal') {
      result[part.type] = part.value;
    }
  });
  return result;
}

function getTimeZoneOffsetMinutes(date, timeZone) {
  const { year, month, day, hour, minute, second } = getDateParts(date, timeZone);
  const utcTime = Date.UTC(
    Number(year),
    Number(month) - 1,
    Number(day),
    Number(hour),
    Number(minute),
    Number(second),
  );
  return (utcTime - date.getTime()) / (60 * 1000);
}

function getStartOfDayInTimeZone(date, timeZone) {
  const normalizedTz = timeZone || BROWSER_TIMEZONE;
  const { year, month, day } = getDateParts(date, normalizedTz);
  const utcTime = Date.UTC(Number(year), Number(month) - 1, Number(day), 0, 0, 0, 0);
  const offsetMinutes = getTimeZoneOffsetMinutes(new Date(utcTime), normalizedTz);
  return new Date(utcTime - offsetMinutes * 60 * 1000);
}

function formatDateInTimeZone(date, timeZone) {
  const normalizedTz = timeZone || BROWSER_TIMEZONE;
  const { year, month, day } = getDateParts(date, normalizedTz);
  if (!year || !month || !day) return '';
  return `${year}-${month}-${day}`;
}

export function getAdvertiserTodayRange(timeZone) {
  const normalizedTz = timeZone || BROWSER_TIMEZONE;
  const now = new Date();
  const start = getStartOfDayInTimeZone(now, normalizedTz);
  return { start, end: now, timeZone: normalizedTz };
}

export function getAdvertiserRecentRange(days, timeZone) {
  const normalizedDays = Number.isFinite(Number(days)) ? Number(days) : 1;
  const { start, end, timeZone: tz } = getAdvertiserTodayRange(timeZone);
  const startCopy = new Date(start);
  startCopy.setUTCDate(startCopy.getUTCDate() - (normalizedDays - 1));
  return { start: startCopy, end, timeZone: tz };
}

export function formatRangeAsIsoStrings(range) {
  const start = range?.start instanceof Date && !Number.isNaN(range.start.getTime())
    ? range.start.toISOString()
    : undefined;
  const end = range?.end instanceof Date && !Number.isNaN(range.end.getTime())
    ? range.end.toISOString()
    : undefined;
  return { start_date: start, end_date: end };
}

export function formatRangeAsDateStrings(range) {
  const timeZone = range?.timeZone || BROWSER_TIMEZONE;
  const start = range?.start instanceof Date && !Number.isNaN(range.start.getTime())
    ? formatDateInTimeZone(range.start, timeZone)
    : undefined;
  const end = range?.end instanceof Date && !Number.isNaN(range.end.getTime())
    ? formatDateInTimeZone(range.end, timeZone)
    : undefined;
  return { start_date: start, end_date: end };
}

export function resolveTimezoneLabel(timezone) {
  return timezone || BROWSER_TIMEZONE;
}

