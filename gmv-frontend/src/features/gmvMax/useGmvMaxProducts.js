import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useDispatch, useSelector } from 'react-redux';

import {
  fetchGmvMaxEligibleProducts,
} from '../products/service.js';
import {
  selectErrorByKey,
  selectListByKey,
  selectStatusByKey,
  setError,
  setList,
  setStatus,
} from '../products/productsSlice.js';

const SEARCH_DEBOUNCE = 400;
export const PRODUCTS_TTL_MS = 5 * 60 * 1000;

const FILTER_DEFAULTS = {
  status: 'ALL',
  occupied: 'ALL',
  query: '',
};

const STATUS_SET = new Set(['ALL', 'AVAILABLE', 'NOT_AVAILABLE']);
const OCCUPIED_SET = new Set(['ALL', 'OCCUPIED', 'UNOCCUPIED']);

function normalizeFilters(next = {}) {
  const status = STATUS_SET.has(next.status) ? next.status : FILTER_DEFAULTS.status;
  const occupied = OCCUPIED_SET.has(next.occupied) ? next.occupied : FILTER_DEFAULTS.occupied;
  const query = typeof next.query === 'string' ? next.query : FILTER_DEFAULTS.query;
  return { status, occupied, query };
}

function applyFilters(items = [], filters = FILTER_DEFAULTS) {
  return items.filter((item) => {
    if (filters.status !== 'ALL' && item.status !== filters.status) {
      return false;
    }
    if (filters.occupied === 'OCCUPIED' && item.gmvMaxAdsStatus !== 'OCCUPIED') {
      return false;
    }
    if (filters.occupied === 'UNOCCUPIED' && item.gmvMaxAdsStatus !== 'UNOCCUPIED') {
      return false;
    }
    return true;
  });
}

function shallowEqualArray(a = [], b = []) {
  if (a === b) return true;
  if (a.length !== b.length) return false;
  for (let index = 0; index < a.length; index += 1) {
    if (a[index] !== b[index]) {
      return false;
    }
  }
  return true;
}

function isPageInfoEqual(a = {}, b = {}) {
  return (
    a.page === b.page
    && a.pageSize === b.pageSize
    && a.totalNumber === b.totalNumber
    && a.totalPage === b.totalPage
  );
}

function buildRequestKey({
  bcId,
  advertiserId,
  storeId,
  sortField,
  sortType,
  page,
  pageSize,
  query,
}) {
  return JSON.stringify({
    bcId,
    advertiserId,
    storeId,
    scope: 'GMV_MAX',
    query,
    sort: { field: sortField, type: sortType },
    page,
    pageSize,
  });
}

function buildViewKey({
  bcId,
  advertiserId,
  storeId,
  sortField,
  sortType,
  page,
  pageSize,
  filters,
  query,
}) {
  return JSON.stringify({
    bcId,
    advertiserId,
    storeId,
    scope: 'GMV_MAX',
    filters: { ...filters, query },
    sort: { field: sortField, type: sortType },
    page,
    pageSize,
  });
}

