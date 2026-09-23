"""backup_one_faz()/run_faz_backup()のオーケストレーションロジックのテスト。

実機接続(net_config.ftnt.faz.cli.FazCli)、および実際のソケット通信を伴う
`common.ftp.SimpleFtpServer`はいずれもフェイクに差し替える(`SimpleFtpServer`
自体のFTPプロトコルレベルの検証は`common/tests/test_ftp.py`で別途行っている)。
`common.iptools.host.routing.get_src_addr()`もネットワーク非依存にするため
フェイクへ差し替える。
"""
from pathlib import Path
from types import SimpleNamespace

import config_backup.faz as faz_mod
from config_backup.faz import FazTarget, backup_one_faz, load_faz_list_csv, run_faz_backup


class FakeFazCli:
    """net_config.ftnt.faz.cli.FazCliの必要最小限を模したフェイク。"""

    instances = []

    def __init__(self, faz_info=None, timeout=30.0):
        self.faz_info = faz_info or SimpleNamespace()
        self.faz_info.hostname = ''
        self.faz_info.version = ''

        self.fail_login = False
        self.hostname_on_login = 'FAZ_VM'
        # 2026-09-23追加: バックアップファイル名にversionを含めるようになったため
        # (こうぢさん指摘)、hostname同様ログイン時に反映されるフェイク版version。
        self.version_on_login = '7.4.11'

        self.show_result = {'code': 0, 'msg': '', 'output': 'faz config dump'}
        self.backup_all_settings_result = {'code': 0, 'msg': '', 'output': 'Backup all settings...Ok.'}
        self.tac_result = {'code': 0, 'msg': '', 'output': 'faz tac report dump'}

        self.show_calls: list[dict] = []
        self.backup_all_settings_calls: list[dict] = []
        self.tac_calls: list[tuple] = []
        self.logged_out = False

        self.show = SimpleNamespace(get=self._show_get)
        self.execute = SimpleNamespace(
            backup_all_settings=SimpleNamespace(run=self._backup_all_settings_run),
            tac_report=SimpleNamespace(get=self._tac_report_get),
        )

        FakeFazCli.instances.append(self)

    def set_target(self, **kwargs):
        self.faz_info.alias = kwargs.get('faz_alias', '')

    def login(self):
        if self.fail_login:
            return {'code': 1, 'msg': '[Error] login failed', 'output': ''}
        self.faz_info.hostname = self.hostname_on_login
        self.faz_info.version = self.version_on_login
        return {'code': 0, 'msg': '', 'output': ''}

    def logout(self):
        self.logged_out = True
        return {'code': 0, 'msg': '', 'output': ''}

    def _show_get(self):
        self.show_calls.append({})
        return self.show_result

    def _backup_all_settings_run(self, **kwargs):
        self.backup_all_settings_calls.append(kwargs)
        return self.backup_all_settings_result

    def _tac_report_get(self, expected_hostname, timeout=None):
        self.tac_calls.append((expected_hostname, timeout))
        return self.tac_result


class FakeFtpServer:
    """common.ftp.SimpleFtpServerの必要最小限を模したフェイク(実ソケット通信なし)。

    実際のFTP転送は行わず、`wait_for_file()`が呼ばれた時点で`directory`配下に
    受信済みとみなすファイルをその場で書き込んで返す。これにより
    `config_backup.faz`側の「コマンド実行→受信確認→save_binary_file()で
    最終保存先へコピー」という呼び出しロジックだけを、実ネットワーク無しで検証できる。
    """

    instances: list['FakeFtpServer'] = []
    fail_to_receive = False
    file_content = b'\x00dummy system backup content\xff'

    def __init__(self, directory, user, password, host='0.0.0.0', port=0, passive_ports=(0, 0)):
        self.directory = Path(directory)
        self.user = user
        self.password = password
        self.requested_port = port
        self.port = port or 2121
        self.started = False
        self.stopped = False
        FakeFtpServer.instances.append(self)

    def start(self):
        self.started = True

    def stop(self, timeout=5.0):
        self.stopped = True

    def wait_for_file(self, timeout=60.0, poll_interval=0.5):
        if FakeFtpServer.fail_to_receive:
            return None
        path = self.directory / 'received.dat'
        path.write_bytes(FakeFtpServer.file_content)
        return SimpleNamespace(filename=path.name, path=path, size=path.stat().st_size)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()


