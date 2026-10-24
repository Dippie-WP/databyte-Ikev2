# PER-HOST CERT — Per-Site Certificate Issuance Runbook (TKT-041)

> **NOT a generic-template doc.**
>
> **Status:** DRAFT — re-purposed 2026-10-24 from the former wildcard plan (`*.myvpn.databyte.co.za`) to the
> **per-host, per-site** procedure decided by Zun (TKT-041, msgs #46104 / #46112 / #46127). The old scope is
> withdrawn: a wildcard would let any site's certificate vouch for another site's name, including the
> customer-payment name, across a multi-site fleet.
>
> **Scope:** Databyte-internal — issue ONE Let's Encrypt certificate per site host, for that site's own
> canonical FQDN only, and install it as both the HTTPS and the IKEv2 server identity on that host.
>
> **Operator:** Zun. Misha advises and drafts; the issuance command is executed by the operator.
>
> **Companion docs:** [`ROADMAP.md`](./ROADMAP.md) · [`VPS-XNEELO-DEPLOY.md`](./VPS-XNEELO-DEPLOY.md) (manual
> per-site build — Ansible is OUT per Zun 2026-10-24) · [`CLOUDFLARE-DNS.md`](./CLOUDFLARE-DNS.md) (API token
> setup) · [LE DNS-01 docs](https://letsencrypt.org/docs/challenge-types/#dns-01-challenge)
>
> **Ticket:** TKT-041 · **History:** the previous wildcard content is preserved in git history at commit
> `902745e` (this file is tracked and was last committed unmodified).

---

# PER-HOST CERT — Per-Site Certificate Issuance Runbook (TKT-041)

| Field | Value |
|---|---|
| Document ID | DAT-OPS-CERT-PERHOST-001 |
| Revision | v0.2.0 (DRAFT — replaces the v0.1.0 wildcard plan) |
| Date (SAST) | 2026-10-24 |
| Author | Misha 🐻 (Zun's assistant) |
| Authoritative source of facts | `ssh root@<site-host>` + `/etc/letsencrypt/renewal/<site>.conf` + `/etc/letsencrypt/live/<site>/cert.pem` + `/etc/nginx/nginx.conf` + `/etc/nginx/sites-enabled/` + `docker/swanctl/conf.d/rw-eap.conf` + the certbot deploy hook |
| ISO 9001:2015 | Document Control: internal — distribution restricted to operators |
| Verification | Each step is independently verifiable; assumes operator has SSH to the site host, a Cloudflare API token for the site, and the per-site build from `VPS-XNEELO-DEPLOY.md` |
| Live fact-check | 2026-10-24 05:20–06:50 UTC — verified against `vpn-prod-01` (JHB, grandfathered): certbot 4.0.0, `dns-cloudflare` plugin present, `/root/.cloudflare.ini` mode 0600, `databyte.co.za` on Cloudflare NS, nginx 1.26.3-3+deb13u10 as a host package |
| Status | DRAFT — not yet executed on any site host |

---

## 0. Purpose and the one rule that governs everything

Issue a Let's Encrypt certificate for **one site host**, covering **that site's own canonical FQDN only**,
and install it as the host's HTTPS certificate **and** its IKEv2 server identity.

**The governing rule:** on this estate the LE certificate and its key are **also** the strongSwan IKEv2
server identity. They are the same bytes:

```
0feecc68a07cc79ec328e1d45c1e0b8fcd3239f3fc8fb9f71abf32810c64a4e8  /opt/strongswan-vpn-gateway/docker/swanctl/x509/server.crt.pem
0feecc68a07cc79ec328e1d45c1e0b8fcd3239f3fc8fb9f71abf32810c64a4e8  /etc/letsencrypt/live/myvpn.databyte.co.za/cert.pem
5336392045c13523a04b04e0cdee05e4274133f5f22e6a038b3175c3f3b90e6b  /opt/strongswan-vpn-gateway/docker/swanctl/private/server-key.pem
5336392045c13523a04b04e0cdee05e4274133f5f22e6a038b3175c3f3b90e6b  /etc/letsencrypt/live/myvpn.databyte.co.za/privkey.pem
```

**A certificate change on a site is therefore a change to that site's payment-VPN server identity.**
There is no separate "web cert" and "VPN cert" to reason about. Every step below treats them as one object.

---

## 1. Preconditions — per site

### 1.1 The per-site values

| Value | Meaning | Example (Cape Town, instance 01) |
|---|---|---|
| `<SITE_ID>` | Canonical short name: `<country>-<site>-<nn>-<role>` | `za-cpt-01-vpn` |
| `<SITE_FQDN>` | `<SITE_ID>.databyte.co.za` — the ONLY name in the cert | `za-cpt-01-vpn.databyte.co.za` |
| `<SITE_IP>` | The site's public IPv4 | `203.0.113.10` |
| `<SITE_HOST>` | SSH target for the site host | `root@<SITE_IP>` |

Site registry (final, Zun msgs #46071 / #46076): `za-cpt` Cape Town · `za-jnb` Johannesburg · `fr-par` France,
Paris. Country = ISO 3166-1 alpha-2, site = UN/LOCODE place code, hyphen-joined. The **live JHB host stays
grandfathered** as `vpn-prod-01` / `myvpn.databyte.co.za` and is not renamed — the schema applies to NEW hosts.

### 1.2 Already required, all verified live 2026-10-24 on the JHB host

| Item | Where | Verified |
|---|---|---|
| certbot 4.0.0 | `/usr/bin/certbot` | 2026-10-24 |
| `dns-cloudflare` plugin | `certbot plugins` | 2026-10-24 |
| Cloudflare API token | `/root/.cloudflare.ini`, mode 0600 | 2026-10-24 |
| Zone on Cloudflare NS | `dig +short databyte.co.za NS` → `maeve/bjorn.ns.cloudflaressl.com` | 2026-10-24 |
| certbot timer | `certbot.timer` — ~08:32 and ~15:28 SAST daily | 2026-10-24 |

**Per site, the operator creates:**
1. **A Cloudflare API token** for that site — `Zone:DNS:Edit` on `databyte.co.za`, stored on that site host
   only, at `/root/.cloudflare.ini`, mode 0600. One token per site (revocable independently).
2. **One DNS A record** — `<SITE_FQDN>` → `<SITE_IP>`, **DNS-only / grey-cloud**, TTL auto.
   **No wildcard record.** The VPN endpoint must stay DNS-only: a proxied Cloudflare address breaks
   UDP 500/4500.
3. **One Telegram bot** (BotFather) for that site, with its own token and webhook secret on that host.
   Telegram permits only one long-poller per token, so the bot cannot be shared between sites.

### 1.3 DNS check before issuance

```bash
dig +short <SITE_FQDN> NS
dig +short <SITE_FQDN> A          # must be <SITE_IP>, unproxied
```

A wildcard requires the zone and the name on the **same** Cloudflare zone — compare the NS records before
issuing, or DNS-01 fails on a delegation that looks fine on paper.

---

## 2. Map every consumer of the certificate and the key — BEFORE issuing

A certificate on a production host is read by more than one program. On this estate it is read by **two**,
and an automated hook copies it into a third.

```bash
ssh root@<SITE_HOST> '
c=/etc/letsencrypt/live/<SITE_FQDN>/cert.pem; k=/etc/letsencrypt/live/<SITE_FQDN>/privkey.pem
sha256sum "$c" "$k"
for root in /etc/letsencrypt /etc/ssl /opt /etc; do
  find "$root" -xdev -maxdepth 6 -type f \( -name "*.pem" -o -name "*.crt" -o -name "*.key" \) 2>/dev/null
done | while read -r f; do
  h=$(sha256sum "$f" 2>/dev/null | cut -d" " -f1)
  case "$h" in
    <cert-hash>) echo "CERT-COPY  $f" ;;
    <key-hash>)  echo "KEY-COPY   $f" ;;
  esac
done'

ssh root@<SITE_HOST> 'for s in /etc/letsencrypt/renewal-hooks/*/*; do
  [ -f "$s" ] || continue; echo "--- $s"
  grep -nE "RENEWED_LINEAGE|RENEWED_DOMAINS|x509|privkey|install|swanctl|nginx" "$s"
done'
```

**Consumer table — fill this in per site before issuing. A row whose "breaks" column is not empty is the
operator's decision, made before issuance, not a discovery at the next renewal.**

| Consumer | Path on the host | Reads | Breaks if the SAN set or CN changes |
|---|---|---|---|
| nginx | `/etc/nginx/sites-enabled/<vhost>` → `ssl_certificate` | cert + key | TLS handshake fails for that name; portal unreachable |
| strongSwan IKEv2 | `/opt/strongswan-vpn-gateway/docker/swanctl/x509/server.crt.pem` + `private/server-key.pem` (bind-mounted to `/etc/swanctl` in the container) | cert + key | **IKE_SA_INIT / IKE_AUTH fails → every customer on that site is cut off.** The `local.id` in `rw-eap.conf` must equal `<SITE_FQDN>` or the client-side identity check fails first |
| certbot deploy hook | `/usr/local/sbin/databyte-strongswan-deploy-hook.sh`, bound per lineage via `renew_hook` in `/etc/letsencrypt/renewal/<SITE_FQDN>.conf` | writes cert + key into the strongswan dir, then `swanctl --load-creds` | hook aborts → renewal recorded as failed → the cert stops renewing. **Never** in `/etc/letsencrypt/renewal-hooks/deploy/` — that dir is global and fires on any lineage |

**Also find the file the web server actually reads.** `sites-enabled/X` is not always a symlink to
`sites-available/X`, and the two copies drift:

```bash
ssh root@<SITE_HOST> 'grep -nE "include .*(sites-enabled|conf\.d)" /etc/nginx/nginx.conf
for f in /etc/nginx/sites-enabled/*; do
  stat -c "%n type=%F" "$f"
  diff -u "/etc/nginx/sites-available/$(basename "$f")" "$f"
done'
```

> **Live finding (JHB, 2026-10-24):** `sites-enabled/vpn-portal` is a **regular file**, not a symlink, and it
> has **drifted** from `sites-available/vpn-portal` (`limit_req zone=portal_api burst=100 nodelay;` enabled vs
> `burst=10` available). Only `sites-enabled/adminer` is a symlink. **The file nginx reads is
> `sites-enabled/vpn-portal`** — edit that copy, and reconcile the other one so the next editor does not change
> nothing.

---

## 3. The deploy hook must be scoped to ONE lineage — PRECONDITION

**On the live JHB host this is DONE (2026-10-24 14:24 UTC, option B — per-lineage `renew_hook`).** The hook
that used to sit globally in `/etc/letsencrypt/renewal-hooks/deploy/deploy-hook.sh` (md5
`a62410c85eecf590e87fbffbbd45c43f`) was:

- moved to `/usr/local/sbin/databyte-strongswan-deploy-hook.sh` — same bytes, same md5,
- **removed from the global hook directory, which is now empty**,
- bound to the **myvnp lineage only** via `renew_hook = /usr/local/sbin/databyte-strongswan-deploy-hook.sh`
  in `/etc/letsencrypt/renewal/myvpn.databyte.co.za.conf`.

`adminer.databyte.co.za.conf` carries **no** `renew_hook`, so an adminer renewal can no longer touch the
strongswan x509 directory at all. Verified live on `vpn-prod-01`: certbot's own loader
(`renewal.reconstitute`) reads `renew_hook` from the myvnp conf and `None` from the adminer conf; certbot's own
conf rewrite (`storage.write_renewal_config`) **preserves** `renew_hook`; the relocated hook ran with
`RENEWED_LINEAGE` set and exited 0 with the identity sha256 unchanged (`server.pem` `0feecc68a07c…`,
`server-key.pem` `53363920…90e6b`), charon still holding `CN=myvpn.databyte.co.za`, SA count 1. Host backup:
`/root/backups/le-hook-20261024T142411Z`.

**Why scoping matters.** A hook in `/etc/letsencrypt/renewal-hooks/deploy/` is **global**: certbot runs it after
*any* successful renewal on that host, and it takes its source from `$RENEWED_LINEAGE`. The moment a second
lineage exists on a host, a renewal of the **wrong** lineage pushes the **wrong** certificate into the strongswan
x509 directory and reloads charon with it — a live customer-facing VPN outage caused by an unrelated certificate
renewing.

Two scoping mechanisms exist, and both are used:

| Mechanism | How it works | Where |
|---|---|---|
| Per-lineage `renew_hook` | the hook lives OUTSIDE the global hook dir; the lineage's renewal conf names it | **JHB, live** |
| Per-host lineage guard | the hook lives in the global dir but `exit 0`s on a foreign lineage **before any write** | new site hosts |

certbot 4.0.0 supports the first natively: `_internal/hooks.py` `renew_hook()` runs the directory hooks first
and then `config.renew_hook`; `renewal.py` restores `renew_hook` from the renewal conf's `[renewalparams]` on
every renewal (`STR_CONFIG_ITEMS`), and `storage.write_renewal_config()` preserves it when certbot rewrites
the conf after a renewal.

**For NEW site hosts use the guarded repo copy** `host/letsencrypt/deploy-hook.sh` (md5
`ce375da1914ae08f74059706f8b4fe38`, **uncommitted**). It runs **before any file is touched** and compares the
lineage NAME rather than the path, so it works whichever absolute path certbot passes:

```bash
# ---- 0. per-host lineage guard (TKT-041) ----
if [[ -z "${SITE_FQDN:-}" && -r /etc/default/site-cert-watch ]]; then
  . /etc/default/site-cert-watch
fi
SITE_FQDN="${SITE_FQDN:-}"
if [[ -z "${SITE_FQDN}" ]]; then
  log_error "no SITE_FQDN configured - set it in /etc/default/site-cert-watch (or export it)"
  exit 1
fi
EXPECTED_LINEAGE_NAME="${EXPECTED_LINEAGE_NAME:-${SITE_FQDN}}"
RENEWED_LINEAGE_NAME="$(basename "${LE_LIVE}")"
if [[ "${RENEWED_LINEAGE_NAME}" != "${EXPECTED_LINEAGE_NAME}" ]]; then
  log_info "skipping: renewed lineage '${RENEWED_LINEAGE_NAME}' is not this host's IKEv2 lineage '${EXPECTED_LINEAGE_NAME}'"
  exit 0
fi
```

The verification name is no longer hard-coded — it is `$SITE_FQDN`:

```bash
if ! echo "$loaded" | grep -q "${SITE_FQDN}"; then
  log_error "new LE cert NOT loaded - list-certs does not contain ${SITE_FQDN}"
```

**Per-host configuration.** The hook and the per-site watcher read the same file,
`/etc/default/site-cert-watch`:

```
# /etc/default/site-cert-watch
SITE_FQDN=za-cpt-01-vpn.databyte.co.za
```

Without it the guarded hook **REFUSES** (exit 1) rather than installing a certificate it cannot attribute to a
site. Failing closed is the point: a wrong install is a customer VPN outage, a refusal is a failed renewal
that survives until the next attempt. (The JHB hook predates the guard and does not read this file — it is
scoped by certbot's `renew_hook` instead, which is the same protection from the other side.)

**Why the guard must come before the file writes, not after.** The hook installs the leaf and key into the
strongswan x509 directory and runs `swanctl --load-creds` BEFORE it verifies the loaded name. On the live JHB
host (verified 2026-10-24) the hook carried two lineages that renewed 48 seconds apart on 2026-09-03 (adminer
21:52:00, myvnp 21:52:48) — the x509 files carried the later mtime, so the correct certificate won by
ordering, not by design. In the gap between the two renewals charon was holding the wrong certificate as its
IKEv2 server identity. So the guard must `exit 0` BEFORE any file is written; a guard placed after the reload
would detect the wrong cert but only after loading it.

**The renewal order IS deterministic — do not treat it as luck.** Verified 2026-10-24 14:20 UTC on
`vpn-prod-01`: certbot 4.0.0 `_internal/storage.py:52 renewal_conf_files()` does `glob.glob()` then
`result.sort()` — **alphabetical** — so `adminer.databyte.co.za.conf` is always processed BEFORE
`myvpn.databyte.co.za.conf`, in every run, across all 12 letsencrypt logs on the host. myvnp is therefore always
the **last writer**, and the steady-state IKEv2 identity is correct. The earlier "ordering is not guaranteed"
wording was wrong and is superseded.

The real exposure was therefore the **~48-second window between the two hook runs** (Sep 3: adminer 21:52:00,
myvnp 21:52:48, read directly from `letsencrypt.log.7.gz`), not a whole ~60-day-cycle flip. Within that window
charon held `CN=adminer.databyte.co.za` (SAN: adminer only) as the IKEv2 server identity while every client pins
`$RemoteId = myvpn.databyte.co.za` — a new IKE_AUTH in that window fails certificate validation. On JHB that
window is now **closed**, because the adminer lineage has no hook at all.

**⇒ JHB: DONE 2026-10-24 14:24 UTC (option B, per-lineage `renew_hook`). No JHB hook deadline remains.**
For a new site host, either mechanism above is acceptable; the guarded repo copy is what the runbook installs.

**Verify the guard is armed on a live host before its next renewal** (paths per the per-lineage
mechanism — the hook is NOT in `/etc/letsencrypt/renewal-hooks/deploy/`):

```bash
ssh root@<SITE_HOST> 'grep -nE "EXPECTED_LINEAGE_NAME|SITE_FQDN" /usr/local/sbin/databyte-strongswan-deploy-hook.sh
cat /etc/default/site-cert-watch
grep -H renew_hook /etc/letsencrypt/renewal/<SITE_FQDN>.conf
ls -A /etc/letsencrypt/renewal-hooks/deploy/'
# Expected: the guard block present; SITE_FQDN set to THIS host's lineage;
#           renew_hook bound on THIS host's lineage conf; global deploy dir EMPTY
```

And replace the hard-coded verification name:

```bash
# was: if ! echo "$loaded" | grep -q "myvpn.databyte.co.za"; then
if ! echo "$loaded" | grep -q "<SITE_FQDN>"; then
  log_error "new LE cert NOT loaded - list-certs does not contain <SITE_FQDN>"
```

Verify the guard is in place **before** the first issuance, and confirm it fires as intended:

```bash
ssh root@<SITE_HOST> 'grep -nE "EXPECTED_LINEAGE_NAME|RENEWED_LINEAGE_NAME|SITE_FQDN|grep -q" /usr/local/sbin/databyte-strongswan-deploy-hook.sh'
# Expected: the guard block is present, before the file writes, and the verification uses $SITE_FQDN
```

---

## 4. Issuance — operator-executed

### 4.1 Dry-run against LE staging

Staging validates the token, the DNS zone and the certbot config **without** consuming an issuance from the
5-per-week duplicate-certificate limit.

```bash
ssh root@<SITE_HOST> 'certbot certonly --dry-run \
  --dns-cloudflare \
  --dns-cloudflare-credentials /root/.cloudflare.ini \
  --cert-name <SITE_FQDN> \
  -d "<SITE_FQDN>"'
```

Expect `The dry run was successful.`

> **Note:** the dns-cloudflare lineage performs a **live DNS-01 challenge** against the production Cloudflare
> zone during the dry run — a transient TXT record that certbot cleans up. No production certificate is
> created and no production rate limit is consumed.

### 4.2 Real issuance

```bash
ssh root@<SITE_HOST> 'certbot certonly \
  --dns-cloudflare \
  --dns-cloudflare-credentials /root/.cloudflare.ini \
  --cert-name <SITE_FQDN> \
  -d "<SITE_FQDN>"'
```

Expected outcome:

- `/etc/letsencrypt/live/<SITE_FQDN>/` created with `cert.pem`, `chain.pem`, `fullchain.pem`, `privkey.pem`
- `/etc/letsencrypt/renewal/<SITE_FQDN>.conf` created, `authenticator = dns-cloudflare`, `server = https://acme-v02.api.letsencrypt.org/directory`, `key_type = ecdsa`
- **One SAN only:** `DNS:<SITE_FQDN>`

### 4.3 Verify the new lineage

```bash
ssh root@<SITE_HOST> 'openssl x509 -in /etc/letsencrypt/live/<SITE_FQDN>/cert.pem -noout -dates -ext subjectAltName
# Expected: notAfter ~90 days out; SAN: DNS:<SITE_FQDN>   (exactly one name)
stat -c "%n %a" /etc/letsencrypt/live/<SITE_FQDN>/privkey.pem /etc/letsencrypt/archive/<SITE_FQDN>/privkey*.pem
# Expected: 600 root on the real key file
```

---

## 5. Install it as the site's IKEv2 server identity

This is the step that makes the site actually work, and it is the step with customer impact.

```bash
ssh root@<SITE_HOST> '
SITE_FQDN="<SITE_FQDN>"
SWANCTL_DIR="/opt/strongswan-vpn-gateway/docker/swanctl"

# 1. leaf -> x509/server.crt.pem AND x509/server.pem (swanctl loads certs = server.pem)
install -m 0644 /etc/letsencrypt/live/$SITE_FQDN/cert.pem "$SWANCTL_DIR/x509/server.crt.pem"
install -m 0644 /etc/letsencrypt/live/$SITE_FQDN/cert.pem "$SWANCTL_DIR/x509/server.pem"

# 2. key -> private/server-key.pem (mode 0600)
install -m 0600 /etc/letsencrypt/live/$SITE_FQDN/privkey.pem "$SWANCTL_DIR/private/server-key.pem"

# 3. LE chain -> x509ca/le-NN-<short-CN>.pem, one cert per file
#    (strongSwan loads only ONE certificate per file - strongswan/strongswan#3072)
awk "/-----BEGIN CERTIFICATE-----/{n++; out=sprintf(\"/tmp/le-cert-%02d.pem\", n)} {print >> out} /-----END CERTIFICATE-----/{close(out)}" \
  /etc/letsencrypt/live/$SITE_FQDN/fullchain.pem
for f in /tmp/le-cert-*.pem; do
  cn=$(openssl x509 -in "$f" -noout -subject | sed "s/.*CN *= *//")
  install -m 0644 "$f" "$SWANCTL_DIR/x509ca/le-$(echo "$cn" | tr -cd "[:alnum:]" | cut -c1-12).pem"
done
rm -f /tmp/le-cert-*.pem

# 4. reload charon WITHOUT dropping established IKE SAs
docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --load-creds

# 5. verify the site identity is loaded
docker exec strongswan swanctl --uri=tcp://127.0.0.1:4502 --list-certs --type x509
# Expected: CN=<SITE_FQDN>, issuer Let's Encrypt, "has private key"
'
```

**Set the site identity in swanctl** — the local ID must equal the certificate's name, or the client-side
identity check fails before authentication:

```bash
ssh root@<SITE_HOST> 'grep -nE "certs =|id +=" /opt/strongswan-vpn-gateway/docker/swanctl/conf.d/rw-eap.conf
# Expected: certs = server.pem ; id = "<SITE_FQDN>"'
```

**The client trusts the site CA, not the LE chain.** The CA bundle the customer installs is served by nginx
from the site's own strongswan directory:

```
/certs/  →  alias /opt/strongswan-vpn-gateway/docker/swanctl/x509ca/;
```

so a per-site CA is served automatically — but the **URL the bot and the setup script quote must be the
site's own FQDN**, not a fixed name. This is part of the `tkt-041-7-config-parameterisation` work:
`bot.py` (6 sites), `build_installer.py` (4), `installer_tokens.py` (2), `setup-databyte-vpn.ps1` (3).

---

## 6. Point nginx at the new certificate

**Edit the file nginx actually reads** — `sites-enabled/`, and on the JHB host that file is a regular file
that has drifted from `sites-available/` (see §2). Do not edit `sites-available/` and assume it took effect.

```bash
ssh root@<SITE_HOST> 'grep -nE "server_name|ssl_certificate" /etc/nginx/sites-enabled/<vhost>'
```

Change the two lines to the new lineage, and narrow `server_name` to the names this host actually serves:

```
ssl_certificate     /etc/letsencrypt/live/<SITE_FQDN>/fullchain.pem;
ssl_certificate_key /etc/letsencrypt/live/<SITE_FQDN>/privkey.pem;
```

Then:

```bash
ssh root@<SITE_HOST> 'nginx -t && systemctl reload nginx'
# Expected: syntax is ok / test is successful
```

**Rollback for this step is one line:** restore the previous two paths and `systemctl reload nginx`.

---

## 7. Verify the served chain

```bash
ssh root@<SITE_HOST> 'openssl s_client -connect <SITE_FQDN>:443 -servername <SITE_FQDN> </dev/null 2>/dev/null \
  | openssl x509 -noout -dates -ext subjectAltName'
# Expected: SAN: DNS:<SITE_FQDN>  (one name), notAfter ~90 days out

curl -sI https://<SITE_FQDN>/api/health | head -3
# Expected: HTTP 200 and the portal health payload
```

External check, from a different network (not the site host):

```bash
dig +short <SITE_FQDN> A          # must be <SITE_IP>, unproxied
curl -sI https://<SITE_FQDN>/ | head -1
```

Then confirm the renewal path is armed for the new lineage:

```bash
ssh root@<SITE_HOST> 'certbot renew --dry-run 2>&1 | tail -8'
# Expected: the new lineage simulates successfully; the guard skips any other lineage
```

**Then install the per-site watcher** — it watches this site's cert AND activates auto-renewal
(`host/cert-monitor/site-cert-watch.sh`, `.service`, `.timer`):

```bash
ssh root@<SITE_HOST> 'install -m 0755 site-cert-watch.sh /usr/local/bin/
install -m 0644 site-cert-watch.service /etc/systemd/system/
install -m 0644 site-cert-watch.timer /etc/systemd/system/
printf "SITE_FQDN=<SITE_FQDN>\n" > /etc/default/site-cert-watch
systemctl daemon-reload
systemctl enable --now site-cert-watch.timer'
```

Confirm it sees a healthy site — `CHECK_ONLY=1` never enables a timer and never renews, it only reports:

```bash
ssh root@<SITE_HOST> 'SITE_FQDN=<SITE_FQDN> CHECK_ONLY=1 /usr/local/bin/site-cert-watch.sh; echo "exit=$?"'
# Expected: LE cert days remaining, strongswan CA days remaining, renewal config present,
#           certbot.timer enabled and active, served cert matches this lineage, exit 0
```

Leave `CHECK_ONLY` off in the timer so the watcher can actually activate auto-renewal.

---

## 8. Rollback

| Stage | Rollback |
|---|---|
| Dry-run | Nothing to roll back — no certificate, no lineage, no rate-limit consumption. Only a transient DNS TXT record, auto-cleaned. |
| After issuance, before nginx/strongswan | `certbot revoke --cert-name <SITE_FQDN>` then `certbot delete --cert-name <SITE_FQDN>`. The site keeps serving its previous certificate. Revoked certificates still count against the weekly limit — a revocation is not a free undo. |
| After nginx swap | Restore the two previous `ssl_certificate` lines in `sites-enabled/<vhost>`, `nginx -t`, `systemctl reload nginx`. |
| After charon reload | `git -C /opt/strongswan-vpn-gateway checkout docker/swanctl/x509/server.crt.pem` then `docker restart strongswan` — forces a fresh load of the previous certificate. Established IKE SAs are lost; customers reconnect. |

**Never delete the previous lineage while it is still being served.** Sequence is: point everything at the new
certificate, verify, and only then retire the old one.

---

## 9. Per-site checklist for the operator

- [ ] Site host provisioned from `VPS-XNEELO-DEPLOY.md` (manual — Ansible is out of scope)
- [ ] Site's own strongswan CA generated (`scripts/gen-certs.sh`, `SERVER_ID=<SITE_FQDN>`)
- [ ] Cloudflare API token created for the site and placed at `/root/.cloudflare.ini` mode 0600
- [ ] DNS A record `<SITE_FQDN>` → `<SITE_IP>`, grey-cloud, TTL auto; no wildcard record
- [ ] Per-host lineage guard installed in the deploy hook (§3), verified by grep
- [ ] `rw-eap.conf` `local.id` set to `<SITE_FQDN>`
- [ ] Site's own Telegram bot created (BotFather) — token + webhook secret on that host
- [ ] LE staging dry-run passes (§4.1)
- [ ] Real issuance done (§4.2), SAN list verified as exactly one name
- [ ] Certificate installed as the IKEv2 server identity, charon reloaded and verified (§5)
- [ ] nginx pointed at the new lineage in `sites-enabled/`, `server_name` narrowed to the site's own names (§6)
- [ ] Served chain + portal health verified externally (§7)
- [ ] `certbot renew --dry-run` passes with the guard in place
- [ ] `/etc/default/site-cert-watch` created with `SITE_FQDN=<SITE_FQDN>`
- [ ] `site-cert-watch` installed and enabled; a `CHECK_ONLY=1` run reports exit 0
- [ ] Backup paths and Grafana dashboard carry the site dimension (`<SITE_ID>` token)

---

## 10. Open items

1. **Filename.** Renamed to `DRAFT-PER-HOST-CERT.md` on 2026-10-24 on Zun's instruction (`git mv`, staged,
   not committed). The former name `DRAFT-WILDCARD-CERT.md` is withdrawn with the wildcard scope.
2. **Duplicate copies.** Two stale copies of the former wildcard text exist in other repos
   (`projects/databyte-ikev2/docs/`, `databyte-ikev2-doc-redraft/docs/`). They were deliberately **not**
   deleted — pruning them is an operator decision.
3. **Site-name hardcodes.** §5 notes where the site's own FQDN must replace a fixed name in the portal,
   bot, installer and setup script. That is tracked as `tkt-041-7-config-parameterisation`; until it is done,
   a new site's bot and installer will still quote the JHB name.

---

## 11. References

- [Let's Encrypt DNS-01 challenge](https://letsencrypt.org/docs/challenge-types/#dns-01-challenge)
- [certbot DNS-01 with Cloudflare](https://eff-certbot.readthedocs.io/en/stable/using.html#dns-plugins)
- [RFC 9525 §7.1 — wildcard certificates vouch single-label hostnames only](https://www.rfc-editor.org/rfc/rfc9525.html)
- [RFC 1035 §2.3.1 — DNS label syntax](https://www.rfc-editor.org/rfc/rfc1035.html)
- [UNECE UN/LOCODE](https://unece.org/trade/uncefact/unlocode) — site place codes
- [strongswan issue #3072 — one certificate per file in x509ca](https://github.com/strongswan/strongswan/issues/3072)

---

## 12. Change log

| Date (SAST) | Rev | Change | Author |
|---|---|---|---|
| 2026-10-15 | v0.1.0 | Initial wildcard draft (`*.myvpn.databyte.co.za`) — MR-1 | Misha |
| 2026-10-23 | v0.2.0 | "MR-1 executed, wildcard cert live" — **REVERTED** same day (unauthorised issuance, revoked + deleted) | Misha |
| 2026-10-24 | v0.2.0 | Re-purposed as the per-host per-site procedure: wildcard scope withdrawn, per-site consumer map + lineage guard + per-site steps added. DRAFT, not executed. | Misha |
| 2026-10-24 | v0.2.0 | Renamed `DRAFT-WILDCARD-CERT.md` -> `DRAFT-PER-HOST-CERT.md` on Zun's instruction. Lineage paths corrected to `<SITE_FQDN>` (were mixed with `<SITE_ID>`). DRAFT, not executed, not committed. | Misha |
