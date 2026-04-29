# Firmware Protection Guide

Once you've set up this integration, your feeder will work indefinitely
without Petkit's cloud. The **one realistic way** that this setup can
break is if the feeder's firmware gets updated to a version that
defeats the local redirect (e.g. hardcoded IPs, TLS with cert pinning,
HMAC-signed responses, or simply a new endpoint).

This guide documents the layered defense against that.

---

## Defense layer 1 — Our server refuses OTA updates

The integration's local HTTP server handles the feeder's `dev_ota_check`
endpoint and always responds with:

```json
{"hasNewVersion": false}
```

As long as the feeder asks **us** whether there's an update, the answer
is "no" — forever. Your firmware is frozen at whatever version it was at
when you set up the redirect.

This works because the feeder trusts whoever is at
`api.eu-pet.com`, and thanks to your DNS override, that's now your HA.

## Defense layer 2 — Block the feeder from the Internet

The OTA-block assumes the feeder only ever talks to our server. If the
firmware also tries to reach a **different** update URL (e.g. `ota.petkit.com`
or a hardcoded IP), the OTA-block won't protect it.

Cheap insurance: in your router/firewall, **block all outbound traffic
from the feeder's IP to the public Internet.**

### On UniFi

Firewall → Internet → Rules → Create New:
- Type: Reject (or Drop)
- Source: the feeder's LAN IP
- Destination: anywhere
- Scope: all protocols

Local-LAN traffic (feeder ↔ HA) is unaffected — it's only Internet-egress
that's blocked.

### On OPNsense/pfSense

Firewall → Rules → LAN:
- Action: Reject
- Source: feeder IP
- Destination: "!LAN net" (negated — not-LAN = Internet)

### On Pi-hole / AdGuard

Insufficient alone — those only filter DNS. The feeder could still reach
hardcoded IPs. You need a real firewall rule.

### Verification

After blocking: check the feeder still works (feeds, heartbeats,
schedule). If so, the Internet-block doesn't affect daily operation — it
just prevents firmware exfiltration.

## Defense layer 3 — Never use the Petkit app on the same network

The Petkit mobile app communicates with the feeder **two ways**:

1. **Via the cloud** (which you blocked — it gets "device offline")
2. **Via BLE or local discovery** if the phone is on the same LAN

The second path is the real danger: the app can push firmware updates
directly to the feeder, completely bypassing our DNS redirect and
Internet-block.

**Recommended:**

- **Delete the Petkit app** from your phones once the feeder is
  integrated into HA.
- If others in the household also had the app installed: have them
  delete it too. The app on any device that connects to your WiFi can
  potentially trigger an update.
- If you need to give someone access to trigger feedings, use HA's user
  system + the HA companion app instead.

## Defense layer 4 — Monitor for firmware changes

If the firmware somehow gets updated despite defenses 1–3 (e.g. a
household member opened the app briefly), you want to know immediately.

Add this automation to your HA setup:

```yaml
automation:
  - alias: "Feeder firmware changed"
    trigger:
      - platform: state
        entity_id: sensor.futterautomat_firmware
    condition:
      - condition: template
        value_template: "{{ trigger.from_state.state not in ['unknown','unavailable',''] }}"
      - condition: template
        value_template: "{{ trigger.to_state.state   not in ['unknown','unavailable',''] }}"
      - condition: template
        value_template: "{{ trigger.from_state.state != trigger.to_state.state }}"
    action:
      - service: notify.persistent_notification
        data:
          title: "⚠️ Petkit firmware changed"
          message: >-
            Feeder firmware changed from
            {{ trigger.from_state.state }}
            to
            {{ trigger.to_state.state }}
            at {{ now().strftime('%Y-%m-%d %H:%M') }}.
            Integration may need to be re-tested — check SECURITY tab in
            the Petkit Feeder Local repo for a mitigation.
```

Replace `sensor.futterautomat_firmware` with your entity ID if different
(e.g. `sensor.feeder_firmware` on English HA). Also works with mobile
notification services instead of `persistent_notification` — swap the
action to your preferred notifier.

## Defense layer 5 — Keep a firmware backup (advanced)

If you're worried about an unrecoverable firmware update bricking the
DIY setup, the bulletproof solution is to have a copy of the current
"known good" firmware so you can downgrade.

Unfortunately, **no public firmware dumps of `1.267` exist** as of
writing. Getting one requires either:

1. Capturing the firmware blob when Petkit pushes an OTA update (watch
   the `dev_ota_check` endpoint for a `url` field, then curl the blob)
2. Physical extraction via UART/JTAG (out of scope for a regular user)

If you manage to capture an OTA URL, please open an issue — we can
mirror the blob so the community has a downgrade path.

---

## Why this is probably enough

Chinese IoT manufacturers rarely push mandatory firmware updates,
especially for niche markets. Pushing an update means:

- Bandwidth cost
- Support cost (bricked devices, failed updates)
- Admitting the original firmware had issues (PR/legal risk)
- Ongoing obligation to keep new firmware patched

**For the tiny fraction of users who set up a local replacement**,
pushing a targeted firmware update is not economically sensible for
Petkit. Our position becomes risky only if this project goes viral —
and even then, the defense-in-depth above makes it surprisingly hard to
re-capture a device that's properly firewalled from both the Internet
and the Petkit app.

## What to do if defenses fail

If your feeder's firmware does get updated and breaks the integration:

1. Open an issue on this repo with the new firmware version + any log
   you can capture of the new protocol behavior
2. Don't factory-reset the feeder yet — the old config might still be
   recoverable
3. Watch the [commit history](https://github.com/Opcodeffm/petkit-local/commits/main)
   — the community may have published a fix
