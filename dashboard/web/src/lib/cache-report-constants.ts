// CACHE_REPORT_MIN_BASELINE_DAYS mirrors bin/_cctally_cache_report.py —
// keep in sync. CACHE_REPORT_BAND_PP is TS-only: it's the modal's
// display band for the daily hit-% highlight (rows below baseline by
// more than this many points render hit-bad), distinct from the
// configurable anomaly_threshold_pp that drives the kernel's
// cache_drop anomaly trigger. See issue #83 QUAL-10.
export const CACHE_REPORT_MIN_BASELINE_DAYS = 5;
export const CACHE_REPORT_BAND_PP = 5;