def _ok_src_addr(value='172.16.201.99'):
    return SimpleNamespace(ok=True, value=value, reason=None, message=None)


def _fail_src_addr(message='no route to host'):
    return SimpleNamespace(ok=False, value=None, reason=None, message=message)


def _patch(monkeypatch, get_src_addr_result=None):
    FakeFazCli.instances = []
    FakeFtpServer.instances = []
    FakeFtpServer.fail_to_receive = False

    monkeypatch.setattr(faz_mod, 'FazCli', FakeFazCli)
    monkeypatch.setattr(faz_mod, 'SimpleFtpServer', FakeFtpServer)
    monkeypatch.setattr(
        faz_mod, 'get_src_addr',
        lambda dst: get_src_addr_result or _ok_src_addr(),
    )


def _target(**overrides):
    kwargs = dict(addr='172.16.201.210', user='admin', password='P@ssw0rd')
    kwargs.update(overrides)
    return FazTarget(**kwargs)


def test_login_failure_returns_early(monkeypatch, tmp_path):
    _patch(monkeypatch)
    FakeFazCli.instances = []
    orig_init = FakeFazCli.__init__

    def _init_and_fail(self, *a, **kw):
        orig_init(self, *a, **kw)
        self.fail_login = True

    monkeypatch.setattr(FakeFazCli, '__init__', _init_and_fail)

    result = backup_one_faz(_target(), backup_dir=str(tmp_path))

    assert result.status == 'login_ng'
    assert result.ok is False
    assert 'login failed' in result.message
    # ログイン失敗時はコンフィグ/システムバックアップのいずれも試みない。
    assert result.config_status == ''
    assert result.sys_status == ''


def test_full_success_flow_saves_config_and_system_backup(monkeypatch, tmp_path):
    _patch(monkeypatch)
    result = backup_one_faz(_target(), backup_dir=str(tmp_path))

    assert result.status == 'done'
    assert result.hostname == 'FAZ_VM'
    assert result.config_status == 'ok'
    assert result.sys_status == 'ok'
    assert result.ok is True

    assert Path(result.config_saved_path).read_text(encoding='utf-8') == 'faz config dump'
    assert Path(result.sys_saved_path).read_bytes() == FakeFtpServer.file_content

    fgt = FakeFazCli.instances[0]
    assert fgt.logged_out is True


def test_filename_uses_hostname_not_alias(monkeypatch, tmp_path):
    """こうぢさん指定: ファイル名はaliasではなくホスト名を基準にする。"""
    _patch(monkeypatch)
    result = backup_one_faz(_target(alias='my-lab-faz'), backup_dir=str(tmp_path))

    assert 'FAZ_VM' in Path(result.config_saved_path).name
    assert 'my-lab-faz' not in Path(result.config_saved_path).name
    assert 'FAZ_VM' in Path(result.sys_saved_path).name
    assert 'my-lab-faz' not in Path(result.sys_saved_path).name


def test_config_and_system_backup_filenames_include_version(monkeypatch, tmp_path):
    """こうぢさん指定(2026-09-23): 他のバックアップ(FGT/MSW)と同様、ファイル名を
    `<hostname>_<version>_<timestamp>.<ext>`にする。TAC reportにも同日、こうぢさんの
    追加要望によりversionを含めるよう統一した(FGT/MSWも同様、
    `config_backup.fortigate._fetch_tac_report()`のdocstring参照)。"""
    _patch(monkeypatch)
    result = backup_one_faz(_target(tac=True), backup_dir=str(tmp_path))

    assert Path(result.config_saved_path).name.startswith('FAZ_VM_7.4.11_')
    assert Path(result.sys_saved_path).name.startswith('FAZ_VM_7.4.11_')
    assert Path(result.tac_saved_path).name.startswith('FAZ_VM_7.4.11_tacreport_')


