# `update --check --force` bypasses the TTL throttle and refetches even
# when the marker is fresh. Pre-seeds a fresh throttle marker that would
# normally suppress a refresh; verifies state.latest moves from 1.4.0
# (cached) to 1.7.2 (registry) regardless.
INSTALL_METHOD=npm
CCTALLY_VERSION=1.4.0
