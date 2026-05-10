# Pre-write update.lock with PID=999999 (essentially never allocated).
# kill(999999, 0) raises ProcessLookupError → reclaim path proceeds.
INSTALL_METHOD=npm
CCTALLY_VERSION=1.4.0
FAKE_LOCK_PID=999999
