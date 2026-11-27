# Upgrade, rollback and commissioning

## Upgrade from manually installed v0.1

1. Create a Home Assistant backup. Keep a separate copy of the existing
   `config/custom_components/goodwe_local_server` directory, including any
   edits to `sensor.py`.
2. Read your existing relay setting, upstream endpoint and ports from that
   copy. They were Python constants in v0.1 and must be entered manually in the
   new options. Save these values privately.
3. Stop Home Assistant and replace the component directory with the contents of
   the new `custom_components/goodwe_local_server` directory. The repository
   layout changed: if v0.1 was installed as a git checkout inside
   `config/custom_components/goodwe_local_server`, do **not** `git pull` there,
   because the integration files now sit two directories deeper. Start Home
   Assistant. Keep the existing GoodWe Local Server config entry and device
   and entity registries. The entry initially uses local-only defaults unless
   options already exist.
4. Restore forwarding immediately if any redirected device needs SEMS, in
   **Settings → Devices & services → GoodWe Local Server
   → Configure** if it was enabled before the upgrade. Set the upstream port
   and valid hostname or address using the [DNS instructions](../README.md#configuration).
   Restore any custom listener port. Saving reloads the entry.
5. Allow the next valid packets to arrive. Check your previous entity IDs,
   custom names, automations and energy statistic sources before switching to
   any of the new HK1000 entities. Existing statistics remain linked to the
   original sensors. New statistics start when the new entities are used.

## Rollback

v0.1 declares the `simple-hexdump` requirement, so Home Assistant may need
internet access to install it on the first start after rolling back. Entities
that only exist in v0.2 remain in the registry as unavailable; delete them if
they are not wanted. Their recorded statistics are not merged into the original
sensors.

1. Stop Home Assistant. Restore the saved component directory, including any
   v0.1 relay edits, and restore any DNS changes made during commissioning.
2. Start Home Assistant with the same config entry and verify the original
   entity IDs, local values and SEMS behavior. The original code reads relay
   constants from `sensor.py` and ignores saved v0.2 options.
3. If you need to restore the exact pre-upgrade registry and database, restore
   the pre-upgrade Home Assistant backup. A full restore also reverts changes
   made since that backup.

## Required physical commissioning

Perform these checks on the real installation after independent review and
before relying on the new sensors or their statistics.

1. Record pre-upgrade local readings, existing entity IDs and the timestamp of the
   latest SEMS upload. Record the wiring of CT1 and CT2 from installer records
   or labels. Have a qualified installer check physical wiring where needed.
2. With PV idle, compare a known load step with an independent grid reading.
   The change should be positive in signed grid power and grid import power.
   Grid export should remain zero when importing. Compare raw CT2 readings for
   several nighttime reporting intervals before choosing any solar offset.
3. In daylight, compare CT2 generation with the standard local inverter
   integration. During export, confirm signed grid power goes negative and
   grid export power increases. Compare household consumption against the
   independent readings. Verify grid voltage and check that the two current
   diagnostics react to the expected CTs.
4. Compare the changes in all three dedicated lifetime counters with measured
   import, export and inverter generation across a sufficiently long interval.
   Include a period of import and one of export. Check energy recording across
   a device restart before assigning the sensors to the Energy Dashboard.
5. With forwarding enabled, confirm continued SEMS updates. Check that the
   Home Assistant and SEMS receipt timestamps continue moving through multiple
   reporting cycles and after a device restart. Verify the endpoint is reached
   without a DNS loop.
6. Exercise both local-only operation and a controlled upstream internet outage
   that leaves LAN DNS and Home Assistant working. Observe continuous HK1000
   reports for several intervals, including after a meter restart if practical.
   Record whether the meter needs a cloud ACK or time sync to keep sending and
   whether SEMS resumes after restoration. Capture only authorized traffic and
   sanitize captures before sharing them.
7. Before redirecting a DNS G3 inverter, or if it already follows the override,
   enable forwarding and confirm that SEMS keeps receiving its data over at
   least a full day, including the morning restart. Download diagnostics and
   record `cloud_success`, `reply_timeout`, `cloud_replies_dropped` and
   `unsupported_packets`. A steadily rising `cloud_replies_dropped` means SEMS
   sends replies this relay does not recognize. Remove the override for that
   inverter if SEMS stops updating.
8. Over at least 24 hours, record the observed reporting interval and the
   `duplicate_telemetry`, `out_of_order_telemetry`, `relay_loop` and
   `queue_full` counters. All should stay at or near zero. Raise the stale
   threshold if healthy reporting gaps exceed it.
9. Observe an integration reload and Home Assistant restart. Confirm recovery
   with the same IDs. Stop device reporting for longer than the stale threshold
   and confirm measurements become unavailable, then resume with fresh data.
   Test any connected HK3000 or legacy DT inverter on its own reporting cycle.

The automated suite uses published HK1000 and DNS G3 captures, synthetic model
fixtures, loopback TCP and a disposable Home Assistant instance. Live-device
and site internet-outage verification are commissioning checks.
