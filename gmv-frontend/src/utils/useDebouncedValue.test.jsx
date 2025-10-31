import { act, renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import useDebouncedValue from './useDebouncedValue.js';

describe('useDebouncedValue', () => {
  it('returns debounced value after delay', () => {
    vi.useFakeTimers();
    const { result, rerender } = renderHook((props) => useDebouncedValue(props, 300), {
      initialProps: 'first',
    });

    expect(result.current).toBe('first');

    rerender('second');
    expect(result.current).toBe('first');

    act(() => {
      vi.advanceTimersByTime(300);
    });

    expect(result.current).toBe('second');
    vi.useRealTimers();
  });
});
