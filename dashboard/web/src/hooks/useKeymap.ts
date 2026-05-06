import { useEffect } from 'react';
import { registerKeymap, type Binding } from '../store/keymap';

/**
 * Register keymap bindings while a component is mounted. The unregister
 * runs on unmount. Pass a stable-identity `bindings` array (define it
 * outside the render or memoize), or accept the re-registration cost
 * of a new identity per render.
 */
export function useKeymap(bindings: Binding[]): void {
  useEffect(() => {
    const unreg = registerKeymap(bindings);
    return unreg;
  }, [bindings]);
}