def test_config_and_system_backup_use_same_unknown_fallback_when_hostname_empty(monkeypatch, tmp_path):
    """こうぢさん指摘(2026-09-23): コンフィグ/システムバックアップでホスト名の
    フォールバック処理が異なっていた(コンフィグ側は'unknown'、システム側は
    `target.alias or target.addr`)。`get system status`のパース失敗等で
    `hostname`が空のまま渡ってきた場合でも、両者が同じ'unknown'フォールバックに
    統一されていることの回帰テスト。"""
    _patch(monkeypatch)
    orig_init = FakeFazCli.__init__

    def _init_with_empty_hostname(self, *a, **kw):
        orig_init(self, *a, **kw)
        self.hostname_on_login = ''

    monkeypatch.setattr(FakeFazCli, '__init__', _init_with_empty_hostname)

    result = backup_one_faz(_target(addr='172.16.201.210'), backup_dir=str(tmp_path))

    assert Path(result.config_saved_path).name.startswith('unknown_')
    assert Path(result.sys_saved_path).name.startswith('unknown_')
    # アドレスがファイル名に紛れ込む旧実装の挙動が再発していないことの確認。
    assert '172.16.201.210' not in Path(result.sys_saved_path).name


def test_config_backup_failure_does_not_block_system_backup(monkeypatch, tmp_path):
    _patch(monkeypatch)
    orig_init = FakeFazCli.__init__

    def _init_with_failing_show(self, *a, **kw):
        orig_init(self, *a, **kw)
        self.show_result = {'code': 1, 'msg': '[Error] show failed', 'output': ''}

    monkeypatch.setattr(FakeFazCli, '__init__', _init_with_failing_show)

    result = backup_one_faz(_target(), backup_dir=str(tmp_path))

    assert result.config_status == 'ng'
    assert 'show failed' in result.message
    # コンフィグが失敗してもシステムバックアップは独立して試みられる。
    assert result.sys_status == 'ok'
    assert result.ok is False


def test_system_backup_command_failure_is_recorded(monkeypatch, tmp_path):
    _patch(monkeypatch)
    orig_init = FakeFazCli.__init__

    def _init_with_failing_backup(self, *a, **kw):
        orig_init(self, *a, **kw)
        self.backup_all_settings_result = {
            'code': 1, 'msg': "[Error] completion word was 'Failed'", 'output': '',
        }

    monkeypatch.setattr(FakeFazCli, '__init__', _init_with_failing_backup)

    result = backup_one_faz(_target(), backup_dir=str(tmp_path))

    assert result.sys_status == 'ng'
    assert 'Failed' in result.sys_message
    assert result.ok is False
    # コマンド自体が失敗した場合、FTP受信待ちは行わない(ftp_serverはstart/stopのみ)。
    assert result.sys_saved_path == ''


def test_system_backup_no_file_received_is_failure(monkeypatch, tmp_path):
    _patch(monkeypatch)
    FakeFtpServer.fail_to_receive = True

    result = backup_one_faz(_target(), backup_dir=str(tmp_path))

    assert result.sys_status == 'ng'
    assert 'no file was received' in result.sys_message
    assert result.ok is False


def test_get_src_addr_failure_fails_system_backup_without_touching_device(monkeypatch, tmp_path):
    _patch(monkeypatch, get_src_addr_result=_fail_src_addr('no route to host'))

    result = backup_one_faz(_target(), backup_dir=str(tmp_path))

    assert result.sys_status == 'ng'
    assert 'no route to host' in result.sys_message
    # アドレスが決定できない場合、機器へバックアップコマンド自体を送らない。
    assert FakeFazCli.instances[0].backup_all_settings_calls == []


