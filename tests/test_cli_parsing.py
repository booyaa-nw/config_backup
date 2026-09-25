import pytest

from config_backup.cli import _build_faz_table, _build_msw_table, _MSW_ITEM_STATUS_TEXT, build_parser
from config_backup.faz import FazBackupResult, FazTarget
from config_backup.msw import MswBackupResult, MswTarget


def test_requires_subcommand():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_fgt_target_and_file_are_mutually_exclusive():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(['fgt', '-t', '1.2.3.4', '-f', 'target.csv', '-u', 'admin', '-p', 'pw'])


def test_fgt_target_mode_defaults():
    parser = build_parser()
    args = parser.parse_args(['fgt', '-t', '172.16.201.201', '-u', 'admin', '-p', 'P@ssw0rd'])

    assert args.vendor == 'fgt'
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


def test_fgt_secondary_flag():
    parser = build_parser()
    args = parser.parse_args(['fgt', '-t', '172.16.201.201', '-u', 'admin', '-p', 'pw', '-s'])
    assert args.secondary is True


def test_fgt_tac_flag():
    parser = build_parser()
    args = parser.parse_args(['fgt', '-t', '172.16.201.201', '-u', 'admin', '-p', 'pw', '--tac'])
    assert args.tac is True


def test_fgt_tac_timeout_option():
    parser = build_parser()
    args = parser.parse_args(['fgt', '-t', '172.16.201.201', '-u', 'admin', '-p', 'pw',
                               '--tac', '--tac-timeout', '120'])
    assert args.tac_timeout == 120.0


def test_fgt_debug_log_option():
    parser = build_parser()
    args = parser.parse_args(['fgt', '-t', '172.16.201.201', '-u', 'admin', '-p', 'pw'])
    assert args.debug_log is None

    args = parser.parse_args(['fgt', '-t', '172.16.201.201', '-u', 'admin', '-p', 'pw',
                               '--debug-log', 'scrapli_debug.log'])
    assert args.debug_log == 'scrapli_debug.log'


def test_fgt_file_mode_does_not_require_user_password():
    parser = build_parser()
    args = parser.parse_args(['fgt', '-f', 'target.csv'])
    assert args.file == 'target.csv'
    assert args.user is None
    assert args.password is None


def test_old_fortigate_identifier_is_no_longer_valid():
    """2026-09-22: 対象識別子を`fortigate`から`fgt`に変更したことの回帰防止。"""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(['fortigate', '-t', '172.16.201.201', '-u', 'admin', '-p', 'pw'])


def test_msw_target_and_file_are_mutually_exclusive():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(['msw', '-t', '1.2.3.4', '-f', 'target.csv', '-u', 'admin', '-p', 'pw'])


def test_msw_target_mode_defaults():
    parser = build_parser()
    args = parser.parse_args(['msw', '-t', '172.16.201.207', '-u', 'admin', '-p', 'P@ssw0rd!!!!'])

    assert args.vendor == 'msw'
    assert args.target == '172.16.201.207'
    assert args.user == 'admin'
    assert args.password == 'P@ssw0rd!!!!'
    assert args.msw_user == 'admin'
    assert args.msw_password is None
    assert args.tac is False
    # MSWはFGT本体と違い、既定のbackup directoryは実行時(FGTホスト名判明後)に
    # 動的に決まるため、argparseの既定値自体はNone。
    assert args.directory is None
    assert args.ssh_port == 22
    assert args.full is False
    assert args.timeout == 60.0
    assert args.tac_timeout is None


def test_msw_user_password_options():
    parser = build_parser()
    args = parser.parse_args(['msw', '-t', '172.16.201.207', '-u', 'admin', '-p', 'pw',
                               '--msw-user', 'mswadmin', '--msw-password', 'mswpw'])
    assert args.msw_user == 'mswadmin'
    assert args.msw_password == 'mswpw'


