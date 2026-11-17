# inheco-tec-api

An HTTPS REST API for Inheco TEC temperature controllers (CPAC slots) over USB HID, written in
Python with FastAPI. It runs as a Windows service on the PC that the controller is attached to,
and it is the API half of a pair: the n8n community node
[`n8n-nodes-inheco-tec`](https://github.com/jamie-zaikov/n8n-nodes-inheco-tec) calls it from n8n
workflows.

The first sections below give the overview (purpose, hardware, install from source,
configuration, network, endpoints, troubleshooting, safety). The numbered sections 1 to 7 are the
operator runbook for a Windows host and the hardware-verification checklist. The placeholder
values that this README uses are listed in [Placeholders](#placeholders).

## Purpose

The `inheco-tec` FastAPI service (`inheco-tec-api`, import package `inheco_tec_api`, console entry
`inheco-tec-api` → `inheco_tec_api.entry:main`), built on `instrument-api-core`, a small service
base that ships inside this package (see [Vendored service base](#vendored-service-base)). It runs
as a Windows service on the API host, the lab PC that the TEC controller is attached to (192.0.2.10
in the examples), terminates TLS itself on TCP **8743**, and is the sole owner of the Inheco TEC
controller(s) over USB HID. It is a host-resident api: it needs no cloud deployment, and no
Terraform or other infrastructure code (no `infra/` folder) belongs to it (FR-1.5).

This file is the operator runbook and the release gate (design C17, DD-13). It holds no secret
value: the bearer token is named by its env var `INHECO_TEC_API_TOKEN`, the TLS key and the token
file by path, and the CA and leaf private keys by their Secret Manager secret names only (NFR-1).

The seven runbook sections:

1. [Status banner](#1-status-banner)
2. [TLS certificate provisioning](#2-tls-certificate-provisioning)
3. [Python and venv install](#3-python-and-venv-install)
4. [Bearer token](#4-bearer-token)
5. [Windows service](#5-windows-service)
6. [Running a legacy VENUS method (release and reacquire)](#6-running-a-legacy-venus-method-release-and-reacquire)
7. [Hardware-verification checklist](#7-hardware-verification-checklist)

### Vendored service base

The shared Python service base `instrument-api-core` (C11, FR-5.1) ships inside this distribution
as the private sub-package `inheco_tec_api._vendor.instrument_api_core`; it is not installed as a
separate package. Its tests run with the API tests under `tests/vendor_core/`.

It exposes the documented base surface for an owned instrument API, in one service style —
structlog, constant-time secret compare, fail-closed startup, `/healthz` unauthenticated, and no
host/port in any response body.

#### Surface of the vendored base

| Module | Purpose |
|--------|---------|
| `health` | The unauthenticated `/healthz` probe contract (`HEALTH_PATH`, `health_payload`). |
| `auth` | Fail-closed, constant-time bearer-token base; the expected token is read by env-var **name** (`load_expected_token`), the value never appears in code, logs, or a body. |
| `config` | Fail-closed config base (`require_env`, `ServiceConfig`); secrets are read by name at point of use, never stored on the config object. |
| `logging` | The structured per-request logging contract (`method` / `path` / `status_class` only). |
| `transport` | The TCP/protobuf transport base (`TcpProtobufTransport`): ephemeral socket per request, length-prefix framing helpers, abstract protobuf `frame`/`parse` seams. |
| `port` | The Cloud Run port contract: `CONTAINER_PORT = 8080` and `resolve_port()`. |

#### Dependency direction of the vendored base

The base imports **nothing** back — not `n8n-node-core` and no consumer. This API imports the
base; the paired n8n node never imports it (a node reaches its API only over HTTP).

#### Standard-library base modules

The base modules are dependency-free (standard library only), so they import, type-check, and test
cleanly on their own. The FastAPI / structlog / protobuf integration points were optional
dependencies of the original base package (`.[api]`, `.[transport]`); here this package installs
FastAPI and structlog itself, so no extra is needed. Each module documents its integration point
for the concrete API that extends the base.

## Supported hardware

- **Controller:** Inheco TEC controller on USB HID (vendor id / product id `VID_03EB&PID_2023`),
  addressed as controller `0`, with up to two CPAC slots.
- **Slots:** the CPAC article numbers of the manifest key `article_numbers`: 7000179 (CPAC
  Microplate), 7000190 and 7000193 (CPAC Ultraflat), 7000163 (CPAC Microplate HT 2TEC), 7000165
  and 7000166 (CPAC Ultraflat HT 2TEC). The slot limits come from the device and the manifest
  (`min_c`, `max_c`, `at_target_tolerance_c`).
- **Verified on hardware so far:** CPAC Ultraflat article 7000190 (4–70 °C) on slots 1 and 2; see
  checklist items 11, 12 and 16 in section 7. Several protocol details are still INFERRED (section 1).
- **Host:** Windows x64 with CPython 3.11 (section 3.3); CI tests the package on Python 3.11 and
  3.12. The `hid` module comes from the PyPI package `hidapi` (cython-hidapi).
- **Legacy software:** a Hamilton VENUS method (`HSLInhecoTECLib`) can use the same controller
  after a Release (section 6).

## Install from source

For development and tests (no hardware needed; the tests use a fake transport):

```bash
git clone https://github.com/jamie-zaikov/inheco-tec-api.git
cd inheco-tec-api
python -m venv .venv
. .venv/bin/activate
python -m pip install -c constraints.txt -e ".[dev]"
ruff check .
mypy src/inheco_tec_api
pytest
```

`constraints.txt` pins every package to the exact version that CI installs. The plain install
`python -m pip install -c constraints.txt .` gives the `inheco-tec-api` console script. On the
Windows host, follow section 3 (non-editable install into a locked venv) instead.

## Configuration

The service reads two inputs: the manifest `instrument.yaml` and its environment.

**Manifest.** `instrument.yaml` holds the executable facts of one installation and is loaded and
validated fail-closed by `inheco_tec_api.manifest.load_manifest` at startup (exit code 2 on an
error, section 5.8). The env var `INHECO_TEC_MANIFEST` names its path (the WinSW XML sets it,
section 5). The keys:

| Key | Meaning |
|---|---|
| `host`, `port` | The API host address and the HTTPS port (8743). |
| `auth.bearer_token_ref` | The name of the env var that holds the bearer token (section 4). |
| `tls.cert_file`, `tls.key_file` | The server certificate and key files (section 2.3). |
| `service.bind` | The listen address. |
| `service.env_file` | The optional `service.env` file that the service loads at startup (section 4). |
| `service.state_file` | The persisted release state (section 6). |
| `service.shutdown_timeout_s` | The shutdown stop deadline (section 5.9). |
| `transport.kind` | `hidapi` (the device) or `fake` (no device; tests only). |
| `transport.pre_send_delay_ms`, `transport.read_timeout_ms`, `transport.retry_delay_ms`, `transport.max_attempts`, `transport.max_stale_reads`, `transport.lock_wait_s` | The transport timing and retry settings; the comments in `instrument.yaml` give each floor and range. |
| `article_numbers` | The CPAC article numbers that the service accepts (quoted 7-digit strings). |
| `controllers` | The controllers and their slots, each slot with `class`, `min_c`, `max_c` and `at_target_tolerance_c`. |
| `capabilities` | The operations that the node may offer. |

Set `host` to the address of your API host and the TLS paths to your data root before the first
start (section 3.5). Change the manifest only through a reviewed commit and a new copy.

**Environment.** `service.env.template` lists every variable that the service reads, each with an
empty value and one comment line: `INHECO_TEC_API_TOKEN` (required, the bearer token), `LOG_LEVEL`
(default `INFO`), `INHECO_TEC_MANIFEST` (the manifest path) and `INHECO_TEC_TRANSPORT` (must stay
unset on a host with hardware, section 3.5). On the host, the token goes into the locked
`service.env` file of section 4, which holds that one line only. Never commit a filled-in copy.

## Network requirements

- **Inbound:** HTTPS on TCP 8743 from the n8n host to the API host. Ask the network administrator
  to allow TCP 8743 from the n8n host (or its subnet, 198.51.100.0/24 in the examples) to the API
  host, then create the local host firewall rule of section 5.5.
- **Port:** TCP 8743 must be free on the host (section 5.5). The service has no plain-HTTP listener.
- **Name:** the API is addressed by IP; the certificate carries the IP as its only SAN (section 2).
- **Outbound:** none at run time. The `/docs` page assets ship in the package, so no browser loads
  a script from the internet (section 5.10).
- **Device:** the TEC controller is attached to the host by USB.
- **Check:** run the reachability check of section 5.6 from the n8n instance after the first start.

## API endpoints and /docs

Every `/v1` route needs the bearer token of section 4 in the `Authorization` header. `{cid}` is a
controller id and `{sid}` a slot id of the manifest.

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Health probe; no token, no device I/O. |
| GET | `/v1/status` | Controllers, slots, states and effective limits. |
| POST | `/v1/controllers/{cid}/connect` | Connect (or re-initialize) a controller. |
| POST | `/v1/controllers/{cid}/release` | Release the device for VENUS (section 6). |
| POST | `/v1/controllers/{cid}/reacquire` | Take the device back after a release (section 6). |
| POST | `/v1/controllers/{cid}/emergency-off` | Emergency Off; needs the body `{"confirm": true}` (section 6.1). |
| GET | `/v1/controllers/{cid}/device-type` | Device type of a slot (query `slot`). |
| GET | `/v1/controllers/{cid}/firmware-version` | Firmware version (query `selector`). |
| GET | `/v1/controllers/{cid}/sensor-values` | Raw sensor values (query `selector`). |
| GET, PUT | `/v1/controllers/{cid}/slots/{sid}/target` | Read or set the target temperature. |
| POST | `/v1/controllers/{cid}/slots/{sid}/start` | Start temperature control. |
| POST | `/v1/controllers/{cid}/slots/{sid}/stop` | Stop temperature control. |
| GET | `/v1/controllers/{cid}/slots/{sid}/temperature` | Actual temperature (query `selector`). |
| GET | `/v1/controllers/{cid}/slots/{sid}/at-target` | Whether the slot is at target (query `tolerance_c`). |
| POST | `/v1/stop-all` | Stop every configured slot. |

`/docs` serves an interactive Swagger UI page without a token, and `/openapi.json` (token
required) gives the full API description; see section 5.10. `contract/api-routes.json` is the
route list that the API tests and the paired node tests check.

## Troubleshooting

- **The service does not start:** read the exit code and the one log line of section 5.8 (manifest,
  token, `service.env`, port, TLS pair), then the logs of section 5.7.
- **A call returns 503 `controller_busy`, 409 `stop_not_delivered` or "outcome UNKNOWN":** act as
  section 6.1 says; a stop may not have reached the device.
- **Set target and start return 409 `emergency_off_active`:** follow the Emergency Off recovery of
  section 5.11 (a power cycle is needed).
- **n8n cannot reach the API while the host health check passes:** see the reachability check in
  section 5.6.
- **The browser shows a certificate warning on `/docs`:** see section 5.10, step 5.
- **After a planned stop, a slot may still be on:** see section 5.9.

## Safety and recovery

The safety procedures are part of the runbook: the stop behaviour and stop budget (section 5.9),
the Emergency Off recovery (section 5.11), the hand-over to a legacy VENUS method (section 6) and
the safety responses that the operator must act on (section 6.1). Read them before the first use on
hardware.

## 1. Status banner

> **NOT RELEASED FOR PRODUCTION USE ON HARDWARE — 3 of 18 checklist items verified**

The API is not released for production use on hardware while any item of the section 7 checklist
is unverified (FR-16.4). The operator updates the count in the banner each time an item is verified.
Only when all 18 items are verified may the banner change to "Released for production use on
hardware" with the date and the verifier. The count records the reference deployment that this
project comes from; on a new installation, run the checklist again on your own hardware.

Several protocol details are INFERRED (not yet confirmed on hardware). Code that depends on one
carries the marker `INFERRED — see api/README.md checklist item N` (that is, item N of section 7
of this README). The cited items are 1, 2, 3, 4, 5, 6, 8, 9, 12 and 13 (see the "Code reference"
note in each row of section 7). Verify the read-only items (1–9) with read-only commands before any
command that changes a slot (`STT`, `ATE`).

## 2. TLS certificate provisioning

The service serves HTTPS only; there is no plain-HTTP listener (FR-3.2). The certificate comes
from a private CA that you own, named `<ca-name>` in this runbook (DD-4, P1). The n8n host must
trust that CA: put the public CA certificate into the n8n image (or onto the n8n host) and set
`NODE_EXTRA_CA_CERTS` to its path. The `caCertificate` field of the `inhecoTecApi` credential may
stay empty. If it is filled, it must hold this same CA, because a filled field replaces the image
trust for that credential. No public DNS zone is needed. The API is addressed by IP only
(`192.0.2.10`): the leaf certificate carries no DNS name (design C17, Risk R10). If your
organization later supplies an enterprise CA, its root certificate replaces the CA certificate in
the n8n image through a new image build (section 2.5).

Rules:

- Do all key work in **GCP Cloud Shell** (project `<gcp-project-id>`) with `openssl`, or on an
  operator workstation with OpenSSL 3.x. Never do key work on the lab host and never in a git
  checkout. Use an empty working folder that only you can read (`umask 077`), and delete it at the
  end. The examples keep the keys in Google Cloud Secret Manager; another secret store works the
  same way if it reads and writes a secret from and to a file.
- The CA private key **and** the leaf (server) private key live in Secret Manager (project
  `<gcp-project-id>`). The operator creates both secrets and their versions by hand, not
  Terraform: a key that Terraform creates or holds lands in Terraform state in plaintext, and this
  api needs no infrastructure code (no `infra/` folder, FR-1.5). This runbook names the keys by
  secret name only: `<CA_KEY_SECRET_NAME>` (record the real name here at creation: `________`) and
  `<LEAF_KEY_SECRET_NAME>` (record the real name here at creation: `________`). Never print a key,
  never `cat` it, never commit it. The CA key never reaches the API host.
- The CA certificate (`ca.crt`) is public data. The server key (`server.key`) is a secret.
- The service refuses a password-protected server key (exit code 4, section 5): write the key
  without a passphrase and protect it with the folder ACL instead.

### 2.1 Create the root CA (once)

In Cloud Shell, in an empty working folder. ECDSA P-256, 10-year validity:

```bash
umask 077
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out ca.key
openssl req -x509 -new -key ca.key -sha256 -days 3650 \
  -subj "/CN=<ca-name>" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -out ca.crt
```

Store the CA key in Secret Manager from the file (the value never goes on a command line or to the
screen), then delete the local copy:

```bash
gcloud secrets create <CA_KEY_SECRET_NAME> --project <gcp-project-id> \
  --replication-policy automatic --data-file ca.key
shred -u ca.key
```

Keep `ca.crt` (public) with the release records. Set a calendar reminder 30 days before the CA
expiry (10 years).

### 2.2 Issue the server (leaf) certificate

In Cloud Shell. ECDSA P-256, `subjectAltName = IP:192.0.2.10` only (no DNS name), EKU
serverAuth, 397-day validity.

Create `leaf.ext`:

```text
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = IP:192.0.2.10
```

Fetch the CA key into the working folder only for the signing step (to a file, never to the
screen), sign, and delete it:

```bash
gcloud secrets versions access latest --secret <CA_KEY_SECRET_NAME> \
  --project <gcp-project-id> --out-file ca.key
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out server.key
openssl req -new -key server.key -subj "/CN=192.0.2.10" -out server.csr
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -days 397 -sha256 -extfile leaf.ext -out server.crt
shred -u ca.key
rm server.csr
```

Check the result (public data only):

```bash
openssl verify -CAfile ca.crt server.crt
openssl x509 -in server.crt -noout -subject -enddate -ext subjectAltName,extendedKeyUsage
```

Store the leaf key in Secret Manager from the file, so that it can be fetched again later (for
example to reinstall the same pair):

```bash
gcloud secrets create <LEAF_KEY_SECRET_NAME> --project <gcp-project-id> \
  --replication-policy automatic --data-file server.key
```

Keep `server.key` in the working folder only until the download of section 2.3 is done, then
delete the local copy in Cloud Shell (`shred -u server.key`). After that the only copies of the
key are the Secret Manager version and the file in `tls\` on the API host.

### 2.3 Install on the API host

Install path (from the manifest `tls.cert_file` / `tls.key_file`), plus the public CA certificate
for the section 5.6 health check:

- `C:\ProgramData\instrument-apis\inheco-tec\tls\server.crt`
- `C:\ProgramData\instrument-apis\inheco-tec\tls\server.key`
- `C:\ProgramData\instrument-apis\inheco-tec\tls\ca.crt` (public)

The API host has no `openssl`, so all key work stays in Cloud Shell. The files come from Cloud
Shell with the steps below. If the Cloud Shell browser download fails, use the Cloud SDK (`gcloud`)
on the API host instead of step 2: in the elevated session, write each file straight into the
locked `tls\` with `gcloud secrets versions access <version> --secret <LEAF_KEY_SECRET_NAME>
--project <gcp-project-id> --out-file C:\ProgramData\instrument-apis\inheco-tec\tls\server.key` (the same
for the leaf certificate secret to `server.crt`; `ca.crt` is public). The key never appears on
screen, and no download copy exists to delete.

1. The `tls\` folder must already exist with its locked ACL (section 3.2) **before** you put the
   files in it: Administrators and SYSTEM full control, the service account
   `NT SERVICE\inheco-tec-api` read (section 5.4), no access for Users, inheritance off.
2. In the operator session on the API host, download `server.crt`, `server.key` and `ca.crt` from
   Cloud Shell (for example `cloudshell download server.key` in the Cloud Shell browser window).
   Then delete the local key copy in Cloud Shell (`shred -u server.key`, section 2.2).
3. **Copy** (do not move) the three downloaded files into `tls\`, so that each new file takes the
   locked ACL of `tls\` (a moved file keeps the ACL of the download folder, which gives your user
   full control):

   ```powershell
   $dl  = Join-Path $env:USERPROFILE 'Downloads'
   $tls = 'C:\ProgramData\instrument-apis\inheco-tec\tls'
   Copy-Item -LiteralPath "$dl\server.crt" -Destination "$tls\server.crt"
   Copy-Item -LiteralPath "$dl\server.key" -Destination "$tls\server.key"
   Copy-Item -LiteralPath "$dl\ca.crt" -Destination "$tls\ca.crt"
   icacls "$tls\server.key"
   ```

4. Confirm with `icacls` that the ACL keeps access limited to the service account and
   Administrators (plus SYSTEM), then delete the downloaded copies of the key and the leaf
   certificate:

   ```powershell
   Remove-Item -LiteralPath "$dl\server.key", "$dl\server.crt"
   Get-ChildItem -LiteralPath $dl -Filter *.key   # must list nothing
   ```

Keep the downloaded `ca.crt` only until it is in `tls\` (step 3), then delete it too. n8n does not
need it: n8n trusts the CA from its own image or host (section 2.4). The key is never in git and
never in the manifest, and the CA key never reaches the lab host.

### 2.4 Trust in n8n

n8n trusts `<ca-name>` from the n8n image: the image build copies the public CA certificate into
the image and sets `NODE_EXTRA_CA_CERTS` to its path (on an n8n host without an image, set the same
variable for the n8n process). In the n8n credential `inhecoTecApi`, set `baseUrl` =
`https://192.0.2.10:8743` and leave the `caCertificate` field empty. If the field is filled, it
must hold the PEM text of this same CA: a filled field replaces the image trust for this
credential, so a different CA in it fails the TLS check. Certificate verification stays on in every
case. See the README of the paired node repository (`<node-repo-root>/README.md`), "Credential
setup", for the other credential fields.

### 2.5 Renewal

1. Set a calendar reminder **30 days before** the leaf `notAfter` date (397-day validity).
2. Issue a new leaf with section 2.2 (same CA, same SAN `IP:192.0.2.10`). Add the new key as a
   new version of the existing leaf-key secret, not as a new secret:
   `gcloud secrets versions add <LEAF_KEY_SECRET_NAME> --project <gcp-project-id> --data-file server.key`.
3. Copy the new `server.crt` / `server.key` into `tls\` with section 2.3, replacing the old files,
   and delete the downloaded copies.
4. Restart the service (section 5). A bad pair stops the start with exit code 4.
5. No n8n change and no image change is needed: the CA is unchanged (Risk R7), and n8n trusts the
   CA, not the leaf. Set the next reminder.

**New CA.** A new CA (not a new leaf) needs a new n8n image. Replace the CA certificate file of the
image build with the new public CA certificate, update every test or check that pins the expected
CA values, then rebuild the n8n image and redeploy n8n. Until the new image serves, n8n does not
trust a leaf from the new CA.

**IP change** (Risk R10). The host is addressed by IP and no address reservation exists. If the IP
of the API host changes, the certificate SAN, the manifest `host`, the n8n `baseUrl` and the firewall
rule no longer match. n8n verifies the certificate, so a call to a changed address fails closed and
never reaches another host silently. To recover:

1. Re-issue the leaf with section 2.2 with the new `IP:` SAN (`subjectAltName = IP:<new IP>` and
   `-subj "/CN=<new IP>"`), store its key as a new version of `<LEAF_KEY_SECRET_NAME>`, and install
   it with section 2.3.
2. Update `host` in `instrument.yaml` (a reviewed commit, then a new copy, section 3.5).
3. Update the n8n credential `baseUrl` to `https://<new IP>:8743`.
4. Update the local firewall rule (section 5.5) if its scope changes.
5. Restart the service and run the reachability check (section 5.6).

If the CA key is ever exposed, create a new CA (section 2.1), reissue the leaf, and ship the new CA
certificate in a new n8n image (**New CA** above). A `caCertificate` field that is filled in an n8n
credential must then be cleared, or hold only the new CA.

## 3. Python and venv install

Fixed host paths (the WinSW XML and the manifest use them):

| What | Path |
|---|---|
| Install root (admin-only) | `C:\instrument-apis\inheco-tec` |
| Checkout of the release tag | `C:\instrument-apis\inheco-tec\src` |
| venv | `C:\instrument-apis\inheco-tec\venv` |
| Manifest used by the service | `C:\instrument-apis\inheco-tec\instrument.yaml` (set by `INHECO_TEC_MANIFEST` in the WinSW XML) |
| WinSW exe + XML | `C:\instrument-apis\inheco-tec\service` |
| Data root | `C:\ProgramData\instrument-apis\inheco-tec` (`tls\`, `secrets\`, `state\`, `logs\`) |

Run every step in an elevated PowerShell. In `icacls`, `*S-1-5-32-544` is Administrators and
`*S-1-5-18` is SYSTEM (SIDs, so the commands work on any Windows display language).

### 3.0 Guard functions (paste first)

Sections 3.1 and 3.2 **check before they change anything**. Each block stops (`throw`) at the first
failed check, before any `icacls` change runs on a path that the block did not create itself.
`icacls` acts on the target of a junction and `/T` recurses through it. So the blocks never run an
`icacls` change on an existing folder, never use `/T` for a change, and add `/L` (act on a link
itself, never on its target). Paste these two functions once into the same elevated PowerShell
session before 3.1 and 3.2 (without them, 3.1 and 3.2 stop at the first check):

```powershell
function global:Assert-NotReparsePoint([string]$Path) {
  $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
  if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "$Path is a junction or symbolic link (ReparsePoint). Stop and investigate."
  }
}

function global:Assert-AdminOnlyFolder([string]$Path) {
  # Not a link; owner is Administrators or SYSTEM; no other principal has an effective allow ACE
  # with Delete, DeleteSubdirectoriesAndFiles, ChangePermissions, TakeOwnership or FullControl.
  Assert-NotReparsePoint $Path
  $sid   = [Security.Principal.SecurityIdentifier]
  $admin = @('S-1-5-32-544', 'S-1-5-18')
  $acl   = Get-Acl -LiteralPath $Path -ErrorAction Stop
  $owner = $acl.GetOwner($sid).Value
  if ($owner -notin $admin) { throw "$Path is owned by $owner, not Administrators or SYSTEM. Stop." }
  $danger = 0x100D0040   # GENERIC_ALL | WRITE_OWNER | WRITE_DAC | DELETE | DELETE_CHILD
  foreach ($r in $acl.GetAccessRules($true, $true, $sid)) {
    if ($r.AccessControlType -ne 'Allow') { continue }
    if ($r.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) { continue }
    if ($r.IdentityReference.Value -in $admin) { continue }
    if (([long]$r.FileSystemRights -band $danger) -ne 0) {
      throw "$Path grants $($r.IdentityReference.Value) $($r.FileSystemRights). Stop."
    }
  }
}
```

### 3.1 Lock down the install root (before anything is put in it)

Anyone who can write under `C:\instrument-apis\inheco-tec` controls the service: the manifest names the
token file, the TLS paths, the bind address and `transport.kind`, and the venv and the checkout are
on the service's import path. A new folder at the `C:\` root inherits modify access for
Authenticated Users (enough to rename `C:\instrument-apis` and put a new tree in its place), so a new
`C:\instrument-apis` gets its inherited access removed before anything else is created in it. An existing
`C:\instrument-apis` (for example from another instrument API) is never re-owned or re-ACLed here. It is
only checked, and the block stops unless it is already admin-only. An existing
`C:\instrument-apis\inheco-tec` always stops the block.

```powershell
& {
  $ErrorActionPreference = 'Stop'
  $top  = 'C:\instrument-apis'
  $root = 'C:\instrument-apis\inheco-tec'

  # 1. Check before any change. Nothing below runs if a check throws.
  if (Test-Path -LiteralPath $root) { throw "$root already exists. Stop: see the text below." }
  $newTop = -not (Test-Path -LiteralPath $top)
  if (-not $newTop) { Assert-AdminOnlyFolder $top }   # existing C:\instrument-apis: check only, no change

  # 2. New C:\instrument-apis only: create it (no -Force, so New-Item fails if the path appeared
  #    meanwhile), then set owner and ACL on the folder itself (/L, no /T).
  if ($newTop) {
    New-Item -ItemType Directory -Path $top | Out-Null
    Assert-NotReparsePoint $top
    icacls $top /setowner '*S-1-5-32-544' /L
    if ($LASTEXITCODE -ne 0) { throw "icacls /setowner failed on $top" }
    icacls $top /inheritance:r /grant:r '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' /L
    if ($LASTEXITCODE -ne 0) { throw "icacls /grant failed on $top" }
    if (Get-ChildItem -LiteralPath $top -Force) { throw "$top changed while it was created. Stop." }
    Assert-AdminOnlyFolder $top
  }

  # 3. Create and lock the install root the same way, then check that nothing slipped in.
  New-Item -ItemType Directory -Path $root | Out-Null
  Assert-NotReparsePoint $root
  icacls $root /setowner '*S-1-5-32-544' /L
  if ($LASTEXITCODE -ne 0) { throw "icacls /setowner failed on $root" }
  icacls $root /inheritance:r /grant:r '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' /L
  if ($LASTEXITCODE -ne 0) { throw "icacls /grant failed on $root" }
  if (Get-ChildItem -LiteralPath $root -Force) { throw "$root changed while it was created. Stop." }
  Assert-AdminOnlyFolder $root

  icacls $top
  icacls $root
}
```

`icacls` must list only Administrators and SYSTEM on `$root` (and on `C:\instrument-apis` if the block
created it). **If a check stops the block**, do not delete, re-own or re-ACL the existing folder with
these commands, and never run `Remove-Item -Recurse` or `icacls ... /T` on a path that can be a
junction. Record the message and inspect the path read-only (`Get-Item -Force`, `Get-Acl`, `icacls`
without options). Decide with the platform owner and record the decision in the release checklist.
Run the block again only after an administrator has resolved the path. The service account gets
read and execute later (section 5.4). Everything you create below (checkout, venv,
`instrument.yaml`, `service\`) inherits this ACL.

### 3.2 Lock down the data folders (before the first start)

Lock `secrets\`, `tls\`, `state\` and `logs\` **before** the service starts for the first time.
Otherwise a local user can pre-create a folder, own it, plant a junction, or pre-plant a
`service.env` with their own token (the service loads it with `override=False` and uses it). The
parent `C:\ProgramData\instrument-apis` is checked too: a user who owns it, or who can delete in it, can
rename the locked `inheco-tec` folder and put their own tree in its place.

```powershell
& {
  $ErrorActionPreference = 'Stop'
  $parent = 'C:\ProgramData\instrument-apis'
  $data   = 'C:\ProgramData\instrument-apis\inheco-tec'

  # 1. Check before any change. Nothing below runs if a check throws.
  if (Test-Path -LiteralPath $data) { throw "$data already exists. Stop: see the text below." }
  $newParent = -not (Test-Path -LiteralPath $parent)
  # Existing parent: the owner must be Administrators or SYSTEM, and no non-admin ACE may grant
  # Delete, DeleteSubdirectoriesAndFiles or FullControl. Check only, no change.
  if (-not $newParent) { Assert-AdminOnlyFolder $parent }

  # 2. New parent only: create it and lock it like C:\instrument-apis (no -Force, /L, no /T).
  if ($newParent) {
    New-Item -ItemType Directory -Path $parent | Out-Null
    Assert-NotReparsePoint $parent
    icacls $parent /setowner '*S-1-5-32-544' /L
    if ($LASTEXITCODE -ne 0) { throw "icacls /setowner failed on $parent" }
    icacls $parent /inheritance:r /grant:r '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' /L
    if ($LASTEXITCODE -ne 0) { throw "icacls /grant failed on $parent" }
    if (Get-ChildItem -LiteralPath $parent -Force) { throw "$parent changed while it was created. Stop." }
    Assert-AdminOnlyFolder $parent
  }

  # 3. Create and lock the data root, then check that nothing slipped in before the lock.
  New-Item -ItemType Directory -Path $data | Out-Null
  Assert-NotReparsePoint $data
  icacls $data /setowner '*S-1-5-32-544' /L
  if ($LASTEXITCODE -ne 0) { throw "icacls /setowner failed on $data" }
  icacls $data /inheritance:r /grant:r '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' /L
  if ($LASTEXITCODE -ne 0) { throw "icacls /grant failed on $data" }
  if (Get-ChildItem -LiteralPath $data -Force) { throw "$data changed while it was created. Stop." }
  Assert-AdminOnlyFolder $data

  # 4. The four sub-folders are new and inherit the locked ACL of $data.
  foreach ($d in @("$data\tls", "$data\secrets", "$data\state", "$data\logs")) {
    New-Item -ItemType Directory -Path $d | Out-Null
    Assert-AdminOnlyFolder $d
  }

  icacls $data /T /L
}
```

`icacls` must list only Administrators and SYSTEM on `$data` and its four sub-folders. **If a check
stops the block** (for example `$data` already exists, perhaps with a `service.env` you did not put
there, or the parent fails its owner or ACL check), follow the same rule as in section 3.1. Change
nothing with these commands, inspect read-only, decide with the platform owner, record the
decision, and run the block again only after an administrator has resolved the path. Users get no
access. The service-account grants (read on `tls\` and `secrets\`, modify on `state\` and `logs\`)
are added in section 5.4, after the service account exists.

### 3.3 Install CPython 3.11 x64 for all users

Install CPython 3.11 (64-bit) from python.org **for all users** under `C:\Program Files\Python311`,
without changing PATH:

```powershell
.\python-3.11.<patch>-amd64.exe /quiet InstallAllUsers=1 PrependPath=0 Include_test=0
& 'C:\Program Files\Python311\python.exe' --version
```

Do not use a per-user install (under the user profile), the Microsoft Store Python, or the
repo-local development `.venv`. Check that no machine PATH entry is user-writable:

```powershell
[Environment]::GetEnvironmentVariable('Path', 'Machine') -split ';' | Where-Object { $_ } |
  ForEach-Object { $_; icacls $_ }
```

Every entry must grant write only to Administrators, SYSTEM or TrustedInstaller.

### 3.4 Check out the release tag and create the venv

Put the source of the release tag `<release-tag>` of `main` (git clone or the tag's source archive)
at `C:\instrument-apis\inheco-tec\src`. If you clone from a local repository path instead of the remote
(for example a branch that is not pushed), use `git clone --no-local`: a plain local clone
hard-links the object files, and a hard link keeps the ACL of the source repository instead of
taking the admin-only ACL of section 3.1. Then create the venv and install with the pinned versions
of `constraints.txt` (repo root, NFR-5). The host install is **non-editable**: one install of this
package (the vendored `instrument-api-core` base ships inside it, so no separate core install is
needed), then `pip check`:

```powershell
$src = 'C:\instrument-apis\inheco-tec\src'
$py  = 'C:\instrument-apis\inheco-tec\venv\Scripts\python.exe'
& 'C:\Program Files\Python311\python.exe' -m venv C:\instrument-apis\inheco-tec\venv
& $py -m pip install --upgrade pip
Set-Location $src
& $py -m pip install -c constraints.txt .
& $py -m pip check
& $py -I -c "import inheco_tec_api.entry, hid; print('import ok')"
```

`pip install -e` (editable) is for development and CI only, never on the host. A non-editable
install copies the package into the venv, so the checkout stays off the import path, and it ships
`docs_assets/` (the vendored Swagger UI files of the section 5.10 page) as package data (design
C1, DD-15). The `hid` module comes from the PyPI package `hidapi` (cython-hidapi), the only native
HID dependency.

**Upgrade.** Reinstall the same way from a fresh clone of `main` at the new release tag: replace
`C:\instrument-apis\inheco-tec\src` with the fresh clone (`git clone --no-local` if the source is a local
path), then run the same commands in the same order: `pip install -c constraints.txt .`, then
`pip check`.

**Restart after an install or upgrade.** The service loads the new code only after a restart. This
is a planned restart per section 5.9: run Stop All from n8n first and read the per-slot outcomes,
because a graceful stop sends `ATE0` to every configured slot of every connected controller. Then
restart the service (section 5.6) and check `/healthz`.

### 3.5 Install the manifest

```powershell
Copy-Item "$src\instrument.yaml" C:\instrument-apis\inheco-tec\instrument.yaml
```

The copy inherits the admin-only ACL. Change it only through a reviewed commit and a new copy.
`INHECO_TEC_TRANSPORT` must not be set in the service environment: it overrides `transport.kind`
and `fake` drives no hardware (the service then logs `transport_fake_active` and `/v1/status`
reports `transport_fake: true`).

## 4. Bearer token

The service reads the bearer token from the env var named by the manifest key
`auth.bearer_token_ref`: **`INHECO_TEC_API_TOKEN`**. At startup it loads
`C:\ProgramData\instrument-apis\inheco-tec\secrets\service.env` (manifest `service.env_file`) into its
environment without overriding a variable that is already set (DD-6). The token is never in the
manifest, the WinSW XML, the service registry key, git, a command line or a log.

Rules:

- `service.env` holds **one line only**: `INHECO_TEC_API_TOKEN=<value>`. Every other line would also
  enter the service environment (for example `LOG_LEVEL` or `INHECO_TEC_TRANSPORT`), so never add one.
- Generate the value in the team password manager: at least 32 random characters from
  `A–Z a–z 0–9 - _` only (a `#`, a space or a quote changes how the dotenv parser reads the line).
- The value is entered with `Read-Host -AsSecureString`, so it is never echoed or put in the shell
  history. Never `Get-Content` the file and never print the variable.
- The `secrets\` ACL is set in section 3.2 and section 5.4: Administrators and SYSTEM full control,
  the service account read, no access for Users.

Create the file (it is written as UTF-8 without a BOM; a BOM would hide the variable name and
the service would exit with code 3):

```powershell
$file = 'C:\ProgramData\instrument-apis\inheco-tec\secrets\service.env'
$secure = Read-Host -AsSecureString -Prompt 'Paste the INHECO_TEC_API_TOKEN value'
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
  $line = 'INHECO_TEC_API_TOKEN=' + [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
  [IO.File]::WriteAllText($file, $line + "`r`n", (New-Object Text.UTF8Encoding $false))
} finally {
  [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
  Remove-Variable line, secure -ErrorAction SilentlyContinue
}
```

Check the file without showing the value:

```powershell
(Get-Content $file | Measure-Object -Line).Lines                 # must be 1
Select-String -Path $file -Pattern '^INHECO_TEC_API_TOKEN=\S' -Quiet   # must be True
icacls $file
```

Put the same value into the `token` field of the n8n credential `inhecoTecApi` (paste it from the
password manager). To rotate: write a new value with the same steps, restart the service
(section 5), and update the n8n credential.

## 5. Windows service

The service wrapper is **WinSW v2.12.0** (DD-5). The definition is `service/inheco-tec-api.xml` of
this repository (`<api-repo-root>/service/inheco-tec-api.xml` in a checkout): service id
`inheco-tec-api`, executable
`C:\instrument-apis\inheco-tec\venv\Scripts\inheco-tec-api.exe`, working directory the venv, env
`INHECO_TEC_MANIFEST=C:\instrument-apis\inheco-tec\instrument.yaml` (plus `PYTHONSAFEPATH=1`,
`PYTHONNOUSERSITE=1` and an empty `PYTHONPATH`, so no user-writable folder is on `sys.path`),
start mode Automatic, restart on failure 3 × with a 10 s delay, failure count reset after 1 hour,
stop timeout 30 s, preshutdown 30 s, logs rolled by size under
`C:\ProgramData\instrument-apis\inheco-tec\logs`. It holds no secret; never add the token as an `env`
element of the XML (local users can read the XML and the service registry key, Risk R13).

### 5.1 Download and verify WinSW

1. Download the x64 executable of the WinSW **v2.12.0** release from
   `https://github.com/winsw/winsw/releases/tag/v2.12.0` (record the asset name: `________`).
2. Compute the SHA-256 and record it here at the first install. On a later install, the hash must
   match the recorded value; stop if it does not.

   ```powershell
   (Get-FileHash .\<winsw-asset>.exe -Algorithm SHA256).Hash
   Get-AuthenticodeSignature .\<winsw-asset>.exe | Format-List Status, SignerCertificate
   ```

   | Field | Value |
   |---|---|
   | WinSW version | v2.12.0 |
   | Asset name | `________` |
   | SHA-256 | `<WINSW_SHA256_TO_FILL_AT_INSTALL>` |
   | Authenticode status | `________` |
   | Recorded by / date | `________` |

3. The WinSW binary is not committed to git.

### 5.2 Place the files

```powershell
$svc = 'C:\instrument-apis\inheco-tec\service'
New-Item -ItemType Directory -Force -Path $svc | Out-Null
Copy-Item .\<winsw-asset>.exe "$svc\inheco-tec-api.exe"
Copy-Item "C:\instrument-apis\inheco-tec\src\service\inheco-tec-api.xml" "$svc\inheco-tec-api.xml"
```

WinSW reads the XML with the same base name next to its exe. Both files inherit the admin-only ACL
of section 3.1.

### 5.3 Install and set the service account (required)

```powershell
Set-Location C:\instrument-apis\inheco-tec\service
.\inheco-tec-api.exe install
sc.exe config inheco-tec-api obj= "NT SERVICE\inheco-tec-api"
sc.exe qc inheco-tec-api
```

The XML has no `serviceaccount` element, so WinSW installs the service as **LocalSystem**. The
`sc.exe config` step is required: it changes the account to the virtual account
`NT SERVICE\inheco-tec-api`. `sc.exe qc` must show
`SERVICE_START_NAME : NT SERVICE\inheco-tec-api`. Record the account actually shown in checklist
item 16.

Fallback: if the virtual account cannot open the HID device (checklist item 16), set
`sc.exe config inheco-tec-api obj= LocalSystem`, confirm with `sc.exe qc`, and record the change
and the reason in item 16. The SYSTEM grants of section 3 already cover LocalSystem.

### 5.4 Grant the service account (before the first start)

The virtual account exists after the install. Grant it only what it needs:

```powershell
$acct = 'NT SERVICE\inheco-tec-api'
$data = 'C:\ProgramData\instrument-apis\inheco-tec'
icacls C:\instrument-apis\inheco-tec /grant "${acct}:(OI)(CI)RX"
icacls "$data\tls"     /grant "${acct}:(OI)(CI)R"
icacls "$data\secrets" /grant "${acct}:(OI)(CI)R"
icacls "$data\state"   /grant "${acct}:(OI)(CI)M"
icacls "$data\logs"    /grant "${acct}:(OI)(CI)M"
icacls C:\instrument-apis\inheco-tec; icacls $data /T
```

`state\` holds the persisted release state (`state.json`, written with a temp file and an atomic
replace), so it needs modify; `logs\` takes the WinSW log files. Users have no access to any of
them.

### 5.5 Port pre-check

TCP 8743 must be free (DD-3; 8443 is often taken by another service on a lab PC, so the API does
not use it):

```powershell
Get-NetTCPConnection -LocalPort 8743 -ErrorAction SilentlyContinue
```

No output means the port is free. If a process owns it, find it with
`Get-Process -Id (Get-NetTCPConnection -LocalPort 8743).OwningProcess` and resolve the conflict
before the start (a bind failure stops the service with exit code 3).

#### Local host firewall rule

The operator creates the Windows Firewall inbound rule on the API host in an elevated PowerShell. It
is a local operator step on the host (design C17, DD-14). The network path from n8n (subnet
`<subnet-name>`, 198.51.100.0/24) to the lab network must already exist: if it does not, ask the
network administrator to allow TCP 8743 from the n8n host to the API host. This rule only opens
TCP 8743 on the host:

```powershell
New-NetFirewallRule -DisplayName "inheco-tec-api TCP 8743" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8743 -RemoteAddress 198.51.100.0/24 -Profile Domain,Private
Get-NetFirewallRule -DisplayName "inheco-tec-api TCP 8743" | Get-NetFirewallAddressFilter
```

An optional admin subnet may be added to `-RemoteAddress` (for example
`-RemoteAddress 198.51.100.0/24, <admin subnet>`). Record the final remote range here: `________`.

The rule is scoped by local port and remote range, not by program. On Windows the venv
`python.exe` is a redirector that starts the base interpreter, so the process that listens on the
socket is `C:\Program Files\Python311\python.exe`, not the venv executable; a program-scoped rule
for the venv path would not match. The uninstall step (section 5.6) removes the rule.

### 5.6 Operate

Run in `C:\instrument-apis\inheco-tec\service`, elevated:

| Action | Command |
|---|---|
| Start | `.\inheco-tec-api.exe start` |
| Stop | `.\inheco-tec-api.exe stop` (or `Stop-Service inheco-tec-api`) |
| Restart | `.\inheco-tec-api.exe restart` |
| Status | `.\inheco-tec-api.exe status` |
| Uninstall | `.\inheco-tec-api.exe stop`, then `.\inheco-tec-api.exe uninstall`, then `Remove-NetFirewallRule -DisplayName "inheco-tec-api TCP 8743"` |

After a start, check the health route (no token, no device I/O) from the host with the public CA
file that section 2.3 put in `tls\`:

```powershell
curl.exe --cacert C:\ProgramData\instrument-apis\inheco-tec\tls\ca.crt --ssl-no-revoke https://192.0.2.10:8743/healthz   # {"status":"ok"}
```

The certificate is still verified against `ca.crt`. `--ssl-no-revoke` only skips the Windows
revocation lookup, which the private CA does not publish.

#### Reachability check

After the first start, check the path from n8n to the service: run an n8n Get Status call (it
needs the token), or `GET /healthz`, **from the n8n instance**. It must succeed over HTTPS with
certificate verification against `<ca-name>`, which n8n trusts from its image (section 2.4; never
with verification turned off). Then check each controller `state` and the startup slot state. The
service starts "leave as found": it sends no `ATE` or `STT` at startup, so a slot that was on stays
on (S1).

If the call from n8n fails (timeout, connection refused or reset) while the host health check above
passes, the assumed network path from n8n to the lab network (DD-14) does not hold. **Stop and
report it** to the platform owner. Do not change any network setting, firewall or route to make it
work (Risk R14); a fix is outside this runbook.

Uninstall does not delete `C:\ProgramData\instrument-apis\inheco-tec`. Delete `secrets\service.env` and
`tls\server.key` by hand if the host is retired. Remove the local firewall rule with
`Remove-NetFirewallRule -DisplayName "inheco-tec-api TCP 8743"` (the uninstall row above).

### 5.7 Logs

WinSW writes the service output under `C:\ProgramData\instrument-apis\inheco-tec\logs` (files
`inheco-tec-api.*.log`):

- stdout: one JSON line per event (structlog), for example `service_starting`, `tec_transaction`,
  `tec_state_change`, `tec_startup_slot_state`, `tec_slot_stop`, `tec_stop_not_delivered`,
  `tec_emergency_off`, `transport_fake_active`, `http_request`.
- stderr (the `.err` log): uvicorn's own `uvicorn.error` lines as plain text, for example a port
  bind failure.

No log line contains the token, the `Authorization` header or the TLS key; startup errors name
only the manifest key, the env var name or the file path.

### 5.8 Exit codes

Every startup check fails closed with a non-zero exit and one log line. WinSW then restarts the
service up to 3 times (10 s apart); after that the service stays stopped until you fix the cause.

| Exit | Log event | Cause | Fix |
|---|---|---|---|
| 1 | `log_level_invalid` (`key=LOG_LEVEL`) | `LOG_LEVEL` is set to an unknown level name | remove or correct `LOG_LEVEL` (default `INFO`) |
| 2 | `manifest_invalid key=<path> reason=<text>` | `C:\instrument-apis\inheco-tec\instrument.yaml` is missing, not valid YAML, or fails validation | fix the named key in the manifest (reviewed commit + copy) |
| 3 | `auth_config_invalid` (`env_var=INHECO_TEC_API_TOKEN`) | the token env var is unset, empty or whitespace-only (for example a missing `service.env`, logged before as `env_file_absent`, or a file with a BOM) | recreate `service.env` (section 4) |
| 3 | `env_file_unreadable` (`key=service.env_file`) | `service.env` exists but cannot be read or decoded (for example a missing read grant) | fix the file or its ACL (sections 4, 5.4) |
| 3 | uvicorn startup failure (`.err` log) | uvicorn `STARTUP_FAILURE`: the port cannot be bound (in use, or no permission) or the lifespan startup failed | section 5.5 port check; read the `.err` log |
| 4 | `tls_invalid` (`key=tls.cert_file`, `tls.key_file` or `tls`) | the certificate or key file is missing or unreadable, the pair is invalid, or the key is password-protected | reinstall the pair (section 2.3) without a passphrase |

### 5.9 Stop behaviour and stop budget

`Stop-Service`, `.\inheco-tec-api.exe stop` and an OS shutdown (preshutdown) make WinSW send
Ctrl+C. Uvicorn stops accepting requests and drains open ones for at most 5 s. The lifespan then
runs the shutdown stop: `ATE0` to every configured slot of every `connected` controller, each
outcome logged as `tec_slot_stop` with `purpose=shutdown_stop`. `released`, `lost` and
`disconnected` controllers get no command (FR-11.1, FR-11.2).

Budget against the WinSW `stoptimeout` of 30 s:

- Common case: 5 s drain + `service.shutdown_timeout_s` 10 s = **15 s**, well inside 30 s.
- Worst case: the 10 s shutdown deadline is soft by up to one transaction in flight (about 15.8 s
  with retries), so about 5 + 10 + 15.8 = **30.8 s**. That is over 30 s: WinSW can kill the
  process before every `ATE0` is sent.

Shutdown with a busy lock: if another request holds a controller's lock past the shutdown
deadline, the shutdown leaves that controller's handle open and its slots **not stopped**. It logs
`tec_slot_stop` with `outcome=not_attempted`, `failure=controller_busy` at error level. A late
device loss during the shutdown starts only a best-effort stop.

Operator rules:

1. Before a planned stop or restart, run Stop All (or stop each slot) from n8n and read the
   per-slot outcomes; do not stop the service while a long request is running.
2. After every stop, search the log for `tec_slot_stop` lines with `outcome` other than
   `stopped`. For each such slot, stop it in VENUS or at the device, or start the service and
   send a stop.

### 5.10 API documentation page (Swagger UI)

The service serves an interactive Swagger UI page of its REST API on its own HTTPS listener (no
internet access needed; the page files ship in the package).

1. Get the page: reinstall or upgrade from `main` per section 3.4 (non-editable install), then do
   the planned restart per section 5.9 (Stop All from n8n first, then read the per-slot outcomes).
2. Open `https://192.0.2.10:8743/docs` in a browser on a host inside the remote range of the
   section 5.5 local firewall rule: `<subnet-name>` 198.51.100.0/24, or the optional admin subnet
   if one was added to `-RemoteAddress`. Other hosts cannot reach TCP 8743.
3. The page is public and read-only: it does no device I/O, and it shows the full API description
   (routes, schemas, error kinds) to any host that the firewall rule admits (Risk R15, accepted).
4. The page loads without a token. To make a call, select **Authorize** and paste the bearer token
   (section 4). The token stays in page memory only: it is lost on reload and the browser never
   stores it. Every call from the page without a valid token gets HTTP 401. `/openapi.json` still
   needs the token (401 without it).
5. The browser shows a certificate warning, because the private CA of section 2 issues the leaf
   certificate. To remove the warning, trust the CA certificate (the public `.crt` file) in the
   **current user** store only: `certmgr.msc` → Trusted Root Certification Authorities, or

   ```powershell
   Import-Certificate -FilePath <CA certificate file> -CertStoreLocation Cert:\CurrentUser\Root
   ```

   Never trust it in the machine-wide store (`Cert:\LocalMachine\Root`). Reason: the CA private
   key is readable by project-level `secretAccessor`/`editor` principals (accepted risk AR-1,
   decisions 2026-10-12, Risk R18), so a machine-wide trust would let a compromised principal
   intercept the TLS of every user on that machine.

### 5.11 Emergency Off recovery

After an Emergency Off (`0AEO`), the TEC controller does not accept the re-initialize until it is
power-cycled (checklist item 12, observed 2026-10-13). Use this one procedure in every case:

1. **Release if connected, power-cycle, then Reacquire.** (1) If the controller is `connected`,
   call Release (`POST /v1/controllers/{cid}/release`). Release is allowed while emergency-off is
   active; only set target and start are refused (FR-17.7). (2) Power-cycle the TEC controller
   (the service can keep running). (3) Then call Reacquire
   (`POST /v1/controllers/{cid}/reacquire`). Success is HTTP 200 with `emergency_off_cleared: true`,
   and `stopped_slots` lists every configured slot (FR-17.8, FR-17.10).
2. **A 502 with firmware code `I` means the power cycle is missing.** If Reacquire returns HTTP 502
   `stop_all_failed` with firmware code `I` ("CPAC voltage not OK") on the slots and the
   power-cycle message (FR-17.11), the power cycle is missing. Emergency-off stays active and the
   controller stays `released`. Power-cycle the TEC controller, then call Reacquire again.
3. **Connect on a `connected` controller after a power cycle is not the documented path.** The
   service keeps the old device handle, which is likely dead after the power cycle, so the call
   fails closed and emergency-off stays active. Call Release first (point 1).
4. **Reacquire on a `connected` controller is a no-op** (FR-12.5) and does not clear
   emergency-off. Call Release first (point 1).
5. **A service restart is not a recovery step.** A service restart clears the emergency-off flag
   (FR-17.9) but does not recover the device, so a service restart is not a recovery step.

## 6. Running a legacy VENUS method (release and reacquire)

The API and VENUS (`HSLInhecoTECLib`) must never drive the same controller at the same time. To run
a legacy VENUS method that uses the TEC, hand the controller over with Release and take it back
with Reacquire (FR-12, O15). The API routes are listed; the n8n node has the same operations
(Release Controller, Reacquire Controller, Stop Temperature Control, Stop All, Emergency Off,
Connect, Get Status).

1. Pause every n8n workflow that uses the controller.
2. **Release:** `POST /v1/controllers/{cid}/release` (controller 0 on the API host). The API waits for
   any transaction in progress, closes the HID handle and persists the controller as released in
   `C:\ProgramData\instrument-apis\inheco-tec\state\state.json`. The response shows `state: released`. The
   released state survives a service restart (FR-12.4).
3. **Run the VENUS method.** While released, the API refuses all device commands with 409
   `controller_released`, except:
   - Stop (`POST /v1/controllers/{cid}/slots/{sid}/stop`), Stop All (`POST /v1/stop-all`) and
     Emergency Off: the API opens the device for a moment, sends only `ATE0` (or `0AEO`) and the
     resync, closes it again, and stays `released` (FR-12.6, FR-17.4).
   - **HTTP 409 `stop_not_delivered`** ("Stop was NOT delivered …" or "Emergency-off was NOT
     delivered …") means the device could not be opened (VENUS holds it) and **nothing was sent**.
     The heater may still be on. **The operator must stop the slot in VENUS or at the device.**
4. **Reacquire** after the VENUS method has ended and VENUS has closed the device:
   `POST /v1/controllers/{cid}/reacquire`. The API opens the device, identifies the slots and
   clears the persisted released state. It sends **no** `ATE0` (FR-12.3): a slot that VENUS left on
   stays on. Run Get Status and check every slot before you resume workflows.
5. Resume the workflows.

If the release state file is corrupt, the service treats every controller as released and opens no
device (DD-11; error log `tec_state_file_corrupt`). Stop and Emergency Off still work. Reacquire
each controller on purpose, after you confirm VENUS does not hold it.

### 6.1 Safety responses the operator must act on

- **Retry Stop and Emergency Off on 503 `controller_busy`.** A stop or emergency-off can get 503
  `controller_busy` (with `Retry-After: 1`) while a long step holds the controller lock: startup
  identification, Connect / reconnect after a loss (the `ATE0` sweep and identification), the
  loss handling itself, or the emergency-off re-initialize (Connect or Reacquire while
  emergency-off is active). The lock wait is `lock_wait_s` (16 s). Repeat the request until it
  succeeds; if it keeps failing, stop the slot in VENUS or at the device.
- **503 `service_stopping`:** the service is shutting down and runs its own shutdown stop (section
  5.9). Check the log afterwards.
- **Stop All returns 200 with `skipped` slots.** Read the per-slot outcomes, not only the status
  code. `skipped` means the controller was `disconnected` or `lost`, and no stop was sent. 409
  `stop_not_delivered` means at least one released controller could not be opened; 502
  `stop_all_failed` means at least one slot failed.
- **"Stop outcome UNKNOWN" / "Emergency-off outcome UNKNOWN"** (504 `transport_error`, or 503
  `controller_not_connected` after a device loss): the command may or may not have been applied.
  Treat the slot as possibly still on, and confirm in VENUS or at the device.
- **Emergency Off** needs the body `{"confirm": true}`. After it, set target and start are refused
  with 409 `emergency_off_active`; reads, status and stop still work. Connect (connected
  controller) or Reacquire (released controller) re-initializes: `ATE0` to every slot, then
  identification, then the flag clears (`emergency_off_cleared: true`). The flag does not persist
  across a service restart (FR-17.9). Recovery needs a power cycle first: see section 5.11.

## 7. Hardware-verification checklist

The release gate (FR-16.3, FR-16.4). Items 1–14 are the FR-16.3 minimum; items 15–18 cover design
assumptions. Run the read-only items first (`0RFV1`, `0RTD0` and the other reads), before any
`STT` or `ATE`. For each item, record the method used, the observed result, the date and the
verifier, then update the count in the section 1 banner. An item with an empty result is
unverified. An item counts as verified, and in the section 1 banner, only when its observed result,
date and verified-by cells are all filled. The three filled rows are the results of the reference
deployment; `<maintainer>` stands for the person who verified them.

| # | Item | Method | Observed result | Date | Verified by |
|---|---|---|---|---|---|
| 1 | HID input report length; `#` continuation in replies; whether `hidapi` strips report ID 0. Code reference: `transport/base.py`, `transport/hidapi_transport.py`, `protocol/framing.py` (INFERRED item 1). | With the service stopped and VENUS closed, open `VID_03EB&PID_2023` from the venv with `hid`, send the framed `0RFV1`, record the raw report length, the first byte and any `#` continuation. | | | |
| 2 | CRC reconstruction: `0RFV1` reply CRC validates; outbound golden vectors accepted. Code reference: `protocol/crc.py`, `protocol/framing.py` (INFERRED item 2). | Send `0RFV1` and `0RTD0`; confirm the reply CRC validates and the controller accepts the golden-vector frames (reply code `0`). | | | |
| 3 | Address `0` = mainboard for `RTD`, `RAN`, `RFV`, `RHV`, `RDC`, `AEO` (`RAN` observed 2026-10-13, item 11). Code reference: `protocol/commands.py`, `identification.py` (INFERRED item 3). | Send the read-only `0RFV1`, `0RTD0`, `0RHV0` and compare with the controller data; `0AEO` is covered by item 12. | | | |
| 4 | Meaning and side effects of `RDC` / `0RDC1` resync. Code reference: `engine.py`, `protocol/commands.py` (INFERRED item 4). | Send a read, then `0RDC1`; record the reply and confirm no slot state changes (`RHE`, `RTT` before and after). | | | |
| 5 | Case of the error character (`A`/`a`). Code reference: `protocol/framing.py` (INFERRED item 5). | Provoke a busy or an error reply with a read-only command; record the error character as received. | | | |
| 6 | `RHV` reply format per selector. Code reference: `operations_read.py`, `transport/fake.py` (INFERRED item 6). | Get Sensor Values for selectors 0–8; record each raw `data` string. | | | |
| 7 | Whether the 200 ms inter-command gap is needed. | Compare `tec_transaction` results with `pre_send_delay_ms` 200 and the 100 ms floor in a test manifest. | | | |
| 8 | `RAT` selector semantics (`RAT3` vs `RAT`). Code reference: `identification.py`, `protocol/commands.py` (INFERRED item 8). | Get Actual Temperature with selectors 1, 2, 3 against a reference thermometer; record each value. | | | |
| 9 | `RLT`/`RMT1` values and `RTD` type of both installed CPACs. Code reference: `identification.py`, `protocol/tables.py` (INFERRED item 9). | Read `RLT`, `RMT1` (startup identification log) and Get Device Type for slots 1 and 2; record the values and the effective limits in `/v1/status`. | | | |
| 10 | Slot-to-serial mapping (the serial number of each installed CPAC). | Identify each CPAC by its label and the slot it answers on; record slot → SN. | | | |
| 11 | `0RAN<slot>` addressing, reply format, and the article numbers of both CPACs. Code reference: `identification.py`, `protocol/commands.py` (observed item 11, amendment A6). | Read-only probe with the controller released (Release first, no `STT`/`ATE`): send `1RAN`, `2RAN`, `0RAN`, `0RAN0`, `0RAN1`, `0RAN2`; record each reply; compare the article with the CPAC labels and the manifest `article_numbers`. | `0RAN1` and `0RAN2` each → `0ran07000190` (E `0`, data `7000190`): article 7000190 on slots 1 and 2 (`cpac`, CPAC Ultraflat 4–70 °C, in the manifest); `1RAN` → error 4 "Command unknown" (both slots); `0RAN` and `0RAN0` → error 5 "Wrong parameter" | 2026-10-13 | `<maintainer>` |
| 12 | `0AEO` reply, controller behaviour after it, and the FR-17.8 recovery. Code reference: `protocol/commands.py` (INFERRED item 12). | With both slots off, Emergency Off (`confirm: true`); Release; Reacquire without a power cycle; power-cycle the TEC controller with the service running; Reacquire again | `0AEO` → code `0`, emergency-off active; Reacquire (released) → re-initialize `1ATE0`, `2ATE0` failed with firmware code `I` ("CPAC voltage not OK"), HTTP 502 `stop_all_failed`, per-slot `failed` / `I`, controller stayed `released`, flag stayed active; after the power cycle Reacquire → HTTP 200, `connected`, `stopped_slots` [1, 2], emergency-off cleared (FR-17.10) | 2026-10-13 | `<maintainer>` |
| 13 | Whether `RHE` (no selector) reflects the `ATE` enable state. Code reference: `identification.py`, `protocol/commands.py` (INFERRED item 13). | Read `RHE` with the slot off, after `ATE1`, and after `ATE0` (startup slot state in the log); record each value. | | | |
| 14 | FR-12.6 transient open while VENUS holds the device (open fails, or opens and interleaves). | Release, open the device in a VENUS method, then send Stop; record 200 or 409 `stop_not_delivered` and any VENUS error. | | | |
| 15 | NFR-2 latency on hardware (single command ≤ 1.5 s, at-target ≤ 2.5 s). | Time Get Target Temperature and Check At Target on the host (`tec_transaction` `duration_ms`, no retries); record the values. | | | |
| 16 | The service virtual account can open the HID device. | Start the service as `NT SERVICE\inheco-tec-api`; confirm `state: connected` in `/v1/status` and no `tec_open_failed`. Record the account that `sc.exe qc inheco-tec-api` shows (`SERVICE_START_NAME`), and the `LocalSystem` fallback if used. | runs as `NT SERVICE\inheco-tec-api` (`sc.exe qc` `SERVICE_START_NAME`), reached `connected`, no `tec_open_failed` | 2026-10-13 | `<maintainer>` |
| 17 | `Stop-Service` and an OS restart run the stop-all on connected controllers (log evidence). | With a slot on, run `Stop-Service inheco-tec-api`, then an OS restart; confirm `tec_slot_stop` `purpose=shutdown_stop` `outcome=stopped` per slot. Confirm that the Ctrl+C from WinSW reaches the Python process through the console-script launcher and the venv redirector. | | | |
| 18 | Leave-as-found: a service restart during an active chill does not change the slot. | Chill a slot to 4.0 °C, then end the service's Python process with `Stop-Process -Force` (a simulated crash; a graceful stop sends `ATE0`, section 5.9) and let WinSW restart it; confirm no `ATE`/`STT` at startup and `tec_startup_slot_state` shows the slot still on at its target. | | | |

## Placeholders

The examples in this README and the files of this repository use the placeholder values below.
Replace each one with the value of your installation. The names in angle brackets inside the
runbook commands (for example `<CA_KEY_SECRET_NAME>`, `<release-tag>` or `<winsw-asset>`) are
values that you choose or record during the install.

<!-- placeholders:begin -->
| Placeholder | Meaning | Example |
|---|---|---|
| `192.0.2.10` | LAN address of the instrument API host. | 203.0.113.25 |
| `198.51.100.0` | Network address of the subnet that the n8n host calls the API from. | 203.0.113.0 |
| `203.0.113.5` | Test address of a remote device in the transport tests. | 203.0.113.50 |
| `203.0.113.9` | Second test address of a remote device in the transport tests. | 203.0.113.90 |
| `<gcp-project-id>` | Google Cloud project id of your deployment. | my-instrument-project |
| `<issue-id>` | Issue tracker id of a change. | PROJ-123 |
| `<maintainer>` | Name of the person who maintains the deployment. | Jane Doe |
| `<ca-name>` | Name of the certificate authority that signs the API host certificate. | Example Lab Root CA |
| `C:\instrument-apis` | Install root folder of the instrument APIs on the Windows host. | D:\lab\apis |
| `C:\ProgramData\instrument-apis` | Data root folder of the instrument APIs on the Windows host. | D:\lab\data |
| `C:/ProgramData/instrument-apis` | Data root folder of the instrument APIs, written with forward slashes. | D:/lab/data |
| `instrument-apis` | Neutral folder name of the install root and the data root. | lab-apis |
| `<knowledge-base-ref>` | Reference to a page of your internal knowledge base. | your instrument wiki page |
| `<private-doc-ref>` | Reference to a private document of your organization. | your site runbook |
| `<api-repo-root>` | Root folder of a checkout of the API repository. | inheco-tec-api |
| `<node-repo-root>` | Root folder of a checkout of the paired n8n node repository. | n8n-nodes-inheco-tec |
| `<vendored-core>` | The vendored instrument_api_core package and its tests inside the API repository. | src/inheco_tec_api/_vendor/instrument_api_core |
| `the API host` | The Windows PC that runs the instrument API (prose form). | the instrument PC next to the device |
| `<subnet-name>` | Name of the instrument subnet. | instrument-subnet |
| `<release-tag>` | Git tag of the release that the host installs. | v0.1.0 |
| `<winsw-asset>` | File name, without .exe, of the downloaded WinSW x64 executable. | WinSW-x64 |
| `<env>` | Name of the WinSW service XML element that sets one environment variable of the service (an element name, not a value to replace). | the env element with the name and value attributes |
<!-- placeholders:end -->

## License

MIT; see [LICENSE](LICENSE). The vendored Swagger UI files under `src/inheco_tec_api/docs_assets/`
are unchanged copies of npm `swagger-ui-dist` and keep their Apache-2.0 license (`LICENSE` and
`NOTICE` in that folder).

## Paired n8n node

This API and the n8n community node `n8n-nodes-inheco-tec` are one `inheco-tec` pair: the API
drives the Inheco TEC controllers attached to the STAR PC over USB HID, and the node calls the API
from n8n workflows. They change, version, and ship together. In the source project they lived side
by side as `api/` and `node/` next to the manifest `instrument.yaml`.

- This repository — the REST service `inheco-tec-api`, built on `instrument-api-core` (vendored).
  It runs on the instrument host as a Windows service (host-resident); it is not a cloud
  deployable and needs no Terraform.
- [`n8n-nodes-inheco-tec`](https://github.com/jamie-zaikov/n8n-nodes-inheco-tec) — the n8n
  community node, built on `n8n-node-core` (vendored in that repository). It reaches the API only
  over HTTP(S), never by code import.
- `instrument.yaml` — the executable manifest (host/IP, port, protocol, auth, controllers and
  slots, limits, capabilities, refs).
