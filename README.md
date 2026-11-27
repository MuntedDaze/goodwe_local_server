# GoodWe Local Server for Home Assistant

Receive GoodWe telemetry over your LAN. GoodWe devices push their existing SEMS
messages to Home Assistant. You can optionally relay those messages to GoodWe
so SEMS monitoring can continue alongside local telemetry.

| Hardware | Local telemetry |
| --- | --- |
| HK1000 single-phase HomeKit meter | Dedicated grid, solar and consumption sensors, plus v0.1 entities |
| HK3000 three-phase HomeKit meter | v0.1 phase and total entities |
| GW8KAU-DT inverter (`58000DTA`) | v0.1 DT inverter entities |

Keep the standard Home Assistant GoodWe integration installed for your local
inverter. This integration does not publish local DNS G3 inverter entities.

> [!WARNING]
> Forwarding is **off** by default. Every GoodWe device that uses your DNS
> override, including a DNS G3 inverter you did not intend to redirect, then
> stops reaching SEMS. Either scope the override to the meter, or enable
> **Forward to GoodWe SEMS** so that all redirected devices are relayed.

## Install

Version 0.2.0 requires Home Assistant Core 2026.11.3 or later.

1. In HACS, add [MuntedDaze/goodwe_local_server](https://github.com/MuntedDaze/goodwe_local_server)
   as a custom repository of type **Integration** after v0.2.0 is available on
   `main`, then download it. For a manual installation, copy
   `custom_components/goodwe_local_server` from the version you are installing
   into Home Assistant's `config/custom_components/`.
2. Restart Home Assistant. In **Settings → Devices & services → Add integration**,
   select **GoodWe Local Server**.
3. Permit LAN TCP traffic from the GoodWe equipment to Home Assistant on port
   `20001`. In your local DNS resolver, override `tcp.goodwe-power.com` to
   the Home Assistant host for the intended GoodWe clients. Make sure the devices
   are using that resolver. Keep the listener off the public internet.
4. Wait for the next device report. Home Assistant adds the device and its
   entities automatically. Open the integration's **Configure** dialog to set
   the stale threshold, enable forwarding, or adjust the HK1000 solar offset.

See [upgrade, rollback and physical commissioning](docs/commissioning.md) before
upgrading an existing v0.1 installation or switching dashboard inputs.

## HK1000 readings

The default mapping follows GoodWe's wired CT1 grid and CT2 inverter
installation. Compare the readings with a known load and independent inverter
output during commissioning. Channel 3 is unused on the HK1000.

| Sensor | Interpretation |
| --- | --- |
| Signed grid power | CT1. Positive means import, negative means export. |
| Grid import and export power | The two nonnegative directions of signed grid power. |
| Solar generation power | Nonnegative generation from CT2. The optional offset filters positive readings at or below the configured threshold. |
| Household consumption power | Solar generation plus signed grid power. Unavailable when those inputs would imply negative household consumption. |
| Lifetime grid import and export energy | Dedicated CT1 counters in kWh. |
| Lifetime solar generation energy | Dedicated CT2 generation counter in kWh, based on the published HK1000 measurement comparisons. |
| Grid voltage | CT1 line voltage in V. |
| CT diagnostics | The preserved L1/L2 export power and peak current entities show raw channel behavior, including signed solar readings and nighttime offsets. |

The default offset is 0 W. Set an offset after measuring a repeatable nighttime
bias. The raw CT2 reading remains visible, and the offset affects the new solar
generation and household consumption readings. CT2 between -100 and 0 W
contributes zero generation. This fixed allowance covers possible standby draw
or noise, which is already included in grid import. It can also mask a small
wiring or sign error in the derived values. Below -100 W, **Solar generation
power** and **Household consumption power** become unavailable. Compare **L2
Export Power**, which always exposes raw CT2 while telemetry is fresh, with the
inverter. One published HK1000 installation reported -3898 W on CT2.
The integration cannot detect a reversed grid clamp, so verify the direction of
**Signed grid power** during commissioning.

Existing total and phase entities keep their IDs and calculations. The
HK1000's combined totals include fields across both CTs. Use the dedicated
lifetime counters for grid and solar energy statistics once commissioning is
complete. The dedicated counters become unavailable when their values fall
below their highest valid readings since integration setup. Other values keep
updating, and the `invalid_lifetime_energy` counter records the sample. If a
physical meter reset or rollover is confirmed, check the recorder statistics
and reload the integration to establish a new baseline. Legacy registers
retain their original behavior.

Local freshness uses monotonic receipt time. The device's calendar field has
no known time zone. Invalid dates and dates more than one day ahead of UTC are
rejected as implausible. A report at the same device time with the same decoded
data is a repeat even if its encryption envelope differs. Explicit backfill
never refreshes readings.

Older timestamps and advances that differ from elapsed receipt time by over
two minutes are held back. After telemetry goes stale, a clock change can
recover on three advancing reports spaced at least five seconds apart,
progressing at approximately the receipt cadence, with nondecreasing lifetime
counters relative to the last accepted report. This also works without SEMS.
This is a heuristic for unmarked reports. A paced replay with unchanged energy
can resemble live traffic, and replay history is reset on integration reload.

## Configuration

| Option | Default | Behavior |
| --- | --- | --- |
| Local TCP port | 20001 | IPv4 LAN listener. Leave at 20001 for devices that use the factory endpoint. |
| Telemetry timeout | 150 seconds | Configurable from 15 to 3600 seconds. Allows a missed 60-second report and 30 seconds of margin. A 30-second HK1000 can go up to five reporting intervals before measurements expire. Saved timeout options are retained. |
| Forward to GoodWe SEMS | Off | Enables the bounded TCP relay. |
| Upstream hostname and TCP port | `tcp.goodwe-power.com`, 20001 | Set the endpoint offered to Home Assistant by DNS. |
| Upstream IP override | Blank | Optional public IPv4 or IPv6 address, taking precedence over the hostname. |
| HK1000 solar offset | 0 W | Optional 0–50 W threshold for small positive CT2 readings. |

When cloud forwarding is enabled, the Home Assistant resolver must reach the
real upstream address. Prefer a DNS override scoped to the GoodWe clients so
Home Assistant can use its normal public DNS route. Where the override applies
to Home Assistant as well, query an unaffected trusted resolver from a machine
that can reach it. These commands use Cloudflare's public DNS.

```sh
dig @1.1.1.1 tcp.goodwe-power.com A +short
dig @1.1.1.1 tcp.goodwe-power.com AAAA +short
```

If the network intercepts DNS, query from another network or use a trusted
DNS-over-HTTPS resolver. Enter the returned address as the upstream IP
override and recheck it if forwarding stops. GoodWe can change its addresses.
The relay checks resolved addresses before connecting, including explicit
overrides. It blocks private or reserved results and local adapter addresses.
An in-flight frame guard also detects traffic routed back to the listener.

### Traffic and acknowledgements

With forwarding enabled, every device message with intact framing and checksum
is eligible for forwarding to SEMS unchanged. This includes unknown models,
unknown message types and payloads rejected for local use. Corrupt frames are
dropped.

Traffic from SEMS is restricted. Only acknowledgements, rejections and clock
messages that match the identity of the message just sent are returned to the
device, plus the two-byte keepalive. Remote-configuration, firmware and any
other undocumented replies are discarded and counted as
`cloud_replies_dropped`. Cloud functions that depend on them do not work through
this relay.

After a retryable failure the relay pauses for 10 seconds, doubling to at most 60 seconds
while failures continue. The pause persists across new device connections. DNS
and connection failures pause all devices; a reply failure pauses only the
affected device. Diagnostics keep the underlying reason when a report is
skipped during a pause. SEMS gaps can occur after a failure or backoff unless
the device resends the data. Upstream work is bounded by a queue of 128 waiting
frames per connection and a 60-second drain window after a connection ends.

Without forwarding, Home Assistant only consumes local telemetry. The
protocol research contains genuine cloud acknowledgements, but the requirements
for continued reporting across HK1000 firmware versions and offline restarts
remain unverified. The listener does not generate acknowledgements or clock
responses. Local reception runs independently of DNS resolution, upstream
connections and cloud reply processing.

### Troubleshooting

| Symptom | Check |
| --- | --- |
| Listener cannot start | Check the configured port and whether another integration or container already binds it. |
| No device appears | Verify device-side DNS, TCP port reachability, VLAN/firewall rules and that Home Assistant receives a known GoodWe frame. |
| Stale or unavailable telemetry | Check last valid telemetry and last connection. Backfill, invalid, repeated and out-of-order frames do not make data fresh; check the packet counters. Ensure the timeout exceeds the observed interval. |
| No recovery after a device clock reset | Check `invalid_telemetry_timestamp`, `future_telemetry_timestamp` and `telemetry_clock_jump`. Confirm the meter clock and verify its latest readings before reloading the integration. |
| Solar and household power unavailable while grid power updates | CT2 reads below -100 W (check clamp direction and placement), or the inputs imply negative household consumption. |
| Upstream address blocked | Home Assistant is resolving only local or nonpublic destinations. Fix split DNS or use an explicit real address. |
| Cloud forwarding fails while local values update | Check upstream DNS, route, port and SEMS. The cloud health entity shows the outcome of the most recent exchange, and the Home Assistant log records when forwarding fails and recovers. |
| Unexpected HK1000 direction or house power | Compare raw L1/L2 readings with the expected wiring and the physical commissioning steps. |

Download **Diagnostics** from the integration menu for status and counters. The
export includes timestamps, models and counters. It omits serial numbers, IPs,
hostnames and packet payloads. Counters run from the last integration setup
or reload. A connection is linked to a device after its first valid,
recognized packet. Before that point errors appear in the aggregate counts.

## Development and evidence

See the [upstream audit and protocol notes](docs/audit.md) for the evidence,
lifecycle design and future source-selection interface, and the
[independent v0.2 audit](docs/independent-audit.md) for the second review's
findings and remaining uncertainties.

On Windows the tests also run natively with Python 3.14.2 or later. Create the
virtual environment, install `requirements-dev.txt` and run the same commands.

On Linux, or as the setup script of a Codex cloud environment, run
`bash scripts/setup.sh`. It creates `.venv` with Python 3.14.2 or later and
installs the pinned dependencies. Then:

```sh
. .venv/bin/activate
ruff check .
ruff format --check .
python -m pytest --cov=custom_components.goodwe_local_server --cov-report=term-missing
```

This fork retains the [MIT license](LICENSE) and the original work of
[Patrick Mead](https://github.com/patmead/goodwe_local_server). Thanks to
[Javison2](https://github.com/Javison2) for the HK1000 model support and
[Scott Leggett](https://smlx.dev/posts/goodwe-sems-protocol-teardown/) for the
protocol research.
