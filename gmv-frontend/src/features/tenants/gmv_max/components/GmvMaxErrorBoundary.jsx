import React from 'react';

export function GmvMaxErrorBoundary({ error }) {
  return (
    <div style={{ padding: 24 }}>
      <h2>GMV Max encountered an error</h2>
      <p>Please refresh the page or contact support if the problem persists.</p>
      {process.env.NODE_ENV === 'development' && error ? (
        <pre style={{ marginTop: 16, whiteSpace: 'pre-wrap' }}>{String(error)}</pre>
      ) : null}
    </div>
  );
}

export default GmvMaxErrorBoundary;
