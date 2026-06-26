#!/bin/sh
# Start charon in the foreground. Charon's built-in start-scripts
# (configured in /etc/strongswan.conf baked into the image) handle
# loading of credentials, connections, and pools via VICI once the
# daemon is ready. This eliminates the previous race condition where
# the wrapper had to manually call swanctl --load-all and could fire
# it before VICI was accepting connections.
#
# Use absolute path: charon lives at /usr/local/bin/charon (symlink to
# /usr/libexec/ipsec/charon, created in the Dockerfile). The image's
# WORKDIR is /strongswan-build which is empty after build, so ./charon
# would not resolve.
exec /usr/local/bin/charon "$@"
