import { afterEach, describe, expect, it, vi } from 'vitest';
import http from '@/lib/http.js';
import * as svc from '../ttb/gmvmax/api.js';

describe('GMV-Max URL contract', () => {
  const wid = 42;
  const auth = 1001;
  const cid = 777;

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('builds list/detail urls without /campaigns segment', async () => {
    const listUrl = svc.base(wid, auth);
    expect(listUrl).toContain(`/tenants/${wid}/ttb/accounts/${auth}/gmvmax`);
    expect(listUrl).not.toContain('/campaigns');

    const getSpy = vi.spyOn(http, 'get').mockResolvedValue({ data: null });
    await svc.getCampaign(wid, auth, cid);
    expect(getSpy).toHaveBeenCalledWith(
      expect.stringContaining(`/gmvmax/${cid}`),
      undefined,
    );
  });

  it('uses correct verbs for side-effect routes', async () => {
    const postSpy = vi.spyOn(http, 'post').mockResolvedValue({ data: null });
    const putSpy = vi.spyOn(http, 'put').mockResolvedValue({ data: null });

    await svc.syncMetrics(wid, auth, cid, {});
    await svc.applyAction(wid, auth, cid, { action: 'PAUSE' });
    await svc.previewStrategy(wid, auth, cid, {});
    await svc.updateStrategy(wid, auth, cid, { enabled: true });

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