def test_tac_requested_and_succeeds(monkeypatch, tmp_path):
    _patch(monkeypatch)
    result = backup_one_faz(_target(tac=True), backup_dir=str(tmp_path))

    assert result.tac_requested is True
    assert result.tac_status == 'ok'
    assert Path(result.tac_saved_path).read_text(encoding='utf-8') == 'faz tac report dump'
    assert result.ok is True

    fgt = FakeFazCli.instances[0]
    assert fgt.tac_calls == [('FAZ_VM', None)]


def test_tac_requested_and_fails_does_not_affect_config_or_system(monkeypatch, tmp_path):
    _patch(monkeypatch)
    orig_init = FakeFazCli.__init__

    def _init_with_failing_tac(self, *a, **kw):
        orig_init(self, *a, **kw)
        self.tac_result = {'code': 1, 'msg': '[Error] tac failed', 'output': ''}

    monkeypatch.setattr(FakeFazCli, '__init__', _init_with_failing_tac)

    result = backup_one_faz(_target(tac=True), backup_dir=str(tmp_path))

    assert result.tac_status == 'ng'
    assert 'tac failed' in result.tac_message
    assert result.config_status == 'ok'
    assert result.sys_status == 'ok'
    assert result.ok is False


def test_tac_not_requested_leaves_status_empty(monkeypatch, tmp_path):
    _patch(monkeypatch)
    result = backup_one_faz(_target(tac=False), backup_dir=str(tmp_path))

    assert result.tac_requested is False
    assert result.tac_status == ''
    assert result.tac_saved_path == ''


def test_ftp_credentials_are_random_when_unspecified(monkeypatch, tmp_path):
    _patch(monkeypatch)
    r1 = backup_one_faz(_target(addr='172.16.201.210'), backup_dir=str(tmp_path))
    r2 = backup_one_faz(_target(addr='172.16.201.211'), backup_dir=str(tmp_path))

    call1 = FakeFazCli.instances[0].backup_all_settings_calls[0]
    call2 = FakeFazCli.instances[1].backup_all_settings_calls[0]

    assert call1['ftp_user'] and call1['ftp_password']
    assert call1['ftp_user'] != call2['ftp_user']
    assert call1['ftp_password'] != call2['ftp_password']
    assert r1.ok and r2.ok


def test_explicit_ftp_credentials_are_used_as_is(monkeypatch, tmp_path):
    _patch(monkeypatch)
    backup_one_faz(_target(ftp_user='nwadmin', ftp_password='P@ssw0rd'), backup_dir=str(tmp_path))

    call = FakeFazCli.instances[0].backup_all_settings_calls[0]
    assert call['ftp_user'] == 'nwadmin'
    assert call['ftp_password'] == 'P@ssw0rd'


def test_config_password_falls_back_to_login_password(monkeypatch, tmp_path):
    _patch(monkeypatch)
    backup_one_faz(_target(password='LoginPw', config_password=''), backup_dir=str(tmp_path))

    call = FakeFazCli.instances[0].backup_all_settings_calls[0]
    assert call['config_password'] == 'LoginPw'


def test_explicit_config_password_overrides_login_password(monkeypatch, tmp_path):
    _patch(monkeypatch)
    backup_one_faz(_target(password='LoginPw', config_password='ConfigPw'), backup_dir=str(tmp_path))

    call = FakeFazCli.instances[0].backup_all_settings_calls[0]
    assert call['config_password'] == 'ConfigPw'


def test_ftp_port_is_forwarded_to_backup_command(monkeypatch, tmp_path):
    _patch(monkeypatch)
    backup_one_faz(_target(ftp_port=2222), backup_dir=str(tmp_path))

    ftp_server = FakeFtpServer.instances[0]
    assert ftp_server.requested_port == 2222

    call = FakeFazCli.instances[0].backup_all_settings_calls[0]
    assert call['ftp_port'] == ftp_server.port


