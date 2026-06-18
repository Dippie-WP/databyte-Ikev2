#!/bin/sh
# Start charon in the foreground. Charon's built-in start-scripts
# (configured in /etc/strongswan.conf baked into the image) handle
# loading of credentials, connections, and pools via VICI once the
# daemon is ready. This eliminates the previous race condition where
# the wrapper had to manually call swanctl --load-all and could fire
# it before VICI was accepting connections.
exec ./charon "$@"
