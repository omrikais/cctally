# Interactive `daily` command + state.available=true → banner appears on
# stderr trailer. STDOUT_DISCARD discards the (large) daily table; we
# only golden the stderr-only banner.
INSTALL_METHOD=npm
CCTALLY_VERSION=1.4.0
TTY_SIM=1
STDOUT_DISCARD=1
BANNER_CHECK=1
