import { useCallback, useEffect, useMemo, useState } from 'react';

import Modal from '@/components/ui/Modal.jsx';
import FormField from '@/components/ui/FormField.jsx';
import Loading from '@/components/ui/Loading.jsx';

import ProductSelectionPanel from './ProductSelectionPanel.jsx';
import {
  extractChoiceList,
  formatError,
  getAvailableProductIds,
  getProductIdentifier,
  parseOptionalFloat,
} from './helpers.js';
import { ErrorBlock } from './ErrorHandling.jsx';
import { useCreateGmvMaxCampaignMutation, useGmvMaxOptionsQuery } from '../../hooks/gmvMaxQueries.js';

export default function CreateSeriesModal({
  open,
  onClose,
  workspaceId,
  provider,
  authId,
  advertiserId,
  storeId,
  products,
  productsLoading,
  storeNameById,
  initialProductIds,
  onCreated,
}) {
  const [step, setStep] = useState(1);
  const [form, setForm] = useState({
    name: '',
    shoppingAdsType: '',
    optimizationGoal: '',
    bidType: '',
    budget: '',
    roasBid: '',
  });
  const [localSelectedIds, setLocalSelectedIds] = useState(new Set());
  const [submitError, setSubmitError] = useState(null);

  const productsById = useMemo(() => {
    const map = new Map();
    (products || []).forEach((product) => {
      const id = getProductIdentifier(product);
      if (id) {
        map.set(id, product);
      }
    });
    return map;
  }, [products]);

  useEffect(() => {
    if (!open) return;
    setStep(1);
    setSubmitError(null);
    setForm({
      name: '',
      shoppingAdsType: '',
      optimizationGoal: '',
      bidType: '',
      budget: '',
      roasBid: '',
    });
    const ids = (initialProductIds || []).map(String);
    setLocalSelectedIds(new Set(ids));
  }, [open, initialProductIds]);

  useEffect(() => {
    if (!open) return;
    const allowed = getAvailableProductIds(products);
    setLocalSelectedIds((prev) => {
      const next = new Set();
      prev.forEach((id) => {
        if (allowed.has(id)) {
          next.add(id);
        }
      });
      return next;
    });
  }, [open, products]);

  const optionsQuery = useGmvMaxOptionsQuery(
    workspaceId,
    provider,
    authId,
    {},
    {
      enabled: Boolean(open && workspaceId && authId),
    },
  );

  const shoppingAdsChoices = useMemo(() => {
    const payload = optionsQuery.data;
    if (!payload) return [];
    const campaignOptions = payload.campaign_options ?? payload.campaign ?? {};
    return extractChoiceList(
      campaignOptions.shopping_ads_types ??
        campaignOptions.shoppingAdsTypes ??
        campaignOptions.shopping_ads_type_options ??
        campaignOptions.shoppingAdsTypeOptions,
    );
  }, [optionsQuery.data]);

  const optimizationGoalChoices = useMemo(() => {
    const payload = optionsQuery.data;
    if (!payload) return [];
    const campaignOptions = payload.campaign_options ?? payload.campaign ?? {};
    return extractChoiceList(
      campaignOptions.optimization_goals ??
        campaignOptions.optimizationGoals ??
        campaignOptions.optimization_goal_options ??
        campaignOptions.optimizationGoalOptions,
    );
  }, [optionsQuery.data]);

  const bidTypeChoices = useMemo(() => {
    const payload = optionsQuery.data;
    if (!payload) return [];
    const campaignOptions = payload.campaign_options ?? payload.campaign ?? {};
    return extractChoiceList(
      campaignOptions.bid_types ??
        campaignOptions.bidTypes ??
        campaignOptions.bid_type_options ??
        campaignOptions.bidTypeOptions,
    );
  }, [optionsQuery.data]);

  const createMutation = useCreateGmvMaxCampaignMutation(workspaceId, provider, authId);

  const selectedProducts = useMemo(() => {
    return Array.from(localSelectedIds)
      .map((id) => productsById.get(id))
      .filter(Boolean);
  }, [localSelectedIds, productsById]);

  const canProceedStep1 = Boolean(form.name.trim() && form.optimizationGoal && form.shoppingAdsType);
  const canProceedStep2 = selectedProducts.length > 0;

  const goNext = useCallback(() => {
    setStep((prev) => Math.min(prev + 1, 3));
  }, []);

  const goBack = useCallback(() => {
    setStep((prev) => Math.max(prev - 1, 1));
  }, []);

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

  const handleSubmit = useCallback(async () => {
    if (!canProceedStep2) return;
    const trimmedName = form.name.trim();
    const payload = {
      campaign: {
        campaign_name: trimmedName,
        shopping_ads_type: form.shoppingAdsType || undefined,
        optimization_goal: form.optimizationGoal || undefined,
        bid_type: form.bidType || undefined,
        advertiser_id: advertiserId ? String(advertiserId) : undefined,
        store_id: storeId ? String(storeId) : undefined,
      },
      session: {
        store_id: storeId ? String(storeId) : undefined,
        product_list: Array.from(localSelectedIds).map((id) => ({ spu_id: String(id) })),
      },
    };

    const budgetValue = parseOptionalFloat(form.budget);
    if (budgetValue !== undefined) {
      payload.campaign.budget = budgetValue;
    }
    const roasValue = parseOptionalFloat(form.roasBid);
    if (roasValue !== undefined) {
      payload.campaign.roas_bid = roasValue;
    }

    setSubmitError(null);
    try {
      await createMutation.mutateAsync(payload);
      onCreated?.();
    } catch (error) {
      setSubmitError(formatError(error));
    }
  }, [
    advertiserId,
    canProceedStep2,
    createMutation,
    form.bidType,
    form.budget,
    form.name,
    form.optimizationGoal,
    form.roasBid,
    form.shoppingAdsType,
    localSelectedIds,
    onCreated,
    storeId,
  ]);

  if (!open) return null;

  return (
    <Modal open={open} title="Create GMV Max Series" onClose={onClose}>
      {optionsQuery.isLoading ? <Loading text="Loading options…" /> : null}
      <ErrorBlock error={optionsQuery.error} onRetry={optionsQuery.refetch} />

      {step === 1 ? (
        <div className="gmvmax-modal-step">
          <h3>Series details</h3>
          <FormField label="Series name">
            <input
              type="text"
              value={form.name}
              onChange={(event) => setForm((prev) => ({ ...prev, name: event.target.value }))}
              placeholder="Enter series name"
            />
          </FormField>
          <FormField label="Shopping ads type">
            {shoppingAdsChoices.length > 0 ? (
              <select
                value={form.shoppingAdsType}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, shoppingAdsType: event.target.value }))
                }
              >
                <option value="">Select type</option>
                {shoppingAdsChoices.map((choice) => (
                  <option key={choice.value} value={choice.value}>
                    {choice.label}
                  </option>
                ))}
              </select>
            ) : (
              <input
                type="text"
                value={form.shoppingAdsType}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, shoppingAdsType: event.target.value }))
                }
                placeholder="e.g. PRODUCT"
              />
            )}
          </FormField>
          <FormField label="Optimization goal">
            {optimizationGoalChoices.length > 0 ? (
              <select
                value={form.optimizationGoal}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, optimizationGoal: event.target.value }))
                }
              >
                <option value="">Select goal</option>
                {optimizationGoalChoices.map((choice) => (
                  <option key={choice.value} value={choice.value}>
                    {choice.label}
                  </option>
                ))}
              </select>
            ) : (
              <input
                type="text"
                value={form.optimizationGoal}
                onChange={(event) =>
                  setForm((prev) => ({ ...prev, optimizationGoal: event.target.value }))
                }
                placeholder="e.g. GMV"
              />
            )}
          </FormField>
          <FormField label="Bid type">
            {bidTypeChoices.length > 0 ? (
              <select
                value={form.bidType}
                onChange={(event) => setForm((prev) => ({ ...prev, bidType: event.target.value }))}
              >
                <option value="">Select bid type</option>
                {bidTypeChoices.map((choice) => (
                  <option key={choice.value} value={choice.value}>
                    {choice.label}
                  </option>
                ))}
              </select>
            ) : (
              <input
                type="text"
                value={form.bidType}
                onChange={(event) => setForm((prev) => ({ ...prev, bidType: event.target.value }))}
                placeholder="Bid type"
              />
            )}
          </FormField>
          <div className="gmvmax-modal-grid">
            <FormField label="Budget">
              <input
                type="number"
                min="0"
                step="0.01"
                value={form.budget}
                onChange={(event) => setForm((prev) => ({ ...prev, budget: event.target.value }))}
                placeholder="Optional"
              />
            </FormField>
            <FormField label="ROAS bid">
              <input
                type="number"
                min="0"
                step="0.01"
                value={form.roasBid}
                onChange={(event) => setForm((prev) => ({ ...prev, roasBid: event.target.value }))}
                placeholder="Optional"
              />
            </FormField>
          </div>
          <div className="gmvmax-modal-footer">
            <button type="button" onClick={onClose}>
              Cancel
            </button>
            <button type="button" onClick={goNext} disabled={!canProceedStep1}>
              Next
            </button>
          </div>
        </div>
      ) : null}

      {step === 2 ? (
        <div className="gmvmax-modal-step">
          <h3>Select products</h3>
          <ProductSelectionPanel
            products={products}
            selectedIds={localSelectedIds}
            onToggle={toggleProduct}
            onToggleAll={toggleAll}
            storeNames={storeNameById}
            loading={productsLoading}
            emptyMessage={productsLoading ? 'Loading products…' : 'No products available.'}
          />
          <div className="gmvmax-modal-footer">
            <button type="button" onClick={goBack}>
              Back
            </button>
            <button type="button" onClick={goNext} disabled={!canProceedStep2}>
              Next
            </button>
          </div>
        </div>
      ) : null}

      {step === 3 ? (
        <div className="gmvmax-modal-step">
          <h3>Review</h3>
          <dl className="gmvmax-review-list">
            <div>
              <dt>Series name</dt>
              <dd>{form.name || '—'}</dd>
            </div>
            <div>
              <dt>Shopping ads type</dt>
              <dd>{form.shoppingAdsType || '—'}</dd>
            </div>
            <div>
              <dt>Optimization goal</dt>
              <dd>{form.optimizationGoal || '—'}</dd>
            </div>
            <div>
              <dt>Bid type</dt>
              <dd>{form.bidType || '—'}</dd>
            </div>
            <div>
              <dt>Budget</dt>
              <dd>{form.budget ? formatMoney(parseOptionalFloat(form.budget)) : '—'}</dd>
            </div>
            <div>
              <dt>ROAS bid</dt>
              <dd>{form.roasBid ? formatMoney(parseOptionalFloat(form.roasBid)) : '—'}</dd>
            </div>
            <div>
              <dt>Products selected</dt>
              <dd>{selectedProducts.length}</dd>
            </div>
          </dl>
          <ul className="gmvmax-review-products">
            {selectedProducts.slice(0, 10).map((product) => {
              const id = getProductIdentifier(product);
              return (
                <li key={id}>
                  {product?.title || product?.name || id} ({id})
                </li>
              );
            })}
            {selectedProducts.length > 10 ? (
              <li>…and {selectedProducts.length - 10} more</li>
            ) : null}
          </ul>
          {submitError ? <div className="gmvmax-error">{submitError}</div> : null}
          {createMutation.isPending ? <Loading text="Creating series…" /> : null}
          <div className="gmvmax-modal-footer">
            <button type="button" onClick={goBack} disabled={createMutation.isPending}>
              Back
            </button>
            <button type="button" onClick={handleSubmit} disabled={createMutation.isPending}>
              Create series
            </button>
          </div>
        </div>
      ) : null}
    </Modal>
  );
}

