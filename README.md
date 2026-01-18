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

### Docker

```bash
# pure nix build
nix build .#docker
docker load < result
docker run -d \
  -e QL_ENABLE_HEALTH_SERVER=true \
  -v $(pwd)/config.yaml:/app/config.yaml:ro \
  -p 8080:8080 \
  quadlink

# or multi-stage Dockerfile
docker build -f docker/Dockerfile -t quadlink .

# or Docker Compose
cd docker && docker-compose up -d
```

## Configuration

Configuration is loaded from these paths (in order):

1. `/app/config.yaml`
2. `./config.yaml`
3. `~/.quadlink/config.yaml`
4. `/etc/quadlink/config.yaml`

See `config.yaml.example` for complete reference.

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
```

### Health Checks

HTTP endpoints on port 8080 (disabled by default, enable with `QL_ENABLE_HEALTH_SERVER=true`):

- `GET /health`: Always returns `200 OK` if process is running
- `GET /ready`: Returns `200 OK` if config loaded, `503` otherwise

**Note**: Health checks are intended for containerized deployments (Docker, Kubernetes). For systemd deployments, leave disabled as systemd monitors the process directly.

### Webhooks

Trigger external automations when quad updates:

```yaml
webhook:
  enabled: true
  url: https://homeassistant.local/api/webhook/refresh-appletv
  timeout: 10
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

## License

MIT