def test_msw_tac_flag():
    parser = build_parser()
    args = parser.parse_args(['msw', '-t', '172.16.201.207', '-u', 'admin', '-p', 'pw', '--tac'])
    assert args.tac is True


def test_msw_directory_override():
    parser = build_parser()
    args = parser.parse_args(['msw', '-t', '172.16.201.207', '-u', 'admin', '-p', 'pw',
                               '-d', './custom_dir'])
    assert args.directory == './custom_dir'


def test_msw_file_mode_does_not_require_user_password():
    parser = build_parser()
    args = parser.parse_args(['msw', '-f', 'target.csv'])
    assert args.file == 'target.csv'
    assert args.user is None
    assert args.password is None


def test_msw_skip_login_text_is_orange():
    """スキップ時、Login列にオレンジで"Skip"と表示する(こうぢさん指定、2026-09-22追加)。"""
    login_txt, backup_txt = _MSW_ITEM_STATUS_TEXT['skipped']
    assert login_txt == '[orange3]Skip[/]'
    assert backup_txt == '-'


def test_msw_table_status_and_message_columns_do_not_truncate():
    """Status/Message列は省略(ellipsis)せず折り返し表示する設定になっていること
    (こうぢさん指定、2026-09-22追加。テーブル自体の描画結果ではなく、rich.Tableの
    列設定(overflow/no_wrap)を直接検証する)。"""
    result = MswBackupResult(target=MswTarget(fgt_addr='172.16.201.207', fgt_user='admin',
                                                fgt_password='pw'), status='done')
    table = _build_msw_table([result], title='t')

    by_header = {c.header: c for c in table.columns}
    assert by_header['Status'].overflow == 'fold'
    assert by_header['Status'].no_wrap is False
    assert by_header['Message'].overflow == 'fold'
    assert by_header['Message'].no_wrap is False


def test_faz_target_and_file_are_mutually_exclusive():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(['faz', '-t', '1.2.3.4', '-f', 'target.csv', '-u', 'admin', '-p', 'pw'])


def test_faz_target_mode_defaults():
    parser = build_parser()
    args = parser.parse_args(['faz', '-t', '172.16.201.210', '-u', 'admin', '-p', 'P@ssw0rd'])

    assert args.vendor == 'faz'
    assert args.target == '172.16.201.210'
    assert args.user == 'admin'
    assert args.password == 'P@ssw0rd'
    assert args.name == ''
    assert args.ssh_port == 22
    assert args.config_password is None
    assert args.ftp_user == ''
    assert args.ftp_password == ''
    assert args.ftp_port == 2121
    assert args.tac is False
    # FAZはFGT本体と同じく既定のbackup directoryを実行時に動的決定する
    # (`config_backup.faz.DEFAULT_BACKUP_DIR`)ため、argparseの既定値自体はNone。
    assert args.directory is None
    assert args.timeout == 60.0
    assert args.sys_backup_timeout is None
    assert args.tac_timeout is None


def test_faz_tac_flag():
    parser = build_parser()
    args = parser.parse_args(['faz', '-t', '172.16.201.210', '-u', 'admin', '-p', 'pw', '--tac'])
    assert args.tac is True


def test_faz_ftp_options():
    parser = build_parser()
    args = parser.parse_args(['faz', '-t', '172.16.201.210', '-u', 'admin', '-p', 'pw',
                               '--ftp-user', 'nwadmin', '--ftp-password', 'ftppw', '--ftp-port', '2222'])
    assert args.ftp_user == 'nwadmin'
    assert args.ftp_password == 'ftppw'
    assert args.ftp_port == 2222


def test_faz_config_password_option():
    parser = build_parser()
    args = parser.parse_args(['faz', '-t', '172.16.201.210', '-u', 'admin', '-p', 'pw',
                               '--config-password', 'CfgPw'])
    assert args.config_password == 'CfgPw'


def test_faz_directory_override():
    parser = build_parser()
    args = parser.parse_args(['faz', '-t', '172.16.201.210', '-u', 'admin', '-p', 'pw',
                               '-d', './custom_dir'])
    assert args.directory == './custom_dir'


