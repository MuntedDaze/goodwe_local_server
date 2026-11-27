# GoodWe Local Server for Home Assistant

Receive GoodWe telemetry over your LAN. GoodWe devices push their existing SEMS
messages to Home Assistant. You can optionally forward documented exchanges to
GoodWe so SEMS monitoring can continue alongside local telemetry.

| Hardware | Local telemetry |
| --- | --- |
| HK1000 single-phase HomeKit meter | Dedicated grid, solar and consumption sensors, plus v0.1 entities |
| HK3000 three-phase HomeKit meter | v0.1 phase and total entities |
| GW8KAU-DT inverter (`58000DTA`) | v0.1 DT inverter entities |

Keep the standard Home Assistant GoodWe integration installed for your local
inverter. The DNS G3 wire model `53000DSC` can pass documented traffic to SEMS
when forwarding is enabled. This integration does not publish local DNS G3
inverter entities.

## Install

Requires Home Assistant Core 2026.11.3 or later.

1. In HACS, add [MuntedDaze/goodwe_local_server](https://github.com/MuntedDaze/goodwe_local_server)
   as a custom repository of type **Integration**, then download it. For a manual
   installation, copy `custom_components/goodwe_local_server` from the release
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
generation and household consumption readings. A negative raw CT2 reading
remains visible on **L2 Export Power** and contributes zero generation.
Household consumption therefore includes any inverter standby draw imported
from the grid.

Existing total and phase entities keep their IDs and calculations. The
HK1000's combined totals include fields across both CTs. Use the dedicated
lifetime counters for grid and solar energy statistics once commissioning is
complete. Invalid or stale readings become unavailable.

## Configuration

| Option | Default | Behavior |
| --- | --- | --- |
| Local TCP port | 20001 | IPv4 LAN listener. Leave at 20001 for devices that use the factory endpoint. |
| Telemetry timeout | 90 seconds | Configurable from 15 to 3600 seconds. Set it above the regular reporting interval. |
| Forward to GoodWe SEMS | Off | Enables a bounded relay for supported traffic. |
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

With forwarding enabled, documented current and backfill message types and known
time synchronisation exchanges pass through for recognized models. The relay
preserves the bytes of accepted device messages and matching GoodWe replies.
It drops unknown messages, unsolicited remote-configuration or firmware
traffic, and undocumented replies. Cloud functions that depend on those
messages are outside the supported relay behavior.

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
| Stale or unavailable telemetry | Check last valid telemetry and last connection. Archived backfill and invalid frames do not make data fresh. Ensure the timeout exceeds the observed interval. |
| Upstream address blocked | Home Assistant is resolving only local or nonpublic destinations. Fix split DNS or use an explicit real address. |
| Cloud forwarding fails while local values update | Check upstream DNS, route, port and SEMS. The cloud health entity shows the outcome of the most recent exchange. |
| Unexpected HK1000 direction or house power | Compare raw L1/L2 readings with the expected wiring and the physical commissioning steps. |

Download **Diagnostics** from the integration menu for status and counters. The
export includes timestamps, models and counters. It omits serial numbers, IPs,
hostnames and packet payloads. Counters run from the last integration setup
or reload. A connection is linked to a device after its first valid,
recognized packet. Before that point errors appear in the aggregate counts.

## Development and evidence

See the [upstream audit and protocol notes](docs/audit.md) for the evidence,
lifecycle design and future source-selection interface.

```sh
python3.14 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
ruff check .
ruff format --check .
python -m pytest --cov=custom_components.goodwe_local_server --cov-report=term-missing
```

This fork retains the [MIT license](LICENSE) and the original work of
[Patrick Mead](https://github.com/patmead/goodwe_local_server). Thanks to
[Javison2](https://github.com/Javison2) for the HK1000 model support and
[Scott Leggett](https://smlx.dev/posts/goodwe-sems-protocol-teardown/) for the
protocol research.
