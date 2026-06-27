# SolaX Ant Controller

Battery-aware controller for a Braiins OS miner powered by a SolaX inverter setup.

The controller polls SolaXCloud, reads battery state of charge and battery charge or discharge power, then adjusts the Braiins OS power target. It is designed to preserve the battery first: mining stays paused overnight or whenever the battery is not charging enough, and resumes only after the battery has recovered.

## Behavior

- Polls SolaXCloud every 5 minutes by default.
- Pauses mining when battery SOC is below `75%`.
- Keeps mining paused when SOC is at least `75%` but the battery is not still charging.
- Resumes mining when SOC is at least `75%` and the battery is charging.
- Ramps miner power step by step instead of jumping straight to full power.
- Avoids Braiins power-target range errors by learning or using configured min/max watt limits.
- Uses Braiins `pause`, not `stop`, for battery protection so BOSminer can resume cleanly.

## Requirements

- Python 3.9+
- `uv`
- SolaXCloud token and Wi-Fi serial number
- Braiins OS miner API URL and credentials

## Install

```bash
uv sync --all-groups
```

## Run

```bash
export SOLAX_TOKEN_ID="your-solax-token"
export SOLAX_WIFI_SN="your-solax-wifi-sn"
export BRAIINS_BASE_URL="http://192.168.1.164"
export BRAIINS_USERNAME="root"
export BRAIINS_PASSWORD="your-password"

uv run solax-ant-controller
```

For a dry run that logs decisions without changing miner state:

```bash
export MINER_DRY_RUN="true"
uv run solax-ant-controller
```

## Configuration

Required environment variables:

| Variable | Description |
| --- | --- |
| `SOLAX_TOKEN_ID` | SolaXCloud API token. |
| `SOLAX_WIFI_SN` | SolaX Wi-Fi dongle serial number. |
| `BRAIINS_BASE_URL` | Miner base URL, for example `http://192.168.1.164`. |
| `BRAIINS_USERNAME` | Braiins OS username. |
| `BRAIINS_PASSWORD` | Braiins OS password. |

Useful optional environment variables:

| Variable | Default | Description |
| --- | ---: | --- |
| `SOLAX_API_URL` | `https://global.solaxcloud.com/api/v2/dataAccess/realtimeInfo/get` | SolaXCloud realtime endpoint. |
| `SOLAX_POLL_INTERVAL` | `300` | Control loop interval in seconds. |
| `SOLAX_TIMEOUT` | `600` | SolaX request timeout in seconds. |
| `BRAIINS_TIMEOUT` | `10` | Braiins request timeout in seconds. |
| `MINER_ENABLE_CONTROL` | `true` | Set to `false` to log decisions without applying them. |
| `MINER_DRY_RUN` | `false` | Uses the Braiins client dry-run mode for writes. |
| `MINER_REFERENCE` | `1` | Braiins relative target reference. |
| `MINER_FULL_PERCENT` | `100` | Maximum target percent the ramp may reach. |
| `MINER_START_SOC_PERCENT` | `75` | Battery SOC needed before mining can resume. |
| `MINER_START_PERCENT` | `50` | Initial target percent when mining resumes. |
| `MINER_RAMP_UP_PERCENT_STEP` | `10` | Percent added each successful loop while still charging. |
| `MINER_BATTERY_CHARGE_RESERVE_W` | `0` | Charging watts to preserve before allocating surplus to the miner. |
| `MINER_MIN_POWER_W` | unset | Optional known Braiins minimum power target in watts. |
| `MINER_MAX_POWER_W` | unset | Optional known Braiins maximum power target in watts. |
| `MINER_RATED_POWER_W` | unset | Optional nominal power used to convert watts to relative percent. |
| `SOLAX_LOG_LEVEL` | `INFO` | Python logging level. |
| `SOLAX_LOG_FILE` | `./solax-miner-controller.jsonl` | JSONL log file path. |

If `MINER_MIN_POWER_W`, `MINER_MAX_POWER_W`, and `MINER_RATED_POWER_W` are not configured, the controller can learn them from Braiins range errors such as:

```text
new power target '1750' is out-of-range (min: Some(2414), max: Some(6435))
```

After learning those limits, later decisions avoid asking Braiins for impossible targets.

## Development

Run tests:

```bash
uv run pytest
```

Build package distributions:

```bash
uv build
```

The GitHub Actions workflow in `.github/workflows/ci.yml` installs from `uv.lock`, then runs tests and builds distributions on pushes and pull requests.

## GitHub Releases

GitHub Packages does not provide a Python/PyPI package registry. The workflow uploads the built wheel and source distribution to a GitHub Release when you push a version tag:

```bash
git tag v0.1.0
git push origin v0.1.0
```

After the workflow finishes, install the wheel from the release assets:

```bash
uv tool install \
  https://github.com/matusso/solax-ant-controller/releases/download/v0.1.0/solax_ant_controller-0.1.0-py3-none-any.whl
```

Or install directly from the tagged Git repository:

```bash
uv tool install "git+https://github.com/matusso/solax-ant-controller.git@v0.1.0"
```

For a real Python registry, publish to PyPI or a private PyPI-compatible registry such as GitLab Package Registry, AWS CodeArtifact, Azure Artifacts, or a self-hosted server.
