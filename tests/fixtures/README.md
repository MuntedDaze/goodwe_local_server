# GoodWe wire fixtures

These hexadecimal captures come from Scott Leggett's published [smlx/goodwe
tests](https://github.com/smlx/goodwe/tree/4787358ab16d3b1d78d8b5aeede6bcc5b21e3e70/mitm).
The telemetry, ACK and time sync fixtures use the upstream's anonymized serial.
The individual wire bytes are unchanged. The upstream fixture license is
[Apache 2.0](LICENSE.smlx-goodwe). The integration's code remains MIT.

## Additional captures added by the audit

From the same pinned smlx revision, with the same anonymized serial:

- `hk1000_time_sync_client_ack` and `dnsg3_time_sync_client_ack` are complete
  device frames from `mitm/outbound_test.go`.
- `dnsg3_time_sync_server_ack` is a complete SEMS reply from
  `mitm/inbound_test.go`.
- `dnsg3_metrics_body`, `dnsg3_backfill_body` and `dnsg3_time_sync_body` are
  published frame bodies from `mitm/outboundmarshal_test.go`, without the frame
  header and checksum. Tests rebuild those two parts, so they are not complete
  wire captures.

`v01_entities.json` is generated output, not a capture. It records every entity
created by the unmodified v0.1 `sensor.py` at commit `42880e2` for one synthetic
payload.
