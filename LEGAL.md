# Legal Position

This project exists to allow owners of Petkit Fresh Element Solo (D4)
feeders and Eversweet Max 2 Cordless (CTW3) fountains to operate their
own hardware without relying on Petkit's cloud infrastructure. Its
primary purpose is **interoperability** — enabling user-controlled,
local operation of devices the user has lawfully purchased.

The vulnerability findings documented in [SECURITY.md](SECURITY.md) and
[CVE_SUBMISSIONS.md](CVE_SUBMISSIONS.md) were identified during the
interoperability work and submitted to MITRE under standard
coordinated-disclosure process. Their public documentation is
incidental to the integration project, not its purpose.

## Statutory framework — Germany / EU

The project maintainer operates from Germany. The relevant statutes are
**§§ 69d, 69e Urheberrechtsgesetz (UrhG)**, transposing Article 6 of
[Directive 2009/24/EC (EU Software Directive)](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX%3A32009L0024)
into German law. Both permit observation, study, and decompilation of
program behavior when "indispensable to obtain the information
necessary to achieve the interoperability of an independently created
computer program with other programs," provided that the necessary
information was not otherwise readily available.

This integration is precisely such an independently created computer
program:

- Written from scratch in Python; no Petkit code is included or
  derived.
- Derived from passive observation of network protocol behavior the
  device sends in plaintext over the air, as documented in
  [SECURITY.md](SECURITY.md).
- Built to enable interoperability with hardware lawfully purchased by
  the maintainer and intended for use on networks they own.
- Petkit publishes no API documentation, SDK, or other authorized
  interoperability path for third-party local control. The information
  was not otherwise readily available.

## Statutory framework — United States (Github hosting, US users)

For the project's hosting on Github and for users in the United States,
the analogous protection is
[**17 U.S.C. § 1201(f) — Reverse Engineering**](https://www.law.cornell.edu/uscode/text/17/1201),
which permits circumvention of technological measures "for the sole
purpose of identifying and analyzing those elements of the program
that are necessary to achieve interoperability of an independently
created computer program with other programs."

## Security findings — disclosure framework

The security findings (F-01 through F-04) concern the device's
exposed-by-default cloud channel: plaintext HTTP, no authentication,
no integrity controls, observable from any network the device
communicates over. They were not obtained by defeating any encryption
or authentication that protected end-user data, and they were
submitted to [MITRE on 2026-04-23](CVE_SUBMISSIONS.md) for CVE-ID
assignment under the standard public-disclosure timeline.

Vendor (Petkit Network Technology Co., Ltd.) was not contacted prior
to submission because no security contact, `security.txt`, or
coordinated-disclosure program is published. The vendor remains free
to engage via this repository's issue tracker.

## Devices tested

All testing was performed on devices owned by the project contributors,
operating on networks they control. This integration is intended for
owners of similar devices on their own networks. It is **not**
intended for use against devices owned by others, and the project does
not condone unauthorized access.

## Right-to-repair alignment

This work aligns with the broader principle that consumers retain the
right to operate, repair, and integrate hardware they have lawfully
purchased — including the right to choose alternatives to vendor cloud
services as a condition of device functionality.

## Disclaimer

This document is **not legal advice**. The maintainer is not a lawyer
and makes no representation as to the legal correctness of the
analysis above. Users in jurisdictions outside the EU/US should
consult their own counsel before relying on it. The MIT License under
which the project is released explicitly disclaims warranties; this
legal-positioning document is provided in the same spirit —
informational, not binding.
