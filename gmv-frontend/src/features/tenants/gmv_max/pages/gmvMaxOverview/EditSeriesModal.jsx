import { useCallback, useEffect, useMemo, useState } from 'react';

import Modal from '@/components/ui/Modal.jsx';
import FormField from '@/components/ui/FormField.jsx';
import Loading from '@/components/ui/Loading.jsx';

import ProductSelectionPanel from './ProductSelectionPanel.jsx';
import { ErrorBlock } from './ErrorHandling.jsx';
import {
  formatError,
  getAvailableProductIds,
  getProductIdentifier,
  parseOptionalFloat,
  setsEqual,
} from './helpers.js';
import {
  useUpdateGmvMaxCampaignMutation,
  useUpdateGmvMaxStrategyMutation,
} from '../../hooks/gmvMaxQueries.js';

export default function EditSeriesModal({
  open,
  onClose,
  workspaceId,
  provider,
  authId,
  campaign,
  detail,
  detailLoading,
  detailError,
  onRetryDetail,
  products,
  productsLoading,
  storeId,
  storeNameById,
  onUpdated,
}) {
  const [name, setName] = useState('');
  const [budget, setBudget] = useState('');
  const [roasBid, setRoasBid] = useState('');
  const [localSelectedIds, setLocalSelectedIds] = useState(new Set());
  const [submitError, setSubmitError] = useState(null);

  const campaignId = campaign?.campaign_id || campaign?.id || '';

  const detailProducts = useMemo(() => {
    const sessions = detail?.sessions || detail?.session_list || [];
    const collected = [];
    sessions.forEach((session) => {
      (session?.product_list || session?.products || []).forEach((product) => {
        if (product) {
          collected.push(product);
        }
      });
    });
    return collected;
  }, [detail]);

  const initialProductSet = useMemo(() => {
    const ids = new Set();
    detailProducts.forEach((product) => {
      const id = getProductIdentifier(product);
      if (id) ids.add(id);
    });
    return ids;
  }, [detailProducts]);

  const mergedProducts = useMemo(() => {
    const map = new Map();
    (products || []).forEach((product) => {
      const id = getProductIdentifier(product);
      if (id) {
        map.set(id, product);
      }
    });
    detailProducts.forEach((product) => {
      const id = getProductIdentifier(product);
      if (id && !map.has(id)) {
        map.set(id, product);
      }
    });
    return Array.from(map.values());
  }, [detailProducts, products]);

  const availableProductIds = useMemo(() => getAvailableProductIds(mergedProducts), [mergedProducts]);

  useEffect(() => {
    if (!open) return;
    if (!detail) return;
    setName(detail.campaign?.campaign_name || '');
    setBudget(
      detail.campaign?.budget !== undefined && detail.campaign?.budget !== null
        ? String(detail.campaign.budget)
        : '',
    );
    setRoasBid(
      detail.campaign?.roas_bid !== undefined && detail.campaign?.roas_bid !== null
        ? String(detail.campaign.roas_bid)
        : '',
    );
    setLocalSelectedIds(new Set(initialProductSet));
    setSubmitError(null);
  }, [detail, initialProductSet, open]);

  useEffect(() => {
    if (!open) return;
    setLocalSelectedIds((prev) => {
      const next = new Set();
      prev.forEach((id) => {
        if (initialProductSet.has(id)) {
          next.add(id);
        }
      });
      return next;
    });
  }, [initialProductSet, open]);

  useEffect(() => {
    if (!open) return;
    setLocalSelectedIds((prev) => {
      const next = new Set();
      prev.forEach((id) => {
        if (availableProductIds.has(id)) {
          next.add(id);
        }
      });
      return next;
    });
  }, [availableProductIds, open]);

  const toggleProduct = useCallback((id) => {
    setLocalSelectedIds((prev) => {
      const next = new Set(prev);
      const key = String(id);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  }, []);

  const toggleAll = useCallback((ids) => {
    setLocalSelectedIds((prev) => {
      const next = new Set(prev);
      const normalized = (ids || []).map(String);
      const shouldDeselect = normalized.every((id) => next.has(id));
      if (shouldDeselect) {
        normalized.forEach((id) => next.delete(id));
      } else {
        normalized.forEach((id) => next.add(id));
      }
      return next;
    });
  }, []);

  const updateCampaignMutation = useUpdateGmvMaxCampaignMutation(workspaceId, provider, authId, campaignId);
  const strategyMutation = useUpdateGmvMaxStrategyMutation(workspaceId, provider, authId, campaignId);

  const productsChanged = useMemo(
    () => !setsEqual(localSelectedIds, initialProductSet),
    [localSelectedIds, initialProductSet],
  );

  const sessionId = detail?.sessions?.[0]?.session_id || detail?.session?.session_id || null;
  const effectiveStoreId = storeId || detail?.campaign?.store_id || null;

  const handleSubmit = useCallback(async () => {
    if (!campaignId) return;
    const trimmedName = name.trim();
    const campaignPatch = {};
    if (trimmedName && trimmedName !== detail?.campaign?.campaign_name) {
      campaignPatch.campaign_name = trimmedName;
    }
    const budgetValue = parseOptionalFloat(budget);
    if (budgetValue !== undefined && budgetValue !== detail?.campaign?.budget) {
      campaignPatch.budget = budgetValue;
    }
    const roasValue = parseOptionalFloat(roasBid);
    if (roasValue !== undefined && roasValue !== detail?.campaign?.roas_bid) {
      campaignPatch.roas_bid = roasValue;
    }

    const tasks = [];
    setSubmitError(null);

    try {
      if (Object.keys(campaignPatch).length > 0) {
        tasks.push(updateCampaignMutation.mutateAsync(campaignPatch));
      }
      if (productsChanged) {
        if (!sessionId) {
          throw new Error('Unable to update products: missing session information.');
        }
        const productList = Array.from(localSelectedIds).map((id) => ({ spu_id: String(id) }));
        const sessionPayload = {
          session_id: sessionId,
          store_id: effectiveStoreId ? String(effectiveStoreId) : undefined,
          product_list: productList,
        };
        tasks.push(strategyMutation.mutateAsync({ session: sessionPayload }));
      }
      if (tasks.length === 0) {
        onClose?.();
        return;
      }
      await Promise.all(tasks);
      onUpdated?.();
      onClose?.();
    } catch (error) {
      setSubmitError(formatError(error));
    }
  }, [
    campaignId,
    detail,
    effectiveStoreId,
    localSelectedIds,
    name,
    budget,
    roasBid,
    onUpdated,
    onClose,
    productsChanged,
    sessionId,
    strategyMutation,
    updateCampaignMutation,
  ]);

  const isSaving = updateCampaignMutation.isPending || strategyMutation.isPending;
  const canSubmit =
    Boolean(detail) &&
    (productsChanged ||
      (name.trim() && name.trim() !== detail?.campaign?.campaign_name) ||
      (budget && parseOptionalFloat(budget) !== detail?.campaign?.budget) ||
      (roasBid && parseOptionalFloat(roasBid) !== detail?.campaign?.roas_bid));

  if (!open) return null;

  return (
    <Modal open={open} title="Edit GMV Max Series" onClose={onClose}>
      {detailLoading ? <Loading text="Loading campaign…" /> : null}
      <ErrorBlock error={detailError} onRetry={onRetryDetail} />
      {!detailLoading && !detailError && !detail ? <p>Campaign details are not available.</p> : null}
      {!detail || detailLoading || detailError ? null : (
        <div className="gmvmax-modal-step">
          <h3>Basic configuration</h3>
          <FormField label="Series name">
            <input type="text" value={name} onChange={(event) => setName(event.target.value)} />
          </FormField>
          <div className="gmvmax-modal-grid">
            <FormField label="Budget">
              <input
                type="number"
                min="0"
                step="0.01"
                value={budget}
                onChange={(event) => setBudget(event.target.value)}
                placeholder="Leave blank to keep current"
              />
            </FormField>
            <FormField label="ROAS bid">
              <input
                type="number"
                min="0"
                step="0.01"
                value={roasBid}
                onChange={(event) => setRoasBid(event.target.value)}
                placeholder="Leave blank to keep current"
              />
            </FormField>
          </div>
          <dl className="gmvmax-review-list">
            <div>
              <dt>Optimization goal</dt>
              <dd>{detail.campaign?.optimization_goal || '—'}</dd>
            </div>
            <div>
              <dt>Shopping ads type</dt>
              <dd>{detail.campaign?.shopping_ads_type || '—'}</dd>
            </div>
          </dl>
          <h3>Products</h3>
          {!sessionId ? (
            <p>Session information is unavailable; product editing is disabled.</p>
          ) : (
            <ProductSelectionPanel
              products={mergedProducts}
              selectedIds={localSelectedIds}
              onToggle={toggleProduct}
              onToggleAll={toggleAll}
              storeNames={storeNameById}
              loading={productsLoading}
              emptyMessage={productsLoading ? 'Loading products…' : 'No products found.'}
              disabled={isSaving}
            />
          )}
          {submitError ? <div className="gmvmax-error">{submitError}</div> : null}
          {isSaving ? <Loading text="Saving changes…" /> : null}
          <div className="gmvmax-modal-footer">
            <button type="button" onClick={onClose} disabled={isSaving}>
              Cancel
            </button>
            <button type="button" onClick={handleSubmit} disabled={isSaving || !canSubmit}>
              Save changes
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}
