# QuadLink

Companion to [QuadStream](https://quadstream.tv) for Apple tvOS.

## How It Works

### Stream Selection Algorithm

1. **Fetch stream metadata**: Uses streamlink library to get Twitch stream info
2. **Apply filters**: Regex-based allow/block rules for categories and titles
3. **Prioritize candidates**: Sort by configured priority levels
4. **Apply bonuses**:
   - Stability bonus (+30 default): Existing streams in quad
   - Diversity bonus (+25 default): First stream in new category
   - Saturation penalty: Graduated penalty for category over-representation
5. **Select top 4**: Unique authors, sorted by adjusted priority
6. **Preserve positions**: Existing streams keep their quad slots
7. **Update QuadStream**: Push new quad configuration

### Stability Bonus

Prevents "quad flapping" by giving existing streams a priority boost. A stream already in your quad needs to drop significantly in priority before being replaced.

**Example**: If `stability_bonus=30`, an existing stream with base priority 100 gets adjusted to 130, making it harder for new streams to replace it.

### Diversity Bonus

Encourages variety by boosting the highest-priority stream in each new category. Applied only once per category.

**Example**: If `diversity_bonus=25`, the first "Music" stream gets +25 priority, but subsequent Music streams don't.

### Category Saturation Penalty

Prevents a single category from dominating the quad with graduated penalties:

- 2nd stream in category: `-diversity_bonus/3` (≈8 points)
- 3rd stream in category: `-2*diversity_bonus/3` (≈16 points)
- 4th+ stream in category: `-diversity_bonus` (25 points)

## Getting Started

**Requirements:**

- Nix with flakes enabled
- QuadStream account

```bash
nix develop # enter development shell
cp config.yaml.example config.yaml # copy example config
$EDITOR config.yaml # edit config with your credentials
python -m quadlink # run daemon
```

## Deployment

### NixOS

Add to your system flake:

```nix
{
  inputs.quadlink.url = "github:etedor/quadlink";

  outputs = { nixpkgs, quadlink, ... }: {
    nixosConfigurations.yourhost = nixpkgs.lib.nixosSystem {
      modules = [
        quadlink.nixosModules.default
        {
          services.quadlink = {
            enable = true;
            configFile = /etc/quadlink/config.yaml;
            interval = 30;
            logLevel = "info";
            openFirewall = true;
          };
        }
      ];
    };
  };
}
```

Deploy: `sudo nixos-rebuild switch`

Manage: `systemctl status quadlink`, `journalctl -u quadlink -f`

## Configuration

Configuration is loaded from these paths (in order):

1. `/app/config.yaml`
2. `./config.yaml`
3. `~/.quadlink/config.yaml`
4. `/etc/quadlink/config.yaml`

See `config.yaml.example` for complete reference.

`lookup_timeout` (default 10 seconds) sets the time limit for one channel lookup. A lookup that takes longer counts as failed for that refresh. On the next refresh, the channel gets 3 times the limit. Real lookups take 0.5 to 2 seconds. Refer to [bench/README.md](bench/README.md) for the measurements.

`log_level` sets the minimum level the daemon logs (`debug`, `info`, `warning` or `error`; default `info`). A change to it takes effect on the next config reload, without a restart. The `--log-level` CLI flag (and NixOS module's `logLevel` option) takes priority over it when set.

### Environment Variables

Override any config value with `QL_` prefix:

```bash
export QL_CREDENTIALS__USERNAME=myusername
export QL_CREDENTIALS__SECRET=mysecret
# or reference a file containing credentials (username:password or password only):
export QL_CREDENTIALS__FILE=/run/secrets/quadlink
export QL_DIVERSITY_BONUS=30
export QL_STABILITY_BONUS=40
export QL_ENABLE_HEALTH_SERVER=true  # enable health checks (off by default)
export QL_STATE_FILE=./state.json    # state file location, same as --state-file
```

### State File

The daemon keeps pins, benches and the slot of each on-screen channel in `/var/lib/quadlink/state.json`, so they survive a restart. The daemon writes the file after each change. To use a different file (for example in development), give `--state-file PATH` or set `QL_STATE_FILE`. If the file is not valid, the daemon logs a warning and starts with no pins or benches.

### Health Checks

HTTP endpoints on port 8080 (disabled by default, enable with `QL_ENABLE_HEALTH_SERVER=true`):

- `GET /health`: Always returns `200 OK` if process is running
- `GET /ready`: Returns `200 OK` if a refresh completed in the last 3 intervals, `503` otherwise. A refresh also completes while QuadStream does not accept the login.

**Note**: Health checks are intended for containerized deployments. For systemd deployments, leave disabled as systemd monitors the process directly.

### Web UI

Start the daemon with `--webui` to edit the config in a browser (default port 8081).
The NixOS option is `enableWebui`.

The page and its `/api/*` routes accept a request only when the `Host` header names an allowed host.
The allowed hosts are `localhost`, `127.0.0.1`, `::1`, and each name that you give with `--webui-allowed-host`.
If a reverse proxy serves the web UI under a different name, add that name:

```nix
services.quadlink.webuiAllowedHosts = [ "quadlink.example.net" ];
```

The reverse proxy must send the original `Host` header (for nginx: `proxy_set_header Host $host`).
If you open the web UI by an IP address, add that address too.
The relay routes at `/streams/` accept all host names.

The config API accepts only JSON requests from the web UI page.
Each request must carry the page token that the server puts in the page.
The token changes when the daemon restarts. After a restart, reload the page.

When you save, the web UI writes only the values on the page to the config file.
It keeps the `credentials` section of the file as written.
It does not write default values, `QL_*` environment overrides, or credentials read from a file.

### Dashboard API

The web UI also serves an API for the dashboard and for scripts.
Slots are numbered 0 to 3 (the relay serves them at `/streams/1` to `/streams/4`).
The API uses the same checks as the config API: an allowed `Host`, a same-origin request, and the page token in the `X-QuadLink-Token` header.
Two routes are different, because a browser cannot send headers for them:

- `/api/events` also accepts the token as the `token` query parameter.
- `/api/slots/{slot}/still.jpg` does not need the token. It refuses requests from other sites.

| Route | Result |
|---|---|
| `GET /api/state` | The snapshot as JSON. |
| `GET /api/events` | Server-Sent Events. The server sends the snapshot when you connect and each time it changes (at most 4 times each second). It sends a comment line after 15 seconds without an event. |
| `POST /api/actions` | Applies one operator action. If the action succeeds, the server sends `200` and the new snapshot. If the action is not valid, the server sends `400` and `{"error": "..."}`. |
| `GET /api/slots/{slot}/still.jpg` | A JPEG (480 pixels wide) of the picture in the slot, at most 5 seconds old. If no client plays the slot, or ffmpeg cannot decode a frame, the server redirects to the Twitch preview image of the channel. For an empty slot, the server sends `404`. |

The snapshot has `"v": 1`. The version changes when the shape changes. The snapshot contains:

- `refreshed_at`: the time of the last channel lookup (seconds since the epoch), or `null`.
- `quadstream`: the QuadStream login (`off`, `pending`, `logged in` or `failed`).
- `slots`: for each slot, `channel`, `display_name`, `title`, `category`, `pinned`, `reserved_for` and `still` (the still URL). An empty slot has `null` values.
- `channels`: for each configured channel, `channel`, `display_name`, `title`, `category`, `state`, `slot`, `pin_slot`, `benched`, `priorities`, `priority`, `reason` and `thumbnail`.

The body of an action is a JSON object with an `action` and its parameters, and no other keys:

| Action | Parameters |
|---|---|
| `place` | `channel` (login), `slot` |
| `swap` | `a`, `b` (slots) |
| `pin`, `unpin` | `slot` |
| `bench`, `unbench` | `channel` |
| `reset` | none |

Example (the token is in the `quadlink-token` meta tag of the page):

```bash
curl -s -H "X-QuadLink-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"action": "pin", "slot": 2}' http://localhost:8081/api/actions
```

### Webhooks

Trigger external automations when quad updates:

```yaml
webhook:
  enabled: true
  url: https://homeassistant.local/api/webhook/refresh-appletv
```

## Development

```bash
nix develop # enter dev shell
pytest # run tests
pytest --cov # run tests with coverage
black src/ tests/ # format
ruff check src/ tests/ # lint
mypy src/ # type check
```

`nix flake check` builds the package, runs the tests, and checks formatting, lint and types. CI (`.github/workflows/ci.yml`) runs the same command on each push and on each pull request.

### Web pages

The pages in `src/quadlink/web/` load in a browser on your own machine, against a running quadlink:

```bash
nix develop -c python dev/serve.py   # http://localhost:8090/quad
```

The page reloads each time you save a file. `/streams/*` and `/api/*` go to `https://quadlink.et42.net` (change it with `--upstream`).

## License

MIT
