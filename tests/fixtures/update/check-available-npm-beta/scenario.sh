# `update --check` on the beta channel: resolves SemVer-max(beta, latest)
# and pins the EXACT resolved version in the "Will run" command (never
# bare @beta). beta dist-tag (1.9.0) is newer than latest (1.7.0).
INSTALL_METHOD=npm
CCTALLY_VERSION=1.4.0
