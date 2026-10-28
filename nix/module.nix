{ config, lib, pkgs, ... }:

with lib;

let
  cfg = config.services.quadlink;
  quadlinkPackage = pkgs.callPackage ./package.nix { };
in {
  options.services.quadlink = {
    enable = mkEnableOption "Quadlink (QuadStream companion)";

    package = mkOption {
      type = types.package;
      default = quadlinkPackage;
      description = "Quadlink package to use";
    };

    user = mkOption {
      type = types.str;
      default = "quadlink";
      description = "User account under which Quadlink runs";
    };

    group = mkOption {
      type = types.str;
      default = "quadlink";
      description = "Group under which quadlink runs";
    };

    configFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      description = "Path to quadlink config.yaml file (default: auto-discover from standard paths)";
      example = "/var/lib/quadlink/.quadlink/config.yaml";
    };

    interval = mkOption {
      type = types.int;
      default = 30;
      description = "Seconds between quad updates";
    };

    logLevel = mkOption {
      type = types.nullOr (types.enum [ "debug" "info" "warn" "error" ]);
      default = null;
      description = "Log level for the daemon; null defers to the config file's log_level (or info)";
    };

    enableWebui = mkOption {
      type = types.bool;
      default = false;
      description = "Enable web UI for config editing";
    };

    webuiHost = mkOption {
      type = types.str;
      default = "127.0.0.1";
      description = "Host address to bind the web UI to";
    };

    webuiPort = mkOption {
      type = types.port;
      default = 8081;
      description = "Port for the web UI";
    };

    webuiAllowedHosts = mkOption {
      type = types.listOf types.str;
      default = [ ];
      description = ''
        Host names that the web UI page and config API accept, such as the name of a
        reverse proxy in front of the web UI. localhost, 127.0.0.1 and ::1 are always
        allowed.
      '';
      example = [ "quadlink.example.net" ];
    };

    twitchApiClientId = mkOption {
      type = types.nullOr types.str;
      default = null;
      description = ''
        Client ID of a registered Twitch application, for viewer counts from the Helix
        API. Sets QL_TWITCH_API__CLIENT_ID; the config file can set twitch_api.client_id
        instead.
      '';
    };

    twitchApiSecretFile = mkOption {
      # a string, so that the secret never goes into the Nix store
      type = types.nullOr types.str;
      default = null;
      description = ''
        File that holds the client secret of the Twitch application. The quadlink user
        must be able to read it. Sets QL_TWITCH_API__CLIENT_SECRET_FILE.
      '';
      example = "/run/agenix/quadlink-twitch-secret";
    };

    openFirewall = mkOption {
      type = types.bool;
      default = false;
      description = "Open the web UI port in the firewall";
    };
  };

  config = mkIf cfg.enable {
    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.group;
      home = "/var/lib/quadlink";
      createHome = true;
      description = "Quadlink daemon user";
    };

    users.groups.${cfg.group} = { };

    systemd.services.quadlink = {
      description = "Quadlink (QuadStream Companion)";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];

      serviceConfig = {
        Type = "simple";
        User = cfg.user;
        Group = cfg.group;
        Restart = "always";
        RestartSec = "10s";

        StateDirectory = "quadlink";
        StateDirectoryMode = "0775";
        WorkingDirectory = "/var/lib/quadlink";

        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectSystem = "strict";
        ProtectHome = true;

        ExecStartPre = "${pkgs.coreutils}/bin/sleep 2";

        ExecStart = ''
          ${cfg.package}/bin/quadlink \
            ${optionalString (cfg.configFile != null) "--config ${cfg.configFile}"} \
            --interval ${toString cfg.interval} \
            ${optionalString (cfg.logLevel != null) "--log-level ${cfg.logLevel}"} \
            ${optionalString cfg.enableWebui "--webui --webui-host ${cfg.webuiHost} --webui-port ${toString cfg.webuiPort}"} \
            ${optionalString cfg.enableWebui (concatMapStringsSep " " (h: "--webui-allowed-host ${escapeShellArg h}") cfg.webuiAllowedHosts)}
        '';

        Environment = [
          "PYTHONUNBUFFERED=1"
          # segment-sized buffers go to mmap and back to the kernel on free; the
          # glibc heap otherwise keeps ~25 MiB of them. ffmpeg children inherit it,
          # which is harmless there: their anon memory drops (bench: 72 -> 61 MiB)
          "MALLOC_MMAP_THRESHOLD_=131072"
        ]
        ++ optional (cfg.twitchApiClientId != null) "QL_TWITCH_API__CLIENT_ID=${cfg.twitchApiClientId}"
        ++ optional (cfg.twitchApiSecretFile != null) "QL_TWITCH_API__CLIENT_SECRET_FILE=${cfg.twitchApiSecretFile}";

        MemoryMax = "512M";
        # each ffmpeg converter runs several threads, up to 4 slots plus retiring runs
        TasksMax = "200";
      };

      environment = {
        HOME = "/var/lib/quadlink";
      };
    };

    networking.firewall.allowedTCPPorts = mkIf (cfg.openFirewall && cfg.enableWebui) [ cfg.webuiPort ];
  };
}
