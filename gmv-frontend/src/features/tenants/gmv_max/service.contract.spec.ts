import { afterEach, describe, expect, it, vi } from 'vitest';
import http from '@/lib/http.js';
import * as svc from '../ttb/gmvmax/api.js';

describe('GMV-Max URL contract', () => {
  const wid = 42;
  const auth = 1001;
  const cid = 777;
  const provider = 'tiktok-business';

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('builds list/detail urls without /campaigns segment', async () => {
    const listUrl = svc.base(wid, provider, auth);
    expect(listUrl).toContain(`/tenants/${encodeURIComponent(wid)}/providers/${provider}/accounts/${encodeURIComponent(auth)}/gmvmax`);
    expect(listUrl).not.toContain('/campaigns');

    const getSpy = vi.spyOn(http, 'get').mockResolvedValue({ data: null });
    await svc.getCampaign(wid, provider, auth, cid);
    expect(getSpy).toHaveBeenCalledWith(
      expect.stringContaining(`/providers/${provider}/accounts/${encodeURIComponent(auth)}/gmvmax/${cid}`),
      undefined,
    );
  });

  it('uses correct verbs for side-effect routes', async () => {
    const postSpy = vi.spyOn(http, 'post').mockResolvedValue({ data: null });
    const putSpy = vi.spyOn(http, 'put').mockResolvedValue({ data: null });

    await svc.syncMetrics(wid, provider, auth, cid, {});
    await svc.applyAction(wid, provider, auth, cid, { action: 'PAUSE' });
    await svc.previewStrategy(wid, provider, auth, cid, {});
    await svc.updateStrategy(wid, provider, auth, cid, { enabled: true });

    const postUrls = postSpy.mock.calls.map((call) => call[0]);
    expect(postUrls).toEqual(
      expect.arrayContaining([
        expect.stringContaining('/metrics/sync'),
        expect.stringContaining('/actions'),
        expect.stringContaining('/strategies/preview'),
      ]),
    );
    expect(putSpy).toHaveBeenCalledWith(
      expect.stringContaining('/strategy'),
      expect.objectContaining({ enabled: true }),
    );
  });
});
