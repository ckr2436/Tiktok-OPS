import { describe, expect, it } from 'vitest';

import { isSyncRateLimitedError } from './errors.js';

describe('isSyncRateLimitedError', () => {
  it('recognizes the structured GMV Max cooldown response', () => {
    expect(isSyncRateLimitedError({
      response: {
        status: 429,
        data: { error: { code: 'SYNC_RATE_LIMITED', message: 'Too soon.' } },
      },
    })).toBe(true);
  });

  it('does not hide unrelated failures', () => {
    expect(isSyncRateLimitedError({
      response: { status: 500, data: { error: { code: 'UPSTREAM_FAILED' } } },
      message: 'Upstream failed',
    })).toBe(false);
  });
});
