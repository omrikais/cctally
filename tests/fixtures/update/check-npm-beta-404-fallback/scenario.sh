# `update --check` on the beta channel during the transition window: the
# beta dist-tag is absent (exact 404) so resolution falls back to latest,
# but the install command still pins the EXACT resolved version (never bare
# @beta/@latest). The file:// fixture harness can't emit a real 404, so the
# beta leg is forced via CCTALLY_TEST_UPDATE_NPM_BETA_STATUS.
INSTALL_METHOD=npm
CCTALLY_VERSION=1.4.0
CCTALLY_TEST_UPDATE_NPM_BETA_STATUS=404
