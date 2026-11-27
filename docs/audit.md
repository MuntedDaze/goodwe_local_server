# Upstream audit

Audited fork baseline `42880e2` on `main`, including the v0.1 integration,
manifest, config flow, README, license and HK1000 support history. The protocol
evidence includes the teardown and recorded packet fixtures, plus GoodWe's
installation instructions. [Fork baseline](https://github.com/MuntedDaze/goodwe_local_server/tree/42880e2aace6b449ebc8f33aabdd919a278d4d55) ·
[Protocol research](https://smlx.dev/posts/goodwe-sems-protocol-teardown/) ·
[Reference captures](https://github.com/smlx/goodwe/tree/4787358ab16d3b1d78d8b5aeede6bcc5b21e3e70/mitm) ·
[HK1000/HK3000 wiring](https://www.goodwe.com.au/Ftp/EN/Downloads/User%20Manual/GW_HK1000%20%26%20HK3000_Quick%20Installation%20Guide-POLY.pdf)

| Area | v0.1 finding | Remediation |
| --- | --- | --- |
| TCP framing | A single read of up to 1024 bytes could split a frame, truncate a larger message or combine multiple messages. | Read the prefix, four-byte length and exact body for every frame. Limit frames to 4096 bytes. |
| Validation | Fixed slicing happened before length, identifier, CRC or AES alignment checks. Short register slices decoded as zero. | Validate bounds, the Modbus CRC-16, ASCII identity, model and payload shape before using registers. The CRC covers the entire prefix, length and body. Its wire byte order is big-endian from devices and little-endian from SEMS. |
| Connection ownership | The listener handle was discarded. Connections had no reliable cleanup, timeout or capacity limit. | One listener per entry, up to 32 device connections and 64 device records per runtime. Bound frame reads and socket drains. Close and cancel client, cloud and expiry work on unload and shutdown. |
| Multiple devices | Devices shared one sensor table keyed by serial, and setup and processing were driven directly by individual reads. | Associate each framed connection with its first validated identity. Accept independent concurrent connections and keep per-device state. |
| Forwarding | A DNS override could point upstream back to Home Assistant. DNS, connection or reply failures could block local state updates. | Commit local data first. The relay uses a queue of eight frames per connection, checked numeric upstream addresses, short DNS caching and bounded TCP operations. It tries new frames after failure. Duplicate frames in flight cannot spawn a relay loop. |
| Freshness | Per-entity polling consulted one last-seen map and could delay stale transitions. | Use a monotonic clock and a per-device expiry timer. Keep diagnostic entities visible when measurement entities become unavailable. |
| HA integration | Legacy config flow created an empty entry and the sensor platform started an unmanaged listener. | Keep the existing entry format and apply defaults to empty options. Add translated configuration, options with reload, diagnostics and HACS layout. |
| Dependencies | The manifest listed an unpinned hexdump package but the sensor imported `Crypto` implicitly. | Remove hexdump. Use Home Assistant Core's pinned `cryptography` dependency and the declared HA `network` integration. |
| Privacy | Debug warnings printed raw and decrypted buffers, peer addresses and identifiers. | Record classified errors and counts. Diagnostic exports redact endpoints and omit payloads, serials and peer addresses. |

## Protocol details

Device messages use `POSTGW`, a big-endian uint32 length and a two-byte type.
The total frame size is prefix length plus four length bytes plus the encoded
length plus three bytes. Cloud replies use `GW` and the same length rule.
The published protocol uses the fixed AES-128-CBC key and IV carried by its
envelope. CRC and encryption are insufficient to authenticate the device.
Treat the LAN and local firewall as the trust boundary.

Malformed prefixes, lengths and truncated frames close the connection to
re-establish framing on a new socket. A complete frame with a bad checksum or
invalid telemetry can be dropped while the next structurally framed message is
still read. Recognized data type `0x04` supplies local telemetry. Type `0x45`
may represent cached or catch-up telemetry and is eligible for relay only. The
listener uses its local receipt time for freshness. Cadence is the most recent
interval between accepted readings, excluding subsecond bursts and gaps longer
than the stale threshold.

The relay sends one request at a time on each upstream connection. It allows
documented ACK/NACK and matching time sync responses with valid lengths, CRC,
identity and payload shapes. A HomeKit time sync request can receive an ACK
followed by a clock response. Documented device clock acknowledgements can be
forwarded without waiting for a reply. The relay passes the observed two-byte
keepalive during an exchange and drops unknown messages. It closes a failed upstream exchange, applies
a short reconnect backoff and keeps the local socket reading. A later frame can
open a new cloud connection. Saturation drops new relay work while retaining
local reads.

## Register evidence and compatibility

The legacy entity registry keys, device identities, raw diagnostic semantics,
big-endian signed values, scales and units are retained. Opaque register
slices are available only if the payload contains the whole field. The DT
inverter test exercises the legacy measurement layout, including power, 0.1 kWh
energy, deci-degrees and hundredths of a Hz. Complex or unknown DT register
meanings remain as upstream published them.

The documented HK1000 uses CT1 on the grid connection and CT2 on inverter
output. GoodWe specifies the grid clamp orientation from house (K) to grid (L). The published capture identifies the following offsets in the
112-byte decrypted payload. It shows 1557 W grid export and 2601 W solar
generation independently from the combined power field.

| Offset | Wire representation | HK1000 interpretation |
| --- | --- | --- |
| `0x4b` | signed int32, W | CT1 positive export, negative import |
| `0x4f` | signed int32, W | CT2 signed inverter power |
| `0x07` | signed int32, 0.01 kWh | Lifetime grid export |
| `0x1f` | signed int32, 0.01 kWh | Lifetime grid import |
| `0x0d` | signed int32, 0.01 kWh | Lifetime solar generation |
| `0x39` | signed int16, 0.1 V | Grid voltage under the legacy register map |
| `0x41`, `0x45` | signed int16, 0.01 A | Legacy CT peak current readings. Treat the peak interpretation as provisional. |

Offset `0x19` includes grid export and solar generation. Offset `0x31`
represents another combined quantity with incomplete documentation. The new
HK1000 grid energy entities use the dedicated CT1 counters. Negative dedicated
energy counters are unavailable on the new entities. Counter overflow, meter
reset behavior and calibration on the target installation require commissioning.

## Future inverter-to-HK1000 source selection

A future selector can consume the standard GoodWe inverter's AC output power
and the HK1000's **Solar generation power**. Both candidate states need units
of W, a consistent generation-positive convention, a last valid observation and
an explicit valid or unavailable status. The HK1000 exposes its last valid
telemetry timestamp, observed interval and availability through Home Assistant.
Use the standard inverter integration's own availability and measurement
timestamp. Define precedence and a sustained recovery interval before switching
sources. Prevent stale or unknown values from becoming zero. For energy
statistics, keep independent lifetime counters with their own reset histories.
An automatic source selector would need an explicit strategy for transitions
between counters with different baselines.
