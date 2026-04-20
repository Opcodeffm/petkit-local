# Security Findings — Petkit Fresh Element Solo (D4)

This document describes security & privacy issues discovered in the Petkit
Fresh Element Solo (firmware 1.267) and related D4-protocol feeders during
the development of this integration. They motivate why this project exists:
to give the device a fully local, self-hosted backend that does **not**
depend on Petkit's cloud.

Findings are listed from highest to lowest severity. CVSS scores are the
reporter's estimates; no CVE IDs have been requested yet.

---

## F-01 — Unauthenticated Remote Command Execution via HTTP Cloud Channel

**Severity:** High · **Estimated CVSS 3.1:** 8.2 (AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:H/A:H)
**CWE:** [CWE-306](https://cwe.mitre.org/data/definitions/306.html) — Missing
Authentication for Critical Function

The feeder polls its cloud server (`api.eu-pet.com`, plain HTTP port 80)
every ~11 seconds for pending commands. The response format embeds
commands like:

```json
{"result": [{"content": "{\"msgType\":2,\"type\":\"feed_realtime\",\"payload\":{\"amount\":20,\"id\":\"r_20260420_30000_30000-1\"}}", "time": 1776696493929}]}
```

These commands are **not authenticated, signed, or validated** in any way.
An attacker who can intercept / MITM the HTTP traffic (e.g. ARP spoofing,
DNS hijack, malicious WiFi, or simply a local-network adversary at the
feeder's WiFi) can arbitrarily trigger feed commands, push settings,
change schedules, or inject malicious data.

This project demonstrates the issue: we built our own cloud replacement
and the feeder accepts all commands we send with no secret, no signature,
no challenge-response.

## F-02 — BLE Provisioning Accepts Arbitrary Server URL

**Severity:** High · **Estimated CVSS 3.1:** 7.1 (AV:A/AC:L/PR:N/UI:N/S:U/C:L/I:H/A:L)
**CWE:** [CWE-940](https://cwe.mitre.org/data/definitions/940.html) —
Improper Verification of Source of a Communication Channel

During Wi-Fi provisioning over BLE (BluFi-compatible protocol on
service `0xFFFF`, characteristics `0xFF01` / `0xFF02`), the feeder accepts
a `key=151` JSON message containing:

```json
{"key": 151, "payload": {
  "ssid": "<wifi>", "pwd": "<password>",
  "apiServers": ["http://<attacker-server>/6/"],
  "ipServers":  ["http://<attacker-server>:80/6/"]
}}
```

The feeder writes these URLs to its persistent flash and will subsequently
reach out to whatever server is specified, **without any validation**
(no cert pinning, no domain whitelist, no signature on provisioning
payload). An attacker within BLE range during pairing mode (LED fast-blink)
can permanently capture the device.

We use this behavior as the intended mechanism to redirect the feeder to
a user-controlled local server — it is, however, a property that can be
abused by anyone within BLE range during initial setup.

## F-03 — Cleartext Transmission of Device State and Commands

**Severity:** Medium · **Estimated CVSS 3.1:** 5.3 (AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:L/A:N)
**CWE:** [CWE-319](https://cwe.mitre.org/data/definitions/319.html) —
Cleartext Transmission of Sensitive Information

All communication between the feeder and its cloud uses plain HTTP
(port 80). No TLS is used, no certificate validation, no
confidentiality. State reports (containing WiFi SSID, BSSID, battery
voltage, firmware version, device MAC, serial number, feed history) are
passively readable on any shared-network segment.

Captured example (redacted):

```json
{"DCV": 5148, "batV": 8100, "firmware": "1.267",
 "wifi": {"bssid": "<redacted>", "rsq": -58, "ssid": "<redacted>"}, ...}
```

## F-04 — Missing Integrity Controls on BLE Channel

**Severity:** Low–Medium · **Estimated CVSS 3.1:** 4.3 (AV:A/AC:L/PR:N/UI:N/S:U/C:L/I:L/A:N)
**CWE:** [CWE-311](https://cwe.mitre.org/data/definitions/311.html) —
Missing Encryption of Sensitive Data

The feeder's BluFi implementation uses the Espressif default parameters
with all security features disabled:

| Flag       | Value   |
|------------|---------|
| Encryption | off     |
| Checksum   | off     |
| ACK        | off     |

Verified via decompilation of the official Petkit Android app
(`BlufiClientImpl` defaults confirm `mEncrypted = false`, `mChecksum = false`,
`mAck = false`). Combined with F-02, this means the entire provisioning
exchange — including the user's Wi-Fi password in plaintext — is readable
by any BLE observer within range during pairing.

## F-05 — Telemetry Destination

**Severity:** Privacy concern, not a vulnerability per se.

The feeder is hardcoded to poll **`api.eu-pet.com`** over HTTP and
maintains a permanent MQTT session to
**`eu-central-prod.mqtt.iotgds.aliyuncs.com`** (Alibaba IoT platform).
"EU" in the hostname notwithstanding, the MQTT broker is part of Alibaba
Cloud infrastructure. The device reports continuously on:

- Every feeding (timestamp, amount, trigger source)
- Battery state, desiccant status, food-container level
- Wi-Fi SSID, BSSID, signal strength
- Device serial number, firmware version
- Uptime, presence (inferred from heartbeat cadence)

There is no documented way in the official app to opt out of this
telemetry or to run in local-only mode. This integration removes the
feeder's need for any external connectivity.

---

## Disclosure status

As of the initial public disclosure date, **no vendor contact has been
attempted**. The findings are documented here for the community and to
motivate the existence of this project. Users who want CVE IDs assigned
can submit to MITRE referencing this file.

## Responsible use

This project is intended for owners of a device operating on their own
networks, to free their hardware from a vendor-cloud dependency. It is
**not** intended to be used to compromise devices owned by others.
Attacks described in F-01 through F-04 are relevant to any attacker who
can already reach the feeder at the network or BLE level — this document
merely makes the mechanisms public so users can make informed choices
about where they deploy their feeder.
