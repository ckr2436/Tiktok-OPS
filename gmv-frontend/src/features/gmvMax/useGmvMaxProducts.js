import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { getStoreProducts } from '../products/service.js';

const DEFAULT_PAGE_INFO = { page: 1, pageSize: 20, total: 0, totalPages: 0 };
const SEARCH_DEBOUNCE = 400;

const FILTER_DEFAULTS = {
  status: 'ALL',
  occupied: 'ALL',
  query: '',
};

function normalizeFilters(next) {
  if (!next || typeof next !== 'object') {
    return { ...FILTER_DEFAULTS };
  }
  return {
    status: next.status || FILTER_DEFAULTS.status,
    occupied: next.occupied || FILTER_DEFAULTS.occupied,
    query: typeof next.query === 'string' ? next.query : FILTER_DEFAULTS.query,
  };
}

export default function useGmvMaxProducts(initial = {}) {
  const [queryState, setQueryState] = useState({
    bcId: initial.bcId || '',
    advertiserId: initial.advertiserId || '',
    storeId: initial.storeId || '',
    sortField: initial.sortField || 'min_price',
    sortType: initial.sortType || 'ASC',
    page: initial.page || 1,
    pageSize: initial.pageSize || 20,
  });

  const [filters, setFiltersState] = useState(() =>
    normalizeFilters({ query: initial.productName || initial.query }),
  );
  const [debouncedQuery, setDebouncedQuery] = useState(
    normalizeFilters({ query: initial.productName || initial.query }).query,
  );
  const [rawItems, setRawItems] = useState([]);
  const [pageInfo, setPageInfo] = useState({ ...DEFAULT_PAGE_INFO, pageSize: queryState.pageSize });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [refreshToken, setRefreshToken] = useState(0);
  const requestRef = useRef(null);

  useEffect(() => {
    setQueryState((prev) => ({
      ...prev,
      bcId: initial.bcId || '',
      advertiserId: initial.advertiserId || '',
      storeId: initial.storeId || '',
    }));
  }, [initial.bcId, initial.advertiserId, initial.storeId]);

  useEffect(() => {
    setFiltersState((prev) =>
      normalizeFilters({ ...prev, query: initial.productName || initial.query || '' }),
    );
  }, [initial.productName, initial.query]);

  useEffect(() => {
    const timer = setTimeout(() => {
      const value = typeof filters.query === 'string' ? filters.query.trim() : '';
      setDebouncedQuery(value);
    }, SEARCH_DEBOUNCE);
    return () => clearTimeout(timer);
  }, [filters.query]);

  useEffect(() => {
    setQueryState((prev) => ({
      ...prev,
      page: 1,
    }));
  }, [debouncedQuery, queryState.bcId, queryState.advertiserId, queryState.storeId]);

  useEffect(() => {
    if (!queryState.bcId || !queryState.storeId || !queryState.advertiserId) {
      setRawItems([]);
      setPageInfo((prev) => ({ ...prev, page: 1, total: 0, totalPages: 0 }));
      setError(null);
      if (requestRef.current) {
        requestRef.current.cancel();
        requestRef.current = null;
      }
      return undefined;
    }

    if (requestRef.current) {
      requestRef.current.cancel();
    }

    const request = getStoreProducts({
      bcId: queryState.bcId,
      storeId: queryState.storeId,
      advertiserId: queryState.advertiserId,
      productName: debouncedQuery || undefined,
      sortField: queryState.sortField,
      sortType: queryState.sortType,
      page: queryState.page,
      pageSize: queryState.pageSize,
      scope: 'GMV_MAX',
    });

    requestRef.current = request;
    setLoading(true);
    setError(null);

    request.promise
      .then(({ items, pageInfo: info }) => {
        setRawItems(Array.isArray(items) ? items : []);
        setPageInfo({ ...DEFAULT_PAGE_INFO, ...info });
      })
      .catch((err) => {
        if (err?.name === 'AbortError') return;
        setError(err);
        setRawItems([]);
      })
      .finally(() => {
        if (requestRef.current === request) {
          requestRef.current = null;
        }
        setLoading(false);
      });

    return () => {
      request.cancel();
    };
  }, [
    queryState.bcId,
    queryState.storeId,
    queryState.advertiserId,
    queryState.sortField,
    queryState.sortType,
    queryState.page,
    queryState.pageSize,
    debouncedQuery,
    refreshToken,
  ]);

  const items = useMemo(() => {
    return rawItems.filter((item) => {
      if (filters.status !== 'ALL' && item.status !== filters.status) {
        return false;
      }
      if (filters.occupied !== 'ALL') {
        if (filters.occupied === 'OCCUPIED' && item.gmvMaxAdsStatus !== 'OCCUPIED') {
          return false;
        }
        if (filters.occupied === 'UNOCCUPIED' && item.gmvMaxAdsStatus !== 'UNOCCUPIED') {
          return false;
        }
      }
      return true;
    });
  }, [rawItems, filters.status, filters.occupied]);

  const setPage = useCallback((page) => {
    const nextPage = Number(page) || 1;
    setQueryState((prev) => ({ ...prev, page: nextPage < 1 ? 1 : nextPage }));
  }, []);

  const setSort = useCallback((field, type) => {
    setQueryState((prev) => ({
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
    setQueryState((prev) => ({ ...prev, page: 1 }));
  }, []);

  const refresh = useCallback(() => {
    setRefreshToken((prev) => prev + 1);
  }, []);

  const sort = useMemo(
    () => ({ field: queryState.sortField, type: queryState.sortType }),
    [queryState.sortField, queryState.sortType],
  );

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
