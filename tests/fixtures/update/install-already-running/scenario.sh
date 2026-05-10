# A live python process holds an LOCK_EX flock on update.lock; the
# cctally subprocess's first LOCK_NB attempt collides, the body's PID
# is alive, and the install rejects with rc=1 + "in progress".
INSTALL_METHOD=npm
CCTALLY_VERSION=1.4.0
FAKE_LOCK_PID=1
FAKE_LOCK_LIVE=1
