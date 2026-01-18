{
  description = "QuadLink - companion to QuadStream for Apple tvOS";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python313;

        pythonPackages = python.pkgs;

        dependencies = with pythonPackages; [
          aiohttp
          httpx
          pydantic
          pydantic-settings
          ruamel-yaml
          structlog
        ];

        devTools = with pythonPackages; [
          black
          ipython
          isort
          mypy
          pytest
          pytest-asyncio
          pytest-cov
          ruff
        ];

        quadlink = pkgs.callPackage ./nix/package.nix {
          python3Packages = pythonPackages;
        };

        dockerImage = pkgs.dockerTools.buildLayeredImage {
          name = "quadlink";
          tag = "latest";

          contents = [
            quadlink

            pkgs.cacert
            pkgs.coreutils
            pythonPackages.python
          ];

          config = {
            Cmd = [
              "${quadlink}/bin/quadlink"
              "--interval"
              "30"
            ];
            ExposedPorts = {
              "8080/tcp" = { };
            };
            Env = [
              "PYTHONUNBUFFERED=1"
              "QL_LOG_LEVEL=info"
            ];
            User = "1000:1000";
            WorkingDir = "/app";
          };
        };

      in
      {
        packages = {
          default = quadlink;
          quadlink = quadlink;
          docker = dockerImage;
        };

        devShells.default = pkgs.mkShell {
          buildInputs = [
            (python.withPackages (ps: dependencies ++ devTools ++ [ ps.pip ]))
            pkgs.git
            pkgs.just
            pkgs.streamlink
          ];

          shellHook = ''
            export PYTHONPATH="$PWD/src:$PYTHONPATH"
            export PYTHONPATH="${pkgs.streamlink}/${python.sitePackages}:$PYTHONPATH"

            # fetch ttvlol plugin if not present
            PLUGIN_DIR="$PWD/src/quadlink/plugins"
            PLUGIN_FILE="$PLUGIN_DIR/twitch.py"
            if [ ! -f "$PLUGIN_FILE" ]; then
              echo "Fetching streamlink-ttvlol plugin..."
              mkdir -p "$PLUGIN_DIR"
              ${pkgs.curl}/bin/curl -fsSL -o "$PLUGIN_FILE" \
                https://raw.githubusercontent.com/2bc4/streamlink-ttvlol/8a2ebd30dbcbd3caff3f171a1a8c84bc50bc8bd5/twitch.py
            fi

            echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            echo "QuadLink Development Environment"
            echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            echo "Python: $(python --version)"
            echo "Streamlink: $(python -c 'import streamlink; print(streamlink.__version__)' 2>/dev/null || echo 'not found')"
            echo "Twitch Plugin: streamlink-ttvlol (8a2ebd30)"
            echo ""
            echo "Available commands:"
            echo "  python -m quadlink         Run the daemon"
            echo "  pytest                     Run tests"
            echo "  black src/ tests/          Format code"
            echo "  ruff check src/ tests/     Lint code"
            echo "  mypy src/                  Type check"
            echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
          '';
        };
      }
    )
    // {
      nixosModules.default = import ./nix/module.nix;
    };
}