def test_faz_sys_backup_timeout_option():
    parser = build_parser()
    args = parser.parse_args(['faz', '-t', '172.16.201.210', '-u', 'admin', '-p', 'pw',
                               '--sys-backup-timeout', '900'])
    assert args.sys_backup_timeout == 900.0


def test_faz_file_mode_does_not_require_user_password():
    parser = build_parser()
    args = parser.parse_args(['faz', '-f', 'target.csv'])
    assert args.file == 'target.csv'
    assert args.user is None
    assert args.password is None


def test_faz_table_columns():
    result = FazBackupResult(
        target=FazTarget(addr='172.16.201.210', user='admin', password='pw'),
        hostname='FAZ_VM', status='done', config_status='ok', sys_status='ok',
    )
    table = _build_faz_table([result], title='t')

    headers = [c.header for c in table.columns]
    assert headers == ['Addr', 'Hostname', 'User', 'Login', 'Config', 'System', 'TAC', 'Message']


# ----------------------------------------------------------------------
# ax(Alaxala AX3000、2026-09-23追加)
# ----------------------------------------------------------------------
def test_ax_target_mode_defaults():
    from config_backup.alaxala import DEFAULT_CONCURRENCY
    parser = build_parser()
    args = parser.parse_args(['ax', '-t', '172.16.201.205', '-u', 'nwadmin', '-p', 'P@ssw0rd'])
    assert args.vendor == 'ax'
    assert args.enable_password == ''
    assert args.no_software is False
    assert args.tac is False
    assert args.directory is None
    assert args.concurrency == DEFAULT_CONCURRENCY == 5
    assert args.ftp_user == '' and args.ftp_password == ''


def test_ax_options():
    parser = build_parser()
    args = parser.parse_args(['ax', '-t', '172.16.201.205', '-u', 'nwadmin', '-p', 'pw',
                              '-e', 'EnPass', '--no-software', '--tac', '--tac-timeout', '1200'])
    assert args.enable_password == 'EnPass'
    assert args.no_software is True
    assert args.tac is True
    assert args.tac_timeout == 1200.0


def test_ax_has_no_ftp_port_option():
    """`backup ftp`はポート番号を指定できないため、--ftp-portは設けない(21番固定)。"""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(['ax', '-t', '1.2.3.4', '-u', 'a', '-p', 'b', '--ftp-port', '2121'])


def test_ax_target_and_file_are_mutually_exclusive():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(['ax', '-t', '1.2.3.4', '-f', 'target.csv'])


def test_ax_comma_separated_targets(monkeypatch):
    import config_backup.cli as cli_mod
    captured = {}

    def fake_run(targets, args):
        captured['targets'] = targets
        return []

    monkeypatch.setattr(cli_mod, '_run_ax_with_live_progress', fake_run)
    rc = cli_mod.main(['ax', '-t', '10.0.0.1, 10.0.0.2,', '-u', 'nwadmin', '-p', 'pw',
                       '-e', 'en', '--no-software', '--tac'])
    assert rc == 0
    assert [t.addr for t in captured['targets']] == ['10.0.0.1', '10.0.0.2']
    assert all(t.enable_password == 'en' and t.no_software and t.tac for t in captured['targets'])


def test_ax_table_shows_noos_and_success():
    from config_backup.alaxala import AxBackupResult, AxTarget
    from config_backup.cli import _build_ax_table, _ax_message
    r = AxBackupResult(target=AxTarget(addr='1.1.1.1', user='u', password='p', no_software=True),
                       hostname='sw', version='12.1.W', status='done',
                       config_status='ok', sys_status='ok')
    table = _build_ax_table([r], title='t')
    assert [c.header for c in table.columns] == [
        'Addr', 'Hostname', 'Version', 'User', 'Login', 'Config', 'System', 'TAC', 'Message']
    assert 'no-os' in list(table.columns[6].cells)[0]
    assert 'success' in _ax_message(r)
