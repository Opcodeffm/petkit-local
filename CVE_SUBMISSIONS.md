# CVE Submission Drafts

Ready-to-paste blocks for MITRE's public CVE request form at
<https://cveform.mitre.org/>.

> **Important:** these drafts reference
> `https://github.com/Opcodeffm/petkit-feeder-local/blob/main/SECURITY.md`.
> That URL only works once the repository is public. Make the repo public
> **before** submitting. MITRE will reject requests with private /
> inaccessible references.

## Submission workflow (per finding)

1. Go to <https://cveform.mitre.org/>
2. *Select a request type* → **Request a CVE ID**
3. Fill the form using the block below (one submission per finding — four in total)
4. Leave "Have you been in contact with the vendor?" → **No**
5. Submit. MITRE sends a confirmation email and (typically 2–6 weeks later) a CVE ID.
6. Once assigned, add the CVE IDs back into `SECURITY.md` and this file, then re-commit.

Each block is labeled to match the MITRE form field. Copy paste one section
per submission.

---

## F-01 · Unauthenticated remote command execution via HTTP cloud channel

**Vulnerability type:** Authentication
**CWE:** [CWE-306: Missing Authentication for Critical Function](https://cwe.mitre.org/data/definitions/306.html)
**CVSS 3.1:** 8.2 High · `AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:H/A:H`

### Vendor of the product(s)
```
Petkit Network Technology Co., Ltd.
```

### Affected product(s)/code base
```
Petkit Fresh Element Solo (model D4), firmware 1.267
Likely affected: other Petkit D-series feeders sharing the /d4/ cloud protocol
(YumShare Solo, YumShare Dual-Hopper)
```

### Has vendor confirmed or acknowledged the vulnerability?
```
No. Issue disclosed publicly without prior vendor contact.
```

### Attack type
```
Remote
```

### Impact
```
Code Execution: yes (attacker can trigger feed commands and push settings)
Denial of Service: yes (bogus commands can exhaust food or block motor)
```

### Affected component(s)
```
HTTP cloud channel: the feeder polls api.eu-pet.com every ~11 seconds
for commands. The /d4/poll/heartbeat endpoint's JSON response can carry
commands (msgType/type/payload fields) which the feeder executes without
any authentication or signature check.
```

### Attack vector(s)
```
Any attacker who can MITM the feeder's HTTP traffic — via ARP spoofing,
DNS hijack, malicious Wi-Fi, or local-network adversary on the feeder's
Wi-Fi — can push arbitrary feed/config commands. No credentials, no shared
secret, no signature, no challenge-response is required.
```

### Suggested description
```
Petkit Fresh Element Solo feeder firmware 1.267 and related D4-series
feeders accept device commands from their HTTP cloud channel
(api.eu-pet.com, plain HTTP port 80) without any authentication or
integrity verification. A network attacker capable of MITM'ing the
feeder's cloud traffic can inject arbitrary feed, schedule, or
configuration commands, as demonstrated by the project's
successful substitution of the vendor cloud with a self-hosted server.
```

### Discoverer
```
Opcodeffm (independent researcher)
```

### References
```
https://github.com/Opcodeffm/petkit-feeder-local/blob/main/SECURITY.md#f-01--unauthenticated-remote-command-execution-via-http-cloud-channel
```

### Additional information
```
Proof of concept is the repository itself: a Home Assistant integration
that fully substitutes the vendor's cloud endpoint and commands the
feeder using no cryptographic material. Fix would require adding
signed responses (HMAC with a device-bound key) or moving the channel
to TLS with pinned certificates.
```

---

## F-02 · BLE provisioning accepts arbitrary server URL without authentication

**Vulnerability type:** Authentication / Authorization
**CWE:** [CWE-940: Improper Verification of Source of a Communication Channel](https://cwe.mitre.org/data/definitions/940.html)
**CVSS 3.1:** 7.1 High · `AV:A/AC:L/PR:N/UI:N/S:U/C:L/I:H/A:L`

### Vendor of the product(s)
```
Petkit Network Technology Co., Ltd.
```

### Affected product(s)/code base
```
Petkit Fresh Element Solo (model D4), firmware 1.267
Likely affected: other Petkit feeders using the same BluFi-compatible
BLE provisioning protocol (YumShare Solo, YumShare Dual-Hopper).
```

### Has vendor confirmed or acknowledged the vulnerability?
```
No. Issue disclosed publicly without prior vendor contact.
```

### Attack type
```
Physical / Adjacent (Bluetooth Low Energy, ~10 m range)
```

### Impact
```
Code Execution: partial (attacker can point device to attacker-controlled cloud,
which then enables the full unauthenticated command channel from F-01)
Device Persistence: attacker's server URL is written to flash and survives reboots
```

### Affected component(s)
```
BLE provisioning over BluFi protocol (GATT service 0xFFFF,
write characteristic 0xFF01, notify 0xFF02). A JSON custom data
frame with key=151 includes fields "apiServers" and "ipServers" that
accept arbitrary URLs. No signature, no certificate pinning, no
whitelist is enforced.
```

### Attack vector(s)
```
An attacker in BLE range during pairing mode (LED fast-blink, triggered
by a 5-second button press or first-time setup) can perform the complete
BluFi handshake — no pairing key, no out-of-band secret — and issue the
provisioning message with attacker-controlled apiServers/ipServers.
Once written, the feeder permanently talks to attacker's server.
```

### Suggested description
```
Petkit Fresh Element Solo feeder firmware 1.267 accepts arbitrary
apiServers and ipServers URLs during BLE provisioning (BluFi custom
data, key=151) without source verification, certificate pinning, or
signed provisioning payload. An attacker in BLE range during pairing
can permanently redirect the feeder to any HTTP server, gaining
persistent command-channel control (see CVE for F-01).
```

### Discoverer
```
Opcodeffm (independent researcher)
```

### References
```
https://github.com/Opcodeffm/petkit-feeder-local/blob/main/SECURITY.md#f-02--ble-provisioning-accepts-arbitrary-server-url
```

### Additional information
```
The BLE channel carries no cryptographic protection (see F-04), so the
provisioning hijack can be performed passively and invisibly from
within radio range.
```

---

## F-03 · Cleartext transmission of device state and commands

**Vulnerability type:** Information Disclosure
**CWE:** [CWE-319: Cleartext Transmission of Sensitive Information](https://cwe.mitre.org/data/definitions/319.html)
**CVSS 3.1:** 5.3 Medium · `AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:L/A:N`

### Vendor of the product(s)
```
Petkit Network Technology Co., Ltd.
```

### Affected product(s)/code base
```
Petkit Fresh Element Solo (model D4), firmware 1.267
Likely affected: all Petkit D-series feeders using api.eu-pet.com.
```

### Has vendor confirmed or acknowledged the vulnerability?
```
No. Issue disclosed publicly without prior vendor contact.
```

### Attack type
```
Remote (passive)
```

### Impact
```
Information Disclosure: Wi-Fi SSID, BSSID, battery voltage,
firmware version, device MAC, serial number, feeding history,
and uptime — passively readable from any shared-network segment.
```

### Affected component(s)
```
HTTP cloud channel: api.eu-pet.com, plain HTTP port 80. No TLS,
no certificate validation. Both the feeder-to-cloud heartbeat/state
channel and the cloud-to-feeder command channel are in the clear.
```

### Attack vector(s)
```
Passive network observer on any segment the traffic traverses —
the feeder's LAN, an intermediate ISP link, or the cloud provider's
network. Captures reveal location-identifying data (BSSID + SSID
enable ~10 m geolocation via commercial WPS databases) and
behavioral data (timestamped feedings infer presence, schedules).
```

### Suggested description
```
Petkit Fresh Element Solo feeder firmware 1.267 transmits all
cloud-channel telemetry and commands over cleartext HTTP
(api.eu-pet.com, port 80). Captures disclose Wi-Fi SSID,
router BSSID, device serial number, firmware version, battery
voltage, feeding timestamps, and online/offline patterns to any
passive network observer.
```

### Discoverer
```
Opcodeffm (independent researcher)
```

### References
```
https://github.com/Opcodeffm/petkit-feeder-local/blob/main/SECURITY.md#f-03--cleartext-transmission-of-device-state-and-commands
```

---

## F-04 · Missing encryption / integrity controls on BLE channel

**Vulnerability type:** Cryptographic
**CWE:** [CWE-311: Missing Encryption of Sensitive Data](https://cwe.mitre.org/data/definitions/311.html)
**CVSS 3.1:** 4.3 Medium · `AV:A/AC:L/PR:N/UI:N/S:U/C:L/I:L/A:N`

### Vendor of the product(s)
```
Petkit Network Technology Co., Ltd.
```

### Affected product(s)/code base
```
Petkit Fresh Element Solo (model D4), firmware 1.267
Likely affected: other Petkit feeders using Espressif BluFi with the
default ("off") security parameters.
```

### Has vendor confirmed or acknowledged the vulnerability?
```
No. Issue disclosed publicly without prior vendor contact.
```

### Attack type
```
Physical / Adjacent (Bluetooth Low Energy, ~10 m range)
```

### Impact
```
Information Disclosure: user's Wi-Fi password is transmitted in plaintext
during provisioning.
Integrity: no checksum / no ACK — any BLE observer can replay or tamper
with provisioning frames undetected.
```

### Affected component(s)
```
BLE provisioning via BluFi protocol. Espressif defaults for encryption,
checksum, and ACK are all disabled. Confirmed via decompilation of
Petkit's Android app (BlufiClientImpl): mEncrypted = false,
mChecksum = false, mAck = false.
```

### Attack vector(s)
```
Any BLE observer within ~10 m of the feeder during pairing captures
the user's Wi-Fi PSK in plaintext from the key=151 provisioning frame.
Additionally, the lack of checksum/ACK means replay or frame injection
cannot be detected by the device.
```

### Suggested description
```
Petkit Fresh Element Solo feeder firmware 1.267 uses the Espressif
BluFi BLE provisioning protocol with all security features disabled
(no encryption, no checksum, no ACK). The user's Wi-Fi pre-shared key
is consequently transmitted in plaintext to the device during initial
provisioning, and no integrity protection prevents BLE frame tampering
or replay.
```

### Discoverer
```
Opcodeffm (independent researcher)
```

### References
```
https://github.com/Opcodeffm/petkit-feeder-local/blob/main/SECURITY.md#f-04--missing-integrity-controls-on-ble-channel
```

### Additional information
```
Combined with F-02 (BLE provisioning accepts arbitrary server URL),
this enables a passive attacker within BLE range to (1) learn the
user's Wi-Fi credentials and (2) redirect the device's cloud channel
to an attacker-controlled server — all without user interaction
beyond the expected pairing button press.
```
