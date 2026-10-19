# tak-feeder-nysse

> [!WARNING]
> **Work in progress. Not recommended for use.** This fork is being improved
> and has not been tested against a live TAK server yet. Things may change or
> break without notice.

TAK feeder for the real-time buses of Nysse (Tampere regional transport).
It polls the Journeys API and sends the buses to a TAK server as CoT.

Fork of [sgofferj/tak-feeder-nysse](https://github.com/sgofferj/tak-feeder-nysse)
by Stefan Gofferje, who now maintains the same functionality as
[trakbridge-plugin-nysse](https://github.com/sgofferj/trakbridge-plugin-nysse).

## What it sends

| Field | Value |
|---|---|
| UID | `nysse-<vehicle>` (unchanged from upstream) |
| Callsign | `NYSSE <line>`, e.g. `NYSSE 3` |
| Type | `a-f-G-E-V-C-M-H` (bus) |
| Speed / course | m/s (converted from km/h) and degrees |

```
Line: 3
To: Hervanta
Next: Keskustori H 14:18
Delay: +2 min
Vehicle: 56920_13
Source: Journeys API (ITS Factory, City of Tampere), CC BY 4.0
#NYSSE #bus
```

Stop names are loaded in one request and refreshed daily. The API is read
every `FETCH_INTERVAL` seconds, never faster than every 3 s (the API publishes
no limit). A moving bus is sent every `SEND_MOVING_INTERVAL` seconds, a stopped
one every `SEND_STATIONARY_INTERVAL` seconds.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `COT_URL` | `udp://239.2.3.1:6969` | TAK server, e.g. `ssl://takserver:8089` |
| `PYTAK_TLS_CLIENT_CERT` / `PYTAK_TLS_CLIENT_KEY` | | Client certificate and unencrypted key (PEM). Old names `CLIENT_CERT` / `CLIENT_KEY` still work |
| `PYTAK_TLS_DONT_VERIFY` | `0` | `1` = do not verify the server certificate |
| `NYSSE_LINE_FILTER` | (all lines) | Line numbers, wildcards allowed: `3,60,6*` |
| `FETCH_INTERVAL` | (send interval) | Seconds between API reads, min 3 |
| `SEND_MOVING_INTERVAL` | `5` | Seconds between sends of a moving bus, min 1 (old name `UPDATE_INTERVAL`) |
| `SEND_STATIONARY_INTERVAL` | `60` | Seconds between sends of a stopped bus, min 30 (old name `REFRESH_INTERVAL`) |
| `STATIONARY_SPEED` | `0.5` | Below this speed (m/s) a bus counts as stopped |
| `STALE_SECONDS` | `180` | Marker lifetime; always at least 3 send intervals |
| `TIMEZONE` | `Europe/Helsinki` | Time zone of the next stop time |
| `SOURCE_LINE` | see above | Attribution line in the remarks |
| `HEALTH_MAX_ERRORS` | `3` | Failed polls in a row before the container is unhealthy |
| `DEBUG` | `0` | Debug logging |

## Running

Image for amd64 and arm64: `ghcr.io/street3r/tak-feeder-nysse:latest`.

```yaml
services:
  feeder-nysse:
    image: ghcr.io/street3r/tak-feeder-nysse:latest
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./certs:/data:ro
```

The container runs as uid 10001; the key must be readable by it.

## Data

Journeys API by ITS Factory / City of Tampere, licensed
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The source line is
in every marker's remarks.

## Credits and changes

Original work © Stefan Gofferje:
[tak-feeder-nysse](https://github.com/sgofferj/tak-feeder-nysse) and
[trakbridge-plugin-nysse](https://github.com/sgofferj/trakbridge-plugin-nysse),
used with his permission.

Changes in this fork (Street3r):

- 2026-10-13
  - **Fixed:** without `NYSSE_LINE_FILTER` only seven hard-coded lines were
    shown; the default is now all lines.
  - **Fixed:** wildcard filters (`6*`) were sent to the API as such; they are
    now matched in the feeder.
  - **Fixed:** the next stop time was always `--:--` (the API gives the
    arrival time, not the departure time).
  - One vehicle with bad data no longer stops the whole round.
  - Remarks: line, destination, next stop with time, delay, data source,
    `#NYSSE #bus`. Bus CoT type `a-f-G-E-V-C-M-H` as in trakbridge-plugin-nysse.
  - Stop names in one daily request instead of one request per stop.
  - Unmoved buses are re-sent every `REFRESH_INTERVAL` instead of every poll.
  - Standard variable names with the old ones as aliases; health check;
    unprivileged container; tests and CI.
- 2026-10-15
  - Every event of a send round reaches TAK. pytak's send queue holds 100
    events and dropped the oldest when full, so rounds of more than 100
    lost the rest.
- 2026-10-19
  - Send intervals shared by all feeders: a moving bus is sent every
    `SEND_MOVING_INTERVAL` (5 s), a stopped one every
    `SEND_STATIONARY_INTERVAL` (60 s) and at once when it stops. The
    latest position is re-sent at that rate even when the source has
    nothing new. The old variable names still work.
  - The API is read every `FETCH_INTERVAL` (min 3 s); buses that leave the
    API response are no longer sent.

## License

GNU General Public License v3.0 or later, see [LICENSE](LICENSE).
