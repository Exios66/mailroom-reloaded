// Mirror of obs/attrs.STATIONS; tests/tui/test_replay_stations.py keeps them equal.
// Pure data, no DOM and no imports.

/** Stations in track order; `order` is the index. */
export const STATIONS = Object.freeze([
  Object.freeze({ id: 'intake', label: 'intake', phase: 'intake_sort', kind: 'main', color_token: '--term-fg-dim', order: 0 }),
  Object.freeze({ id: 'sorter', label: 'sorter', phase: 'intake_sort', kind: 'main', color_token: '--term-cyan', order: 1 }),
  Object.freeze({ id: 'gate', label: 'gate', phase: 'intake_sort', kind: 'main', color_token: '--term-phosphor', order: 2 }),
  Object.freeze({ id: 'specialist', label: 'specialist', phase: 'extraction', kind: 'main', color_token: '--term-amber', order: 3 }),
  Object.freeze({ id: 'judge', label: 'judge', phase: 'extraction', kind: 'detour', color_token: '--term-station-judge', order: 4 }),
  Object.freeze({ id: 'boss', label: 'boss', phase: 'extraction', kind: 'detour', color_token: '--term-red', order: 5 }),
  Object.freeze({ id: 'review', label: 'review', phase: 'review', kind: 'bay', color_token: '--term-station-review', order: 6 }),
  Object.freeze({ id: 'archive', label: 'archive', phase: 'reporting', kind: 'main', color_token: '--term-green', order: 7 }),
  Object.freeze({ id: 'failed', label: 'failed', phase: 'terminal', kind: 'bay', color_token: '--term-red', order: 8 }),
]);

export const STATION_IDS = Object.freeze(STATIONS.map((s) => s.id));
