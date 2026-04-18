{ config, lib, pkgs, ... }:

with lib;

let
  cfg = config.services.quadlink;
  quadlinkPackage = pkgs.callPackage ./package.nix { };
in {
  options.services.quadlink = {
    enable = mkEnableOption "QuadLink (QuadStream companion)";

    package = mkOption {
      type = types.package;
      default = quadlinkPackage;
      description = "QuadLink package to use";
    };

    user = mkOption {
      type = types.str;
      default = "quadlink";
      description = "User account under which QuadLink runs";
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
      type = types.enum [ "debug" "info" "warn" "error" ];
      default = "info";
      description = "Log level for the daemon";
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
      description = "QuadLink daemon user";
    };

    users.groups.${cfg.group} = { };

    systemd.services.quadlink = {
      description = "QuadLink (QuadStream Companion)";
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
            --log-level ${cfg.logLevel} \
            ${optionalString cfg.enableWebui "--webui --webui-host ${cfg.webuiHost} --webui-port ${toString cfg.webuiPort}"}
        '';

        Environment = [
          "PYTHONUNBUFFERED=1"
        ];

        MemoryMax = "512M";
        TasksMax = "50";
      };

      environment = {
        HOME = "/var/lib/quadlink";
      };
    };

    networking.firewall.allowedTCPPorts = mkIf (cfg.openFirewall && cfg.enableWebui) [ cfg.webuiPort ];
  };
}
