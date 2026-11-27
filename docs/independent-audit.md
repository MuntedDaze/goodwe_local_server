# Independent audit of v0.2

The final release review rechecked the audited implementation at `a3e748c`.
The timestamp and forwarding findings below describe that snapshot. The final
review found that a two-report backward-clock heuristic admitted paced old
reports, raw-envelope hashes missed re-encrypted duplicates, and devices
skipped by a shared SEMS backoff could still display a healthy forwarding
state. The current behavior is documented in the [README](../README.md) and
the [commissioning guide](commissioning.md).

Reviewed commit `13cf7e78ddb6a06211d245dd32a089f527f47f91` against fork baseline
`42880e2`, the published GoodWe protocol captures and parsers at
[smlx/goodwe `4787358`](https://github.com/smlx/goodwe/tree/4787358ab16d3b1d78d8b5aeede6bcc5b21e3e70/mitm),
the [protocol teardown](https://smlx.dev/posts/goodwe-sems-protocol-teardown/),
the upstream HK1000 discussions
([pull request 1](https://github.com/patmead/goodwe_local_server/pull/1),
[pull request 2](https://github.com/patmead/goodwe_local_server/pull/2)) and
Home Assistant Core 2026.11.3.

Everything below is verified by software only. **No GoodWe hardware, live SEMS
service or live Home Assistant installation was touched.**

## Findings and remediation

Each defect was reproduced by a test that failed against the reviewed commit,
unless the evidence column says otherwise.

### High

| ID | Finding and evidence | Affected code | Remediation |
| --- | --- | --- | --- |
| H1 | With forwarding on, intact device frames were never sent to SEMS if the model or message type was unknown, or the payload failed a local check. This included any DNS G3 other than model `53000DSC`, blank meter samples and short DT backfill. v0.1 and the reference relay forward all device traffic. | `server.py`, `protocol.py` | Every frame with valid framing and CRC is forwarded unchanged. Local decoding is independent. SEMS-to-device filtering is unchanged in strictness. |
| H2 | The published SEMS acknowledgement of a DNS G3 time sync (`0x01 0x03`, zero payload) was parsed as an invalid clock value and discarded. The exchange timed out and entered backoff. The published clock message type `0x01 0x16` was also refused for inverters, and an acknowledgement without a clock message counted as a failure. | `protocol.classify_reply`, `relay.py` | Acknowledgements are recognized, `0x01 0x16` is accepted for both families, and the follow-up clock message is optional. |
| H3 | Byte-identical repeats refreshed availability. Older reports replaced newer values. Home Assistant treats a `total_increasing` fall of more than 10% as a meter reset, which inflates energy statistics. | `server.py` | The last 64 reports per device are remembered. Repeats and older device timestamps do not update readings. A clock moved backwards is accepted after two advancing reports at least five seconds apart. |
| H4 | A strongly negative CT2 value was shown as 0 W solar generation, and household consumption was calculated from that zero. A [published HK1000 packet](https://github.com/patmead/goodwe_local_server/pull/1) has CT2 at -3898 W with its import counter advancing. | `sensor.py` | Below -100 W, solar generation and household consumption are unavailable. Raw CT2 is unchanged. |

### Medium

| ID | Finding and evidence | Affected code | Remediation |
| --- | --- | --- | --- |
| M1 | Retry backoff lived on one device connection. An upstream pull request reports a meter opening a new connection for each report, so every report retried immediately. | `relay.py` | Backoff is shared by the endpoint, doubles from 10 to 60 seconds, and is scoped to one device for reply failures. A rejection does not trigger it. A stale reused socket reconnects at once. |
| M2 | Clearing the upstream IP override in the options dialog silently restored the old address. | `config_flow.py` | The field now offers a suggested value instead of a default. |
| M3 | A device connection reset or idle timeout discarded frames already received and queued for SEMS. | `server.py` | Queued frames are drained after any ending except shutdown. |
| M4 | The queue held eight frames, so a burst of buffered reports lost most of them. | `server.py` | The queue holds 128 frames and the drain limit is 60 seconds. |

### Low

| ID | Finding | Remediation |
| --- | --- | --- |
| L1 | Failure to deliver a reply to a departed device was reported as an upstream failure. | Counted as `reply_undeliverable`; the cloud result stands. |
| L2 | An unexpected error ended forwarding for a connection without any record. | Recorded by exception type, and forwarding continues. |
| L3 | With eight DNS answers each address had 0.5 seconds to connect; the whole reply had 4 seconds. The reference relay allows 8 seconds to connect. | At least 2 seconds per address; 8 seconds connect budget and reply timeout. These values are not measured against live SEMS. |
| L4 | The default 90 second timeout is only 1.5 times the documented one minute cadence, and an upstream report describes routine 90 to 120 second gaps. (Evidence review.) | Default is 150 seconds. Saved options are kept. |
| L5 | One LAN host could occupy all 32 connection slots. | Limit of 16 per source address. |
| L6 | A buffer of frames was processed without yielding to the event loop. | Yields after each frame. |
| L7 | Forwarding failures and recovery were not logged. | One warning and one recovery message per device, without identifiers. |
| L8 | Local adapter addresses were compared as text, so another IPv6 notation passed the loop filter. | Addresses are normalized. |
| L9 | Checksummed but invalid frames on a new connection never appeared in that device's diagnostics. | Attributed to the known device. |
| L10 | Documentation did not warn that local-only mode cuts SEMS for every redirected device, or that a git checkout cannot be upgraded with `git pull`. | README and commissioning guide updated. |

## Checked without finding a defect

- Frame length bounds, truncation, fragmentation, coalesced frames, CRC byte
  order and AES block validation. Randomized frames against the listener and
  relay produced no unhandled errors and left no connections open.
- HK1000 offsets, signs and scaling against the published capture: 1557 W
  export, 2601 W generation, 243.8 V, and the three lifetime counters. The
  published currents are within 5% of power divided by voltage.
- All 78 legacy register definitions. A test now compares keys, names, classes,
  units, categories, default enablement, device identity and decoded values with
  output generated by running the unmodified v0.1 `sensor.py`.
- Unchanged v0.1 config entry, renamed entity IDs across restart, reload, busy
  port recovery, stale expiry, unload and integration removal.
- Public-address filtering, numeric-address connections and the in-flight loop
  guard.
- Diagnostics omit serial numbers, endpoints and payloads.
- MIT license and upstream attribution are intact.

## Test quality

The reviewed suite had good lifecycle coverage, but one genuine device family,
no DNS G3 traffic, backoff tested only inside a single connection, no
connection-loss or replay cases, and no repeatable legacy comparison. High line
coverage (96%) did not reveal any of the findings above. Most cloud tests
replace `RelayEndpoint.connect` with a loopback server, and HK3000 and DT
frames remain synthetic.

## Remaining uncertainties

| Topic | Uncertainty |
| --- | --- |
| Local-only operation | Whether an HK1000 keeps reporting without SEMS acknowledgements, including after a restart or on other firmware. |
| DNS G3 clock | No capture exists of the clock message SEMS sends to the inverter. The device's published `0x10` acknowledgement implies one. Check `cloud_replies_dropped`. |
| SEMS gaps | Messages received during a backoff pause or upstream failure are not stored or resent. Whether devices resend them when no acknowledgement arrives is unknown. |
| Other SEMS replies | Any reply type not documented is dropped, so some cloud features may not work. |
| Report timestamps | Ordering assumes the six-byte envelope field is the device clock. HK1000 and DNS G3 captures agree; HK3000 and DT are unverified. Watch `out_of_order_telemetry`. |
| CT wiring | A reversed grid clamp cannot be detected. A reversed CT2 reading small enough to stay above -100 W looks like standby, and its generation counter will not advance. |
| Energy counters | Meter reset, rollover and a zero counter in an otherwise valid frame are not characterized or filtered. |
| Current sensors | The "peak current" meaning of the legacy registers is provisional. |
| Loop guard | A device repeating an identical frame on a new connection while the first is still awaiting SEMS is counted as `relay_loop` and that connection is closed. |
| Trust boundary | The listener accepts any LAN client. Checksums and the fixed key do not authenticate devices. A rogue client can create up to 64 devices, which cannot be deleted individually. |
| Recorder volume | The **Data age** diagnostic updates every 10 seconds. Disable it if the history is unwanted. |

## Recommendation

**GO for merging into `main`** once CI passes on the final commit. **NO-GO for
relying on it on the live installation** until the
[physical commissioning steps](commissioning.md#required-physical-commissioning)
pass. The relay changes made by this audit have not themselves had a second
independent review.
