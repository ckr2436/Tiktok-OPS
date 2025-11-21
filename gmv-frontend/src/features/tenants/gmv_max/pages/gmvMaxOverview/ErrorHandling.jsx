import { formatError } from './helpers.js';

export function ErrorBlock({ error, onRetry, message: overrideMessage }) {
  if (!error) return null;
  console.error('GMV Max request failed', error);
  const message = overrideMessage ?? formatError(error) ?? 'Something went wrong. Please try again.';
  const safeMessage =
    typeof message === 'string' && message.trim().startsWith('[')
      ? 'Something went wrong. Please try again.'
      : message;
  return (
    <div className="gmvmax-inline-error" role="alert">
      <span>{safeMessage}</span>
      {onRetry ? (
        <button type="button" onClick={onRetry} className="gmvmax-button gmvmax-button--link">
          Retry
        </button>
      ) : null}
    </div>
  );
}

export function SeriesErrorNotice({ error, onRetry }) {
  if (!error) return null;
  console.error('Failed to load GMV Max series', error);
  return (
    <div className="gmvmax-error-card" role="alert">
      <div>
        <h3>Failed to load GMV Max series</h3>
        <p>Please check your filters and try again.</p>
      </div>
      {onRetry ? (
        <button type="button" onClick={onRetry} className="gmvmax-button gmvmax-button--primary">
          Retry
        </button>
      ) : null}
    </div>
  );
}
