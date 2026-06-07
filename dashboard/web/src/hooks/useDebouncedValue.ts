// dashboard/web/src/hooks/useDebouncedValue.ts
import { useEffect, useState } from 'react';

// Debounce a changing value. `initial` seeds the first emitted value and
// defaults to `value` (standard pass-through); pass a different `initial`
// (e.g. '') to debounce even a non-empty initial mount (trailing-from-cold).
export function useDebouncedValue<T>(value: T, delayMs: number, initial: T = value): T {
  const [debounced, setDebounced] = useState(initial);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(t);
  }, [value, delayMs]);
  return debounced;
}
