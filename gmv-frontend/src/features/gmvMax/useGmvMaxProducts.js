import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { fetchProducts } from '../products/service.js';
import { adaptPageInfo, adaptProduct } from '../products/adapters.js';

const DEFAULT_PAGE_INFO = { page: 1, pageSize: 20, total: 0, totalPages: 0 };
const SEARCH_DEBOUNCE = 400;

function ensureFilters(next) {
  return {
    status: next?.status || 'ALL',
    gmvMax: next?.gmvMax || 'ALL',
    productName: next?.productName || '',
  };
}

export default function useGmvMaxProducts(initial = {}) {
  const [query, setQuery] = useState({
    bcId: initial.bcId || '',
    advertiserId: initial.advertiserId || '',
    storeId: initial.storeId || '',
    sortField: initial.sortField || 'min_price',
    sortType: initial.sortType || 'ASC',
    page: initial.page || 1,
    pageSize: initial.pageSize || 20,
  });

  const [filters, setFiltersState] = useState(() => ensureFilters({ productName: initial.productName }));
  const [items, setItems] = useState([]);
  const [pageInfo, setPageInfo] = useState({ ...DEFAULT_PAGE_INFO, pageSize: query.pageSize });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [refreshToken, setRefreshToken] = useState(0);
  const lastRequestRef = useRef(null);
  const [debouncedSearch, setDebouncedSearch] = useState(filters.productName);

  useEffect(() => {
    setQuery((prev) => ({
      ...prev,
      bcId: initial.bcId || '',
      advertiserId: initial.advertiserId || '',
      storeId: initial.storeId || '',
    }));
  }, [initial.bcId, initial.advertiserId, initial.storeId]);

  useEffect(() => {
    setFiltersState((prev) => ensureFilters({ ...prev, productName: initial.productName }));
  }, [initial.productName]);

  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedSearch(filters.productName || '');
    }, SEARCH_DEBOUNCE);
    return () => clearTimeout(timer);
  }, [filters.productName]);

  useEffect(() => {
    setQuery((prev) => ({ ...prev, page: 1 }));
  }, [debouncedSearch, query.bcId, query.advertiserId, query.storeId]);

  useEffect(() => {
    if (!query.bcId || !query.advertiserId || !query.storeId) {
      setItems([]);
      setPageInfo((prev) => ({ ...prev, page: 1, total: 0, totalPages: 0 }));
      setError(null);
      if (lastRequestRef.current) {
        lastRequestRef.current.abort();
        lastRequestRef.current = null;
      }
      return;
    }

    const controller = new AbortController();
    if (lastRequestRef.current) {
      lastRequestRef.current.abort();
    }
    lastRequestRef.current = controller;

    setLoading(true);
    setError(null);

    fetchProducts(
      {
        bcId: query.bcId,
        advertiserId: query.advertiserId,
        storeId: query.storeId,
        productName: debouncedSearch || undefined,
        sortField: query.sortField,
        sortType: query.sortType,
        page: query.page,
        pageSize: query.pageSize,
        adCreationEligible: 'GMV_MAX',
      },
      { signal: controller.signal },
    )
      .then((response) => {
        const rawItems =
          response?.data?.list
          ?? response?.data?.items
          ?? response?.list
          ?? response?.items
          ?? [];
        const adaptedItems = Array.isArray(rawItems) ? rawItems.map(adaptProduct) : [];
        const rawPageInfo = response?.data?.page_info || response?.page_info || {};
        const adaptedPageInfo = adaptPageInfo(rawPageInfo);
        setItems(adaptedItems);
        setPageInfo({ ...DEFAULT_PAGE_INFO, ...adaptedPageInfo });
      })
      .catch((err) => {
        if (err?.name === 'AbortError') return;
        setError(err);
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      });

    return () => {
      controller.abort();
    };
  }, [
    query.bcId,
    query.advertiserId,
    query.storeId,
    query.sortField,
    query.sortType,
    query.page,
    query.pageSize,
    debouncedSearch,
    refreshToken,
  ]);

  const setFilters = useCallback((updater) => {
    setFiltersState((prev) => {
      const next = typeof updater === 'function' ? updater(prev) : { ...prev, ...updater };
      return ensureFilters(next);
    });
  }, []);

  const setPage = useCallback((page) => {
    setQuery((prev) => ({ ...prev, page }));
  }, []);

  const setSort = useCallback((field, type) => {
    setQuery((prev) => ({
      ...prev,
      sortField: field || prev.sortField,
      sortType: type || prev.sortType,
      page: 1,
    }));
  }, []);

  const refresh = useCallback(() => {
    setRefreshToken((prev) => prev + 1);
  }, []);

  const sort = useMemo(() => ({ field: query.sortField, type: query.sortType }), [query.sortField, query.sortType]);

  return {
    items,
    pageInfo,
    loading,
    error,
    filters,
    sort,
    setPage,
    setSort,
    setFilters,
    refresh,
  };
}