def test_default_backup_dir_used_when_unspecified(monkeypatch, tmp_path):
    _patch(monkeypatch)
    # DEFAULT_BACKUP_DIR('./booyaa_log/config')は相対パスのため、カレントディレクトリを
    # tmp_pathに切り替えて実ファイルシステムを汚さないようにする。
    monkeypatch.chdir(tmp_path)
    result = backup_one_faz(_target(), backup_dir=None)
    assert result.backup_dir == faz_mod.DEFAULT_BACKUP_DIR


def test_explicit_directory_overrides_default(monkeypatch, tmp_path):
    _patch(monkeypatch)
    result = backup_one_faz(_target(), backup_dir=str(tmp_path / 'explicit'))
    assert result.backup_dir == str(tmp_path / 'explicit')


def test_target_backup_dir_overrides_cli_backup_dir(monkeypatch, tmp_path):
    _patch(monkeypatch)
    result = backup_one_faz(
        _target(backup_dir=str(tmp_path / 'from_target')), backup_dir=str(tmp_path / 'from_cli'),
    )
    assert result.backup_dir == str(tmp_path / 'from_target')


def test_run_faz_backup_processes_all_targets_sequentially(monkeypatch, tmp_path):
    _patch(monkeypatch)
    targets = [_target(addr='172.16.201.210'), _target(addr='172.16.201.211')]

    results = run_faz_backup(targets, backup_dir=str(tmp_path))

    assert len(results) == 2
    assert all(r.ok for r in results)
    assert len(FakeFazCli.instances) == 2


def test_progress_callback_is_notified_on_each_stage_change(monkeypatch, tmp_path):
    _patch(monkeypatch)
    statuses = []

    def on_progress(result):
        statuses.append((result.status, result.config_status, result.sys_status, result.tac_status))

    backup_one_faz(_target(tac=True), backup_dir=str(tmp_path), progress_callback=on_progress)

    assert ('connecting', '', '', '') in statuses
    assert any(s[0] == 'login_ok' for s in statuses)
    assert any(s[1] == 'backing_up' for s in statuses)
    assert any(s[2] == 'backing_up' for s in statuses)
    assert any(s[3] == 'fetching' for s in statuses)
    assert statuses[-1] == ('done', 'ok', 'ok', 'ok')


def test_load_faz_list_csv_parses_all_columns(tmp_path):
    csv_path = tmp_path / 'targets.csv'
    csv_path.write_text(
        'addr,user,password,alias,config_password,ftp_user,ftp_password,ftp_port,backup_dir,ssh_port,tac\n'
        '172.16.201.210,admin,P@ssw0rd,Lab-FAZ,CfgPw,ftpuser,ftppw,2222,./custom,2222,yes\n',
        encoding='utf-8',
    )

    targets = load_faz_list_csv(csv_path)

    assert len(targets) == 1
    t = targets[0]
    assert t.addr == '172.16.201.210'
    assert t.user == 'admin'
    assert t.password == 'P@ssw0rd'
    assert t.alias == 'Lab-FAZ'
    assert t.config_password == 'CfgPw'
    assert t.ftp_user == 'ftpuser'
    assert t.ftp_password == 'ftppw'
    assert t.ftp_port == 2222
    assert t.backup_dir == './custom'
    assert t.ssh_port == 2222
    assert t.tac is True


def test_load_faz_list_csv_defaults_when_columns_missing(tmp_path):
    csv_path = tmp_path / 'targets.csv'
    csv_path.write_text('172.16.201.210,admin,P@ssw0rd\n', encoding='utf-8')

    targets = load_faz_list_csv(csv_path)

    assert len(targets) == 1
    t = targets[0]
    assert t.ftp_port == faz_mod.FTP_DEFAULT_PORT
    assert t.ssh_port == 22
    assert t.tac is False


def test_load_faz_list_csv_missing_file_raises():
    import pytest
    with pytest.raises(FileNotFoundError):
        load_faz_list_csv('/no/such/file.csv')
