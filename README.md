# Appliance utilization

Local web app that charts three Meraki Dashboard series for the appliances in one organization:

- **Utilization %**, the 20-minute history of CPU and peak bandwidth use per appliance.
- **Performance score** (0–100), fetched in many short windows so it becomes a time series at the resolution you choose.
- **Uplink usage**, sent and received throughput per uplink (wan1, wan2, cellular) for each appliance's network.

The appliance list is every device returned by `getOrganizationDevices` with `productTypes=["appliance"]`. Pick all models or a subset.

The app is a single Python process. `server.py` serves one HTML, one CSS, and one JavaScript file from `static/` using the Python standard library, and listens on `127.0.0.1` only. It reads Dashboard data with the official `meraki` SDK and never changes configuration.

## Important notice

This project is provided **as is**, without warranty of any kind, as stated in the [MIT License](LICENSE). It is intended for lab, educational, and evaluation use, and as an example to learn from.

- It is not production software. It is not suitable for production environments and is not supported.
- Test it in your own environment and adapt it to your requirements and security policies before relying on it for anything.
- Use a read-only Dashboard API key, and only with organizations you are authorized to access.
- The authors and contributors are not liable for any damage, data loss, service disruption, or other consequences of using or misusing this project.

## Requirements

- Python 3.11 or later (tested on 3.14). On Windows the `py` launcher comes with the python.org installer. macOS ships an older `python3`; install a current one from python.org or Homebrew. On Debian or Ubuntu, also install `python3-venv`. Check with `python3 --version` (or `py -3 --version` on Windows) before installing; an older Python silently gets an older Meraki SDK.
- Git, to clone the project (or download the ZIP from the repository page).
- A Meraki Dashboard API key with **read-only** access to the organizations you want to chart. See [Generate a Dashboard API key](https://documentation.meraki.com/General_Administration/Other_Topics/Cisco_Meraki_Dashboard_API#Enable_API_access).
- Outbound HTTPS to `api.meraki.com`.
- A web browser on the same machine.

## Install

Get the code and open a terminal in the project folder:

```bash
git clone <repository-url> appliance_performance_compare
cd appliance_performance_compare
```

Then run these once. They create a virtual environment in `.venv` and install the dependencies into it.

Windows (PowerShell):

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

macOS or Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

The commands call the virtual environment's Python directly, so you do not need to activate it.

## Set the API key

The app reads the key from the environment variable `MK_CSM_KEY` and nowhere else. There is no `.env` file, secrets file, or key field in the page, and the app never writes the key to disk.

Current terminal only (recommended). The key is gone when you close the terminal.

```powershell
# Windows PowerShell
$env:MK_CSM_KEY = "<your-read-only-key>"
```

```bash
# macOS / Linux
export MK_CSM_KEY="<your-read-only-key>"
```

To keep the key out of your shell history, type it at a prompt instead:

```powershell
$env:MK_CSM_KEY = [System.Net.NetworkCredential]::new("", (Read-Host "Dashboard API key" -AsSecureString)).Password
```

```bash
# bash
read -rs -p "Dashboard API key: " MK_CSM_KEY && echo && export MK_CSM_KEY
# zsh (macOS default)
read -rs "MK_CSM_KEY?Dashboard API key: " && echo && export MK_CSM_KEY
```

Permanent, for your user account. This stores the key in plain text in your user environment or shell profile, so only do it on a machine you trust.

- Windows: `setx MK_CSM_KEY "<your-read-only-key>"`, then open a **new** terminal. `setx` does not change the terminal it runs in. A terminal inside an editor such as Cursor or VS Code only sees the key after you restart the editor.
- macOS / Linux: add the `export` line to `~/.zshrc` or `~/.bashrc`, then open a new terminal.

Check the key is set without printing it:

```powershell
[bool]$env:MK_CSM_KEY
```

```bash
[ -n "$MK_CSM_KEY" ] && echo set || echo missing
```

## Launch

Windows:

```powershell
.\.venv\Scripts\python.exe server.py
```

macOS or Linux:

```bash
.venv/bin/python server.py
```

The terminal prints the address that bound, for example `http://127.0.0.1:8765/`, and the browser opens on it about half a second later. Ctrl+C stops the app.

| Option | Effect |
|--------|--------|
| *(none)* | Tries port 8765, then each port up to 8814, and says which ports were busy. |
| `--port 8800` | A port inside 8765–8814 is tried first, then the rest of that range. |
| `--port 9000` | A port outside 8765–8814 is tried once only. Accepted ports are 1024–65535. |
| `--no-browser` | Starts without opening the browser. Open the printed address yourself. |

If `MK_CSM_KEY` is missing at startup, the page still loads and the terminal prints a warning. Set the key and restart.

## Use

1. Pick an **Organization**, then tick **All models** or the models you want under **Appliances**.
2. Under **Time**, pick a window (2 hours to 28 days), then a **Performance** and an **Uplink** resolution.
3. Set **Highlight** to the threshold used by the count cards, the table filter, and the chart line.
4. Check the call count and time estimate in the sidebar, then click **Fetch**.

### Window and resolution

| Series | Resolution | Calls |
|--------|-----------|-------|
| Utilization | Fixed at 20 minutes by the API | 1 for the organization |
| Performance score | 30 minutes up to the whole window, at most 14 days per point | 1 per appliance per point |
| Uplink usage | 1 minute, 5, 10, or 30 minutes, 1 hour, or 1 day | 1 per network |

The window is capped at 28 days because the performance score and uplink history only look back 30 days. The resolution pickers list only values the API accepts for the chosen window, from finest to coarsest, and show the number of points for each. The sidebar shows the total call count.

- A performance resolution that needs more than 2,000 calls is shown but disabled. Pick a coarser one or fewer appliances.
- An uplink resolution that would draw more than 2,016 points is disabled.

While a fetch runs, the page shows `done / total` for utilization, each network, and each appliance, and the terminal prints one line per item plus a summary when it finishes. **Cancel fetch** stops it after the calls already in flight.

### Page

- **Count cards** for all appliances, utilization at or above the threshold, score at or above the threshold, and appliances with no utilization history. Click a card to filter the table.
- **Hot spots:** appliances ranked by peak, and a heatmap over time. The metric picker switches between utilization, performance score, and uplink Mbps.
- **Appliance detail:** utilization and performance score on one 0–100 chart with the threshold line, and uplink Mbps per interface underneath on the same time axis. Point at the chart to read every value at that time. **Series CSV** downloads all three series for that appliance.
- **Table:** click a header to sort, click again to reverse. Search, tick **Only ≥ threshold**, or download the current view with **Table CSV**. Click a row to open its detail chart.

Uplink usage belongs to the network. Appliances that share a network (for example a warm spare) show the same uplink numbers, marked "(shared)".

Only primary MX devices return utilization history.

## Dashboard calls

All calls are reads.

- `organizations.getOrganizations`
- `organizations.getOrganizationDevices` with `productTypes=["appliance"]`
- `organizations.getOrganizationApplianceDevicesSystemUtilizationByInterval` with `interval=1200`
- `appliance.getDeviceAppliancePerformance` with `t0` and `t1` for each point
- `appliance.getNetworkApplianceUplinksUsageHistory` with `t0`, `t1`, and `resolution`
- `appliance.getNetworkApplianceWarmSpare` when a performance call returns 400 Feature not supported, to report a warm spare instead of the raw error

Every fetch hits the Dashboard again. Organization, inventory, utilization, performance, and uplink results are not stored between requests.

## Security and privacy

- The server binds to `127.0.0.1` and rejects requests whose `Host` header is not `127.0.0.1` or `localhost` on the bound port.
- Use a read-only key. The app only calls read endpoints, but a read-only key limits the damage if the key leaks.
- `application.log` is written next to `server.py` and grows with each run; delete it whenever you like. It can include serials, network IDs, and Dashboard error messages, but not the API key. Keep it on the machine that ran the app. `.gitignore` excludes `*.log` and `.env` files.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Warning: MK_CSM_KEY is not set` after `setx` | Open a new terminal; `setx` only affects terminals started afterwards. |
| `Ports 8765-8814 are all in use` | Run with `--port <free port>`. |
| `WinError 10013` at startup | Windows reserves that port (often for Hyper-V or WSL). Run `netsh interface ipv4 show excludedportrange protocol=tcp` and pass a `--port` outside the listed ranges. |
| The browser does not open | Run with `--no-browser` and open the printed address. |
| `Forbidden host.` in the browser | Open `http://127.0.0.1:<port>/` or `http://localhost:<port>/`, not the machine's name or IP. |
| A `Dashboard API error` mentioning `401` or `403` | The key is wrong or has no access to that organization. |
| An error mentioning `429`, or a fetch is slow | You hit the Dashboard rate limit. Pick a coarser performance resolution or fewer appliances. |
| `py` or `python3` is not found | Install Python 3.11+ from [python.org](https://www.python.org/downloads/) and reopen the terminal. |

## Tests

The tests use fakes and do not call the Dashboard, so no key is needed.

```powershell
# Windows
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest tests -q
```

```bash
# macOS / Linux
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

## Project layout

| Path | Contents |
|------|----------|
| `server.py` | Launch file: HTTP server, request validation, background fetch jobs |
| `logic.py` | Dashboard calls through the Meraki SDK |
| `utilization_rules.py` | Windows, resolutions, parsing, and summaries (no I/O) |
| `core/api.py` | Builds the SDK client from `MK_CSM_KEY` |
| `core/logger.py` | Console logging and `application.log` |
| `static/` | Page HTML, CSS, JavaScript, and icon |
| `tests/` | pytest suite |

## License

[MIT](LICENSE). Meraki is a trademark of Cisco Systems, Inc. This project is not an official Cisco product.
