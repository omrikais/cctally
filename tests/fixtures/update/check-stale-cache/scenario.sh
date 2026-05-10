# `update --check` is synchronous: even when the throttle marker says
# the cache isn't due yet, --check re-fetches if state.json has stale
# values. Pre-seeds an aged state.json with latest=1.4.0 (matches
# current); after the run, state.json has latest=1.7.2 (registry says
# so) and current=1.4.0. The throttle marker is left absent so
# _do_update_check fires unconditionally on the first invocation.
INSTALL_METHOD=npm
CCTALLY_VERSION=1.4.0
