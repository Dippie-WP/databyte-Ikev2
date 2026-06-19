# strongSwan 6.0.7 - custom build with attr-sql + sqlite + EAP-MSCHAPv2
#
# 2026-06-16 r2 changes (vs upstream strongx509/strongswan:6.0.6):
#   - +--enable-eap-mschapv2 --enable-eap-radius (upstream lacks these)
#   - VERSION=6.0.7 for CVE-2026-47895 (libstrongswan double-free)
#   - +--enable-attr-sql --enable-sqlite (per-identity static VIP leases)
#   - Note: --enable-pools is a no-op flag (it's for charon-tkm only);
#           charon's built-in IP pool is enabled by default
#   - Bake /etc/strongswan.conf with start-scripts (auto-loads creds/conns/pools
#     at startup, eliminates the race where wrapper ran swanctl --load-all
#     before VICI was up)
#
# Build lessons (logged 2026-06-17):
#   - libsqlite3-dev is needed at compile time
#   - libsqlite3-0 (runtime) MUST be reinstalled after `apt-get autoremove`
#     strips it. Otherwise attr-sql .so fails to load.
#   - Plugin load log line is the ground truth: `grep 'loaded plugins' /var/log/charon.log`
FROM ubuntu:24.04
LABEL org.opencontainers.image.authors="Andreas Steffen <andreas.steffen@strongswan.org> (upstream); Zun (fork)"
LABEL org.opencontainers.image.source="https://github.com/strongX509/docker"
LABEL zun.fork.delta="+eap-mschapv2 +eap-radius; +attr-sql +sqlite; VERSION 6.0.7 for CVE-2026-47895; baked strongswan.conf with start-scripts"
ENV VERSION="6.0.7"

WORKDIR /strongswan-build

RUN \
  DEV_PACKAGES="wget bzip2 make gcc libssl-dev libsqlite3-dev" && \
  apt-get -y update && \
  apt-get -y install iproute2 iputils-ping nano $DEV_PACKAGES && \
  \
  wget --progress=dot:giga https://download.strongswan.org/strongswan-$VERSION.tar.bz2 && \
  tar xfj strongswan-$VERSION.tar.bz2 && \
  cd strongswan-$VERSION && \
  ./configure --prefix=/usr --sysconfdir=/etc --disable-defaults      \
    --enable-charon --enable-ikev2 --enable-nonce --enable-random     \
    --enable-openssl --enable-pem --enable-x509 --enable-pubkey       \
    --enable-constraints --enable-pki --enable-socket-default         \
    --enable-kernel-netlink --enable-swanctl --enable-resolve         \
    --enable-eap-identity --enable-eap-md5 --enable-eap-dynamic       \
    --enable-eap-tls --enable-updown --enable-vici --enable-ml        \
    --enable-eap-mschapv2 --enable-eap-radius                         \
    --enable-attr-sql --enable-sqlite                                 \
    && \
   make all && make install && \
   cd / && rm -R /strongswan-build && \
   ln -s /usr/libexec/ipsec/charon /usr/local/bin/charon && \
   \
   apt-get -y remove $DEV_PACKAGES && \
   apt-get -y autoremove && \
   # Re-install libsqlite3 runtime — the dev package pulled it in but autoremove
   # removed it once -dev was gone. The attr-sql plugin .so needs libsqlite3.so.0.
   apt-get -y install --no-install-recommends libsqlite3-0 && \
   apt-get clean && \
   rm -rf /var/lib/apt/lists/*

# Bake the strongswan.conf that auto-loads credentials/conns/pools at startup.
# This file lives at /etc/strongswan.conf. Operator's strongswan.d/*.conf
# bind-mount can still override charon settings per the strongswan.conf
# include chain (the strongswan.d/ pattern is included by default).
COPY strongswan.conf /etc/strongswan.conf

COPY start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 500 4500
