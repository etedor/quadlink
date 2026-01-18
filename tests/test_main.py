"""Tests for __main__ module."""

from unittest.mock import MagicMock, patch


class TestSetupLogging:
    """Tests for setup_logging function."""

    def test_setup_logging_configures_structlog(self):
        """setup_logging should configure structlog."""
        from quadlink.__main__ import setup_logging

        with patch("quadlink.__main__.structlog") as mock_structlog:
            setup_logging()

            mock_structlog.configure.assert_called_once()
            call_kwargs = mock_structlog.configure.call_args[1]
            assert "processors" in call_kwargs
            assert "wrapper_class" in call_kwargs
            assert "logger_factory" in call_kwargs

    def test_setup_logging_uses_console_renderer_for_tty(self):
        """Should use ConsoleRenderer when stderr is a tty."""
        from quadlink.__main__ import setup_logging

        with patch("quadlink.__main__.structlog") as mock_structlog:
            with patch("quadlink.__main__.sys") as mock_sys:
                mock_sys.stderr.isatty.return_value = True
                setup_logging()

                # verify ConsoleRenderer was used
                call_kwargs = mock_structlog.configure.call_args[1]
                call_kwargs["processors"]
                # last processor should be ConsoleRenderer
                mock_structlog.dev.ConsoleRenderer.assert_called()

    def test_setup_logging_uses_json_renderer_for_non_tty(self):
        """Should use JSONRenderer when stderr is not a tty."""
        from quadlink.__main__ import setup_logging

        with patch("quadlink.__main__.structlog") as mock_structlog:
            with patch("quadlink.__main__.sys") as mock_sys:
                mock_sys.stderr.isatty.return_value = False
                setup_logging()

                # verify JSONRenderer was used
                mock_structlog.processors.JSONRenderer.assert_called()


class TestParseArgs:
    """Tests for parse_args function."""

    def test_parse_args_defaults(self):
        """Should use default values when no args provided."""
        from quadlink.__main__ import parse_args

        with patch("sys.argv", ["quadlink"]):
            args = parse_args()

        assert args.one_shot is False
        assert args.interval == 30

    def test_parse_args_one_shot(self):
        """Should parse --one-shot flag."""
        from quadlink.__main__ import parse_args

        with patch("sys.argv", ["quadlink", "--one-shot"]):
            args = parse_args()

        assert args.one_shot is True

    def test_parse_args_interval(self):
        """Should parse --interval argument."""
        from quadlink.__main__ import parse_args

        with patch("sys.argv", ["quadlink", "--interval", "60"]):
            args = parse_args()

        assert args.interval == 60

    def test_parse_args_combined(self):
        """Should parse multiple arguments."""
        from quadlink.__main__ import parse_args

        with patch("sys.argv", ["quadlink", "--one-shot", "--interval", "45"]):
            args = parse_args()

        assert args.one_shot is True
        assert args.interval == 45


class TestMain:
    """Tests for main function."""

    def test_main_calls_setup_and_run(self):
        """main should setup logging and run daemon."""
        from quadlink.__main__ import main

        with patch("quadlink.__main__.parse_args") as mock_parse:
            mock_args = MagicMock()
            mock_args.one_shot = False
            mock_args.interval = 30
            mock_parse.return_value = mock_args

            with patch("quadlink.__main__.setup_logging") as mock_setup:
                with patch("quadlink.__main__.asyncio.run") as mock_run:
                    main()

                    mock_setup.assert_called_once()
                    mock_run.assert_called_once()

    def test_main_passes_args_to_run_daemon(self):
        """main should pass parsed args to run_daemon."""
        from quadlink.__main__ import main

        with patch("quadlink.__main__.parse_args") as mock_parse:
            mock_args = MagicMock()
            mock_args.one_shot = True
            mock_args.interval = 60
            mock_args.config = "/path/to/config.yaml"
            mock_parse.return_value = mock_args

            with patch("quadlink.__main__.setup_logging"):
                with patch("quadlink.__main__.os.environ.get", return_value="false"):
                    with patch("quadlink.__main__.asyncio.run"):
                        with patch("quadlink.__main__.run_daemon") as mock_daemon:
                            main()

                            mock_daemon.assert_called_once_with(
                                one_shot=True,
                                interval=60,
                                enable_health_server=False,
                                config_path="/path/to/config.yaml",
                            )

    def test_main_handles_keyboard_interrupt(self):
        """main should handle KeyboardInterrupt gracefully."""
        from quadlink.__main__ import main

        with patch("quadlink.__main__.parse_args") as mock_parse:
            mock_args = MagicMock()
            mock_args.one_shot = False
            mock_args.interval = 30
            mock_parse.return_value = mock_args

            with patch("quadlink.__main__.setup_logging"):
                with patch("quadlink.__main__.asyncio.run") as mock_run:
                    mock_run.side_effect = KeyboardInterrupt()

                    # should not raise
                    main()

    def test_main_exits_on_exception(self):
        """main should exit with code 1 on exception."""
        from quadlink.__main__ import main

        with patch("quadlink.__main__.parse_args") as mock_parse:
            mock_args = MagicMock()
            mock_args.one_shot = False
            mock_args.interval = 30
            mock_parse.return_value = mock_args

            with patch("quadlink.__main__.setup_logging"):
                with patch("quadlink.__main__.asyncio.run") as mock_run:
                    mock_run.side_effect = RuntimeError("test error")

                    with patch("quadlink.__main__.sys.exit") as mock_exit:
                        main()
                        mock_exit.assert_called_once_with(1)

    def test_main_logs_mode_daemon(self):
        """main should log daemon mode when not one-shot."""
        from quadlink.__main__ import main

        with patch("quadlink.__main__.parse_args") as mock_parse:
            mock_args = MagicMock()
            mock_args.one_shot = False
            mock_args.interval = 30
            mock_parse.return_value = mock_args

            with patch("quadlink.__main__.setup_logging"):
                with patch("quadlink.__main__.asyncio.run"):
                    with patch("quadlink.__main__.structlog") as mock_structlog:
                        mock_logger = MagicMock()
                        mock_structlog.get_logger.return_value = mock_logger

                        main()

                        mock_logger.info.assert_called_with(
                            "starting quadlink", mode="daemon", interval=30
                        )

    def test_main_logs_mode_one_shot(self):
        """main should log one-shot mode."""
        from quadlink.__main__ import main

        with patch("quadlink.__main__.parse_args") as mock_parse:
            mock_args = MagicMock()
            mock_args.one_shot = True
            mock_args.interval = 30
            mock_parse.return_value = mock_args

            with patch("quadlink.__main__.setup_logging"):
                with patch("quadlink.__main__.asyncio.run"):
                    with patch("quadlink.__main__.structlog") as mock_structlog:
                        mock_logger = MagicMock()
                        mock_structlog.get_logger.return_value = mock_logger

                        main()

                        mock_logger.info.assert_called_with(
                            "starting quadlink", mode="one-shot", interval=30
                        )