export default function useGmvMaxProducts(initial = {}) {
  const dispatch = useDispatch();
  const desiredSelection = {
    bcId: initial.bcId || '',
    advertiserId: initial.advertiserId || '',
    storeId: initial.storeId || '',
    sortField: initial.sortField || 'min_price',
    sortType: initial.sortType || 'ASC',
    page: Number(initial.page) > 0 ? Number(initial.page) : 1,
    pageSize: Number(initial.pageSize) > 0 ? Number(initial.pageSize) : 20,
  };

  const desiredFilters = normalizeFilters({
    status: initial.filters?.status ?? initial.status,
    occupied: initial.filters?.occupied ?? initial.occupied,
    query:
      initial.productName
      ?? initial.filters?.query
      ?? initial.query
      ?? FILTER_DEFAULTS.query,
  });

  const [selection, setSelection] = useState(desiredSelection);
  const [filters, setFiltersState] = useState(desiredFilters);
  const [debouncedQuery, setDebouncedQuery] = useState(desiredFilters.query.trim());
  const [refreshTick, setRefreshTick] = useState(0);
  const refreshForceRef = useRef(false);
  const activeRequestRef = useRef(null);

  useEffect(() => {
    setSelection((prev) => {
      const next = { ...prev };
      let changed = false;
      if (prev.bcId !== desiredSelection.bcId) {
        next.bcId = desiredSelection.bcId;
        changed = true;
      }
      if (prev.advertiserId !== desiredSelection.advertiserId) {
        next.advertiserId = desiredSelection.advertiserId;
        changed = true;
      }
      if (prev.storeId !== desiredSelection.storeId) {
        next.storeId = desiredSelection.storeId;
        changed = true;
      }
      if (prev.sortField !== desiredSelection.sortField) {
        next.sortField = desiredSelection.sortField;
        changed = true;
      }
      if (prev.sortType !== desiredSelection.sortType) {
        next.sortType = desiredSelection.sortType;
        changed = true;
      }
      if (prev.pageSize !== desiredSelection.pageSize) {
        next.pageSize = desiredSelection.pageSize;
        changed = true;
      }
      if (prev.page !== desiredSelection.page) {
        next.page = desiredSelection.page;
        changed = true;
      }
      return changed ? next : prev;
    });
  }, [
    desiredSelection.bcId,
    desiredSelection.advertiserId,
    desiredSelection.storeId,
    desiredSelection.sortField,
    desiredSelection.sortType,
    desiredSelection.page,
    desiredSelection.pageSize,
  ]);

  useEffect(() => {
    setFiltersState((prev) => {
      const next = normalizeFilters({
        status: desiredFilters.status,
        occupied: desiredFilters.occupied,
        query: desiredFilters.query,
      });
      if (
        prev.status !== next.status
        || prev.occupied !== next.occupied
        || prev.query !== next.query
      ) {
        return next;
      }
      return prev;
    });
  }, [desiredFilters.status, desiredFilters.occupied, desiredFilters.query]);

  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedQuery(filters.query.trim());
    }, SEARCH_DEBOUNCE);
    return () => clearTimeout(timer);
  }, [filters.query]);

  useEffect(() => {
    return () => {
      if (activeRequestRef.current) {
        activeRequestRef.current.abort();
        activeRequestRef.current = null;
      }
    };
  }, []);

  const trimmedQuery = debouncedQuery;

  const requestKey = useMemo(
    () =>
      buildRequestKey({
        bcId: selection.bcId,
        advertiserId: selection.advertiserId,
        storeId: selection.storeId,
        sortField: selection.sortField,
        sortType: selection.sortType,
        page: selection.page,
        pageSize: selection.pageSize,
        query: trimmedQuery,
      }),
    [
      selection.bcId,
      selection.advertiserId,
      selection.storeId,
      selection.sortField,
      selection.sortType,
      selection.page,
      selection.pageSize,
      trimmedQuery,
    ],
  );

  const viewKey = useMemo(
    () =>
      buildViewKey({
        bcId: selection.bcId,
        advertiserId: selection.advertiserId,
        storeId: selection.storeId,
        sortField: selection.sortField,
        sortType: selection.sortType,
        page: selection.page,
        pageSize: selection.pageSize,
        filters,
        query: trimmedQuery,
      }),
    [
      selection.bcId,
      selection.advertiserId,
      selection.storeId,
      selection.sortField,
      selection.sortType,
      selection.page,
      selection.pageSize,
      filters,
      trimmedQuery,
    ],
  );

  const baseEntry = useSelector((state) => selectListByKey(state, requestKey));
  const baseStatus = useSelector((state) => selectStatusByKey(state, requestKey));
  const baseError = useSelector((state) => selectErrorByKey(state, requestKey));

  const viewEntry = useSelector((state) => selectListByKey(state, viewKey));
  const viewStatus = useSelector((state) => selectStatusByKey(state, viewKey));
  const viewError = useSelector((state) => selectErrorByKey(state, viewKey));

  const baseItems = baseEntry?.items || [];
  const filteredItems = useMemo(
    () => applyFilters(baseItems, filters),
    [baseItems, filters],
  );

  useEffect(() => {
    if (!selection.bcId || !selection.storeId || !selection.advertiserId) {
      if (activeRequestRef.current) {
        activeRequestRef.current.abort();
        activeRequestRef.current = null;
      }
      if (refreshForceRef.current) {
        refreshForceRef.current = false;
      }
      return undefined;
    }

    const updatedAt = baseEntry?.updatedAt || 0;
    const expired = Date.now() - updatedAt > PRODUCTS_TTL_MS;
    const shouldFetch = refreshForceRef.current || !baseEntry || expired;

    if (!shouldFetch) {
      return undefined;
    }

    if (activeRequestRef.current) {
      activeRequestRef.current.abort();
    }

    const controller = new AbortController();
    activeRequestRef.current = controller;

    dispatch(setStatus({ key: requestKey, status: 'loading' }));
    dispatch(setStatus({ key: viewKey, status: 'loading' }));

    fetchGmvMaxEligibleProducts({
      bcId: selection.bcId,
      storeId: selection.storeId,
      advertiserId: selection.advertiserId,
      page: selection.page,
      pageSize: selection.pageSize,
      productName: trimmedQuery || undefined,
      sortField: selection.sortField,
      sortType: selection.sortType,
      signal: controller.signal,
    })
      .then(({ items, pageInfo }) => {
        const updatedAt = Date.now();
        dispatch(setList({ key: requestKey, items, pageInfo, updatedAt }));
        const nextFiltered = applyFilters(items, filters);
        dispatch(setList({ key: viewKey, items: nextFiltered, pageInfo, updatedAt }));
        dispatch(setStatus({ key: requestKey, status: 'succeeded' }));
        dispatch(setStatus({ key: viewKey, status: 'succeeded' }));
      })
      .catch((error) => {
        if (error?.name === 'AbortError') {
          return;
        }
        dispatch(setError({ key: requestKey, error }));
        dispatch(setError({ key: viewKey, error }));
      })
      .finally(() => {
        if (activeRequestRef.current === controller) {
          activeRequestRef.current = null;
        }
        refreshForceRef.current = false;
      });

    return () => {
      controller.abort();
    };
  }, [
    dispatch,
    filters,
    requestKey,
    selection.advertiserId,
    selection.bcId,
    selection.page,
    selection.pageSize,
    selection.sortField,
    selection.sortType,
    selection.storeId,
    baseEntry,
    trimmedQuery,
    viewKey,
    refreshTick,
  ]);

  useEffect(() => {
    if (!baseEntry || !selection.bcId || !selection.storeId || !selection.advertiserId) {
      return;
    }

    const needsItemsUpdate = !viewEntry || !shallowEqualArray(viewEntry.items, filteredItems);
    const needsPageInfoUpdate = !viewEntry || !isPageInfoEqual(viewEntry.pageInfo, baseEntry.pageInfo);
    const needsStatusUpdate = viewStatus !== baseStatus;
    const needsErrorUpdate = baseError !== viewError;

    if (needsItemsUpdate || needsPageInfoUpdate) {
      dispatch(
        setList({
          key: viewKey,
          items: filteredItems,
          pageInfo: baseEntry.pageInfo,
          updatedAt: baseEntry.updatedAt,
        }),
      );
    }
    if (needsStatusUpdate) {
      dispatch(setStatus({ key: viewKey, status: baseStatus }));
    }
    if (needsErrorUpdate) {
      if (baseError) {
        dispatch(setError({ key: viewKey, error: baseError }));
      } else if (viewError) {
        dispatch(setStatus({ key: viewKey, status: baseStatus }));
      }
    }
  }, [
    baseEntry,
    baseError,
    baseStatus,
    dispatch,
    filteredItems,
    selection.advertiserId,
    selection.bcId,
    selection.storeId,
    viewEntry,
    viewError,
    viewKey,
    viewStatus,
  ]);

  useEffect(() => {
    setSelection((prev) => {
      if (prev.page === 1) {
        return prev;
      }
      return { ...prev, page: 1 };
    });
  }, [filters.status, filters.occupied, trimmedQuery]);

  const items = viewEntry?.items || filteredItems;
  const pageInfo = viewEntry?.pageInfo
    || baseEntry?.pageInfo
    || {
      page: selection.page,
      pageSize: selection.pageSize,
      totalNumber: 0,
      totalPage: 0,
    };

  const loading = viewStatus === 'loading' || baseStatus === 'loading';
  const error = viewError || baseError || null;

  const setPage = useCallback((page) => {
    const nextPage = Number(page) || 1;
    setSelection((prev) => ({ ...prev, page: nextPage < 1 ? 1 : nextPage }));
  }, []);

  const setSort = useCallback((field, type) => {
    setSelection((prev) => ({
      ...prev,
      sortField: field || prev.sortField,
      sortType: type || prev.sortType,
      page: 1,
    }));
  }, []);

  const setFilters = useCallback((updater) => {
    setFiltersState((prev) => {
      const next = typeof updater === 'function' ? updater(prev) : { ...prev, ...updater };
      return normalizeFilters(next);
    });
    setSelection((prev) => ({ ...prev, page: 1 }));
  }, []);

  const refresh = useCallback((options = {}) => {
    if (options?.force) {
      refreshForceRef.current = true;
    }
    setRefreshTick((prev) => prev + 1);
  }, []);

  return {
    items,
    pageInfo,
    loading,
    error,
    filters,
    sort: { field: selection.sortField, type: selection.sortType },
    setPage,
    setSort,
    setFilters,
    refresh,
  };
}
