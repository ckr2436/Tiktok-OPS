import React, { useEffect, useMemo } from 'react';
import { useRouteError } from 'react-router-dom';

export function GmvMaxErrorBoundary({ error: explicitError }) {
  const routeError = useRouteError();
  const error = explicitError || routeError;

  const details = useMemo(() => {
    if (!error) return null;
    const message = error?.message || String(error);
    const stack = error?.stack && String(error.stack).includes(message)
      ? error.stack
      : `${message}\n${error?.stack || ''}`;
    return { message, stack };
  }, [error]);

  useEffect(() => {
    if (error) {
      // Surface the original stack trace to the console so we can diagnose issues hidden by the boundary UI.
      console.error('GMV Max route failed', error);
    }
  }, [error]);

  return (
    <div style={{ padding: 24 }}>
      <h2>GMV Max encountered an error</h2>
      <p>Please refresh the page or contact support if the problem persists.</p>
      {details ? (
        <details style={{ marginTop: 16 }} open={process.env.NODE_ENV === 'development'}>
          <summary>Technical details</summary>
          <pre style={{ marginTop: 8, whiteSpace: 'pre-wrap' }}>{details.stack}</pre>
        </details>
      ) : null}
    </div>
  );
}

export default GmvMaxErrorBoundary;
