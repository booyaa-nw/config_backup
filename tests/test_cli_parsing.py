import pytest

from config_backup.cli import build_parser


def test_requires_subcommand():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_fortigate_target_and_file_are_mutually_exclusive():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(['fortigate', '-t', '1.2.3.4', '-f', 'target.csv', '-u', 'admin', '-p', 'pw'])


def test_fortigate_target_mode_defaults():
    parser = build_parser()
    args = parser.parse_args(['fortigate', '-t', '172.16.201.201', '-u', 'admin', '-p', 'P@ssw0rd'])

    assert args.vendor == 'fortigate'
    assert args.target == '172.16.201.201'
    assert args.user == 'admin'
    assert args.password == 'P@ssw0rd'
    assert args.secondary is False
    assert args.tac is False
    assert args.directory == './booyaa_log/config'
    assert args.ssh_port == 22
    assert args.full is False
    assert args.timeout == 60.0
    assert args.tac_timeout is None


def test_fortigate_secondary_flag():
    parser = build_parser()
    args = parser.parse_args(['fortigate', '-t', '172.16.201.201', '-u', 'admin', '-p', 'pw', '-s'])
    assert args.secondary is True


def test_fortigate_tac_flag():
    parser = build_parser()
    args = parser.parse_args(['fortigate', '-t', '172.16.201.201', '-u', 'admin', '-p', 'pw', '--tac'])
    assert args.tac is True


def test_fortigate_tac_timeout_option():
    parser = build_parser()
    args = parser.parse_args(['fortigate', '-t', '172.16.201.201', '-u', 'admin', '-p', 'pw',
                               '--tac', '--tac-timeout', '120'])
    assert args.tac_timeout == 120.0


def test_fortigate_debug_log_option():
    parser = build_parser()
    args = parser.parse_args(['fortigate', '-t', '172.16.201.201', '-u', 'admin', '-p', 'pw'])
    assert args.debug_log is None

    args = parser.parse_args(['fortigate', '-t', '172.16.201.201', '-u', 'admin', '-p', 'pw',
                               '--debug-log', 'scrapli_debug.log'])
    assert args.debug_log == 'scrapli_debug.log'


def test_fortigate_file_mode_does_not_require_user_password():
    parser = build_parser()
    args = parser.parse_args(['fortigate', '-f', 'target.csv'])
    assert args.file == 'target.csv'
    assert args.user is None
    assert args.password is None
