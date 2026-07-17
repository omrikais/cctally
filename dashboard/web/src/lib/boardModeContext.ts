import { createContext } from 'react';
import type { BoardMode } from './boardLayout';

// #293 S3 — the single board-mode resolver. `App` provides the value from its
// ONE useBoardMode() call; stacked panels (Weekly/Monthly) read it via
// useContext to slice their summary window WITHOUT registering duplicate
// matchMedia listeners or drifting a frame out of sync with the grid's
// data-board-mode. Default 'bento' so a panel rendered without a provider
// (unit tests) shows all rows — the pre-S3 behavior.
export const BoardModeContext = createContext<BoardMode>('bento');
