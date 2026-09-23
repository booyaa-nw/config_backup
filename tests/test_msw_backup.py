"""backup_one_fgt()/run_msw_backup()のオーケストレーションロジックのテスト。

実機接続(net_config.ftnt.fgt.cli.FgtCli / net_config.ftnt.msw.cli.MswCli)は
フェイクに差し替える。
"""
from pathlib import Path
from types import SimpleNamespace

import config_backup.msw as msw_mod
from config_backup.msw import MswBackupItem, MswTarget, backup_one_fgt, load_msw_list_csv, run_msw_backup
from net_config.ftnt.fgt.cli.execute.switch_controller_conn_status import MswConnStatus


def _switch(serial='S1', hostname='-', addr='10.255.1.1', auth_state='Authorized', status='Up'):
    return MswConnStatus(serial=serial, version='7.4.3', auth_state=auth_state,
                          status=status, flag='-', addr=addr, join_time='N/A',
                          hostname=hostname)


class FakeFgtCli:
    """net_config.ftnt.fgt.cli.FgtCliの必要最小限を模したフェイク。"""

    instances = []

    def __init__(self, fgt_info=None, timeout=30.0):
        self.fgt_info = fgt_info or SimpleNamespace()
        self.fgt_info.hostname = ''
        self.fgt_info.msw_list = []

        self.fail_login = False
        self.fail_listing = False
        self.switch_entries = []
        self.logged_out = False

        FakeFgtCli.instances.append(self)

    def set_target(self, **kwargs):
        self.fgt_info.alias = kwargs.get('fgt_alias', '')

    def login(self):
        if self.fail_login:
            return {'code': 1, 'msg': '[Error] login failed', 'output': ''}
        self.fgt_info.hostname = 'LABFG01'
        return {'code': 0, 'msg': '', 'output': ''}

    def get_switch_list(self):
        if self.fail_listing:
            return {'code': 1, 'msg': '[Error] listing failed', 'output': ''}
        self.fgt_info.msw_list = self.switch_entries
        return {'code': 0, 'msg': '', 'output': ''}

    def logout(self):
        self.logged_out = True
        return {'code': 0, 'msg': '', 'output': ''}


class FakeMswTacReport:
    def __init__(self, outer):
        self.outer = outer
        self.calls = []

    def get(self, expected_hostname, timeout=None):
        self.calls.append((expected_hostname, timeout))
        if self.outer.fail_tac:
            return {'code': 1, 'msg': '[Error] msw tac failed', 'output': ''}
        return {'code': 0, 'msg': '', 'output': 'msw tac report dump'}


class FakeMswCli:
    """net_config.ftnt.msw.cli.MswCliの必要最小限を模したフェイク。

    振る舞い(ログイン/バックアップ/TAC成否)は接続先アドレスごとに
    `FakeMswCli.behavior_by_addr`で制御する(複数スイッチをまとめて処理する
    テストで、スイッチごとに異なる結果を注入できるようにするため)。
    """

    instances = []
    behavior_by_addr: dict = {}

    def __init__(self, fgt_cli, msw_info=None):
        self.fgt_cli = fgt_cli
        self.msw_info = msw_info
        behavior = FakeMswCli.behavior_by_addr.get(msw_info.addr, {})
        self.fail_login = behavior.get('fail_login', False)
        self.fail_backup = behavior.get('fail_backup', False)
        self.fail_tac = behavior.get('fail_tac', False)
        self.hostname_override = behavior.get('hostname', 'LABMSW01')

        self.execute = SimpleNamespace(tac_report=FakeMswTacReport(self))
        self.logged_out = False

        FakeMswCli.instances.append(self)

    def login(self, expected_serial=None):
        # `expected_serial`は`net_config.ftnt.msw.cli.MswCli.login()`の照合機能
        # (こうぢさん指定、2026-09-22追加)のフェイク版。実際の照合ロジック自体は
        # net_config側でテスト済みのため、ここでは呼び出し元(`config_backup.msw`)が
        # 正しく渡していることだけを記録・検証する。
        self.login_expected_serial = expected_serial
        if self.fail_login:
            return {'code': 1, 'msg': '[Error] msw login failed', 'output': ''}
        self.msw_info.hostname = self.hostname_override
        self.msw_info.version = '7.4.3'
        return {'code': 0, 'msg': '', 'output': ''}

    @property
    def show(self):
        outer = self

        class _Show:
            def get(self, full=False):
                if outer.fail_backup:
                    return {'code': 1, 'msg': '[Error] msw backup failed', 'output': ''}
                return {'code': 0, 'msg': '', 'output': 'msw config dump'}

        return _Show()

    def logout(self):
        self.logged_out = True
        return {'code': 0, 'msg': '', 'output': ''}


def _patch(monkeypatch):
    FakeFgtCli.instances = []
    FakeMswCli.instances = []
    FakeMswCli.behavior_by_addr = {}
    monkeypatch.setattr(msw_mod, 'FgtCli', FakeFgtCli)
    monkeypatch.setattr(msw_mod, 'MswCli', FakeMswCli)


def test_backup_one_fgt_success_single_switch(tmp_path, monkeypatch):
    _patch(monkeypatch)
    FakeFgtCli.instances  # noqa
    target = MswTarget(fgt_addr='172.16.201.207', fgt_user='admin', fgt_password='pw')

    def fake_login(self):
        self.fgt_info.hostname = 'LABFG01'
        self.switch_entries = [_switch()]
        return {'code': 0, 'msg': '', 'output': ''}

    monkeypatch.setattr(FakeFgtCli, 'login', fake_login)

    result = backup_one_fgt(target, backup_dir=str(tmp_path))

    assert result.status == 'done'
    assert result.ok is True
    assert len(result.items) == 1
    item = result.items[0]
    assert item.eligible is True
    assert item.status == 'backup_ok'
    assert item.saved_path
    assert Path(item.saved_path).read_text(encoding='utf-8') == 'msw config dump'
    assert 'success' in item.message
    assert FakeFgtCli.instances[0].logged_out is True
    assert FakeMswCli.instances[0].logged_out is True


def test_backup_one_switch_passes_expected_serial_to_login(tmp_path, monkeypatch):
    """`_backup_one_switch()`が`MswCli.login()`へ`expected_serial`(`sw.serial`)を
    渡していることの回帰テスト(こうぢさん指定の照合機能、2026-09-22追加)。"""
    _patch(monkeypatch)
    target = MswTarget(fgt_addr='172.16.201.207', fgt_user='admin', fgt_password='pw')

    def fake_login(self):
        self.fgt_info.hostname = 'LABFG01'
        self.switch_entries = [_switch(serial='S224EPTF20005577')]
        return {'code': 0, 'msg': '', 'output': ''}

    monkeypatch.setattr(FakeFgtCli, 'login', fake_login)

    backup_one_fgt(target, backup_dir=str(tmp_path))

    assert FakeMswCli.instances[0].login_expected_serial == 'S224EPTF20005577'


def test_non_eligible_switch_is_skipped_without_login(tmp_path, monkeypatch):
    _patch(monkeypatch)
    target = MswTarget(fgt_addr='172.16.201.207', fgt_user='admin', fgt_password='pw')

    fgt = FakeFgtCli()
    fgt.switch_entries = [_switch(serial='S2', auth_state='Discovered', status='Down')]
    monkeypatch.setattr(msw_mod, 'FgtCli', lambda *a, **kw: fgt)

    result = backup_one_fgt(target, backup_dir=str(tmp_path))

    assert len(result.items) == 1
    item = result.items[0]
    assert item.eligible is False
    assert item.status == 'skipped'
    assert 'Discovered/Down' in item.message
    assert item.ok is True  # 対象外は失敗としては数えない
    assert result.ok is True
    assert FakeMswCli.instances == []  # ログインを試みていないこと


def test_skip_message_is_orange(tmp_path, monkeypatch):
    """スキップ時のMessageはオレンジで表示する(こうぢさん指定、2026-09-22追加)。"""
    _patch(monkeypatch)
    target = MswTarget(fgt_addr='172.16.201.207', fgt_user='admin', fgt_password='pw')

    fgt = FakeFgtCli()
    fgt.switch_entries = [_switch(serial='S2', auth_state='Discovered', status='Down')]
    monkeypatch.setattr(msw_mod, 'FgtCli', lambda *a, **kw: fgt)

    result = backup_one_fgt(target, backup_dir=str(tmp_path))

    item = result.items[0]
    assert item.message.startswith('[orange3]')
    assert item.message.endswith('[/]')
    assert 'Discovered/Down' in item.message


def test_display_status_abbreviates_authorized_and_deauthorized():
    """列幅の都合でAuth stateが省略表示されないよう短縮する(こうぢさん指定、2026-09-22追加)。"""
    assert MswBackupItem(auth_state='Authorized', conn_status='Up').display_status == 'Auth/Up'
    assert MswBackupItem(auth_state='Deauthorized', conn_status='Down').display_status == 'Deauth/Down'
    # 大文字小文字は問わない
    assert MswBackupItem(auth_state='authorized', conn_status='Up').display_status == 'Auth/Up'


def test_display_status_leaves_other_auth_states_unabbreviated():
    """"Discovered"等、短縮対象外の値はそのまま表示する。"""
    assert MswBackupItem(auth_state='Discovered', conn_status='Down').display_status == 'Discovered/Down'


def test_multiple_switches_mixed_results(tmp_path, monkeypatch):
    _patch(monkeypatch)
    target = MswTarget(fgt_addr='172.16.201.207', fgt_user='admin', fgt_password='pw')

    fgt = FakeFgtCli()
    fgt.switch_entries = [
        _switch(serial='S1', addr='10.255.1.1'),
        _switch(serial='S2', addr='10.255.1.2', auth_state='Discovered', status='Down'),
        _switch(serial='S3', addr='10.255.1.3'),
    ]
    monkeypatch.setattr(msw_mod, 'FgtCli', lambda *a, **kw: fgt)
    FakeMswCli.behavior_by_addr = {'10.255.1.3': {'fail_login': True}}

    result = backup_one_fgt(target, backup_dir=str(tmp_path))

    assert [item.status for item in result.items] == ['backup_ok', 'skipped', 'login_ng']
    assert result.ok is False
    # S2(skip)にはログインを試みず、S1/S3のみMswCliが作られること
    assert len(FakeMswCli.instances) == 2


def test_fgt_login_failure(tmp_path, monkeypatch):
    _patch(monkeypatch)
    target = MswTarget(fgt_addr='172.16.201.207', fgt_user='admin', fgt_password='wrong')

    fgt = FakeFgtCli()
    fgt.fail_login = True
    monkeypatch.setattr(msw_mod, 'FgtCli', lambda *a, **kw: fgt)

    result = backup_one_fgt(target, backup_dir=str(tmp_path))

    assert result.status == 'login_ng'
    assert result.ok is False
    assert result.items == []
    assert 'login failed' in result.message


def test_switch_listing_failure(tmp_path, monkeypatch):
    _patch(monkeypatch)
    target = MswTarget(fgt_addr='172.16.201.207', fgt_user='admin', fgt_password='pw')

    fgt = FakeFgtCli()
    fgt.fail_listing = True
    monkeypatch.setattr(msw_mod, 'FgtCli', lambda *a, **kw: fgt)

    result = backup_one_fgt(target, backup_dir=str(tmp_path))

    assert result.status == 'listing_ng'
    assert result.ok is False
    assert 'listing failed' in result.message
    assert fgt.logged_out is True  # 一覧取得失敗時もFGTからはログアウトすること


def test_default_backup_dir_uses_fgt_hostname(tmp_path, monkeypatch):
    """MSWの既定バックアップ先は`./booyaa_log/config/<fgtのhostname>_msw`
    (2026-09-22変更、こうぢさん指定: FGT本体と同じ`config/`配下に統一)。"""
    _patch(monkeypatch)
    target = MswTarget(fgt_addr='172.16.201.207', fgt_user='admin', fgt_password='pw')

    fgt = FakeFgtCli()
    fgt.switch_entries = [_switch()]
    monkeypatch.setattr(msw_mod, 'FgtCli', lambda *a, **kw: fgt)

    result = backup_one_fgt(target, backup_dir=None)

    assert result.backup_dir == './booyaa_log/config/LABFG01_msw'


def test_explicit_directory_overrides_default(tmp_path, monkeypatch):
    _patch(monkeypatch)
    target = MswTarget(fgt_addr='172.16.201.207', fgt_user='admin', fgt_password='pw')

    fgt = FakeFgtCli()
    fgt.switch_entries = [_switch()]
    monkeypatch.setattr(msw_mod, 'FgtCli', lambda *a, **kw: fgt)

    result = backup_one_fgt(target, backup_dir=str(tmp_path))

    assert result.backup_dir == str(tmp_path)


def test_per_target_csv_backup_dir_overrides_cli_directory(tmp_path, monkeypatch):
    _patch(monkeypatch)
    target = MswTarget(fgt_addr='172.16.201.207', fgt_user='admin', fgt_password='pw',
                        backup_dir=str(tmp_path / 'from_csv'))

    fgt = FakeFgtCli()
    fgt.switch_entries = [_switch()]
    monkeypatch.setattr(msw_mod, 'FgtCli', lambda *a, **kw: fgt)

    result = backup_one_fgt(target, backup_dir=str(tmp_path / 'from_cli'))

    assert result.backup_dir == str(tmp_path / 'from_csv')


def test_msw_password_falls_back_to_fgt_password(tmp_path, monkeypatch):
    _patch(monkeypatch)
    target = MswTarget(fgt_addr='172.16.201.207', fgt_user='admin', fgt_password='fgtpw',
                        msw_password='')

    fgt = FakeFgtCli()
    fgt.switch_entries = [_switch()]
    monkeypatch.setattr(msw_mod, 'FgtCli', lambda *a, **kw: fgt)

    backup_one_fgt(target, backup_dir=str(tmp_path))

    assert FakeMswCli.instances[0].msw_info.password == 'fgtpw'


def test_msw_password_explicit_overrides_fgt_password(tmp_path, monkeypatch):
    _patch(monkeypatch)
    target = MswTarget(fgt_addr='172.16.201.207', fgt_user='admin', fgt_password='fgtpw',
                        msw_password='mswpw')

    fgt = FakeFgtCli()
    fgt.switch_entries = [_switch()]
    monkeypatch.setattr(msw_mod, 'FgtCli', lambda *a, **kw: fgt)

    backup_one_fgt(target, backup_dir=str(tmp_path))

    assert FakeMswCli.instances[0].msw_info.password == 'mswpw'


# --- TAC report(`--tac`)---------------------------------------------------

def test_tac_report_success(tmp_path, monkeypatch):
    _patch(monkeypatch)
    target = MswTarget(fgt_addr='172.16.201.207', fgt_user='admin', fgt_password='pw', tac=True)

    fgt = FakeFgtCli()
    fgt.switch_entries = [_switch()]
    monkeypatch.setattr(msw_mod, 'FgtCli', lambda *a, **kw: fgt)

    result = backup_one_fgt(target, backup_dir=str(tmp_path))

    item = result.items[0]
    assert item.tac_requested is True
    assert item.tac_status == 'ok'
    assert item.tac_saved_path
    assert 'tacreport' in item.tac_saved_path
    assert Path(item.tac_saved_path).read_text(encoding='utf-8') == 'msw tac report dump'
    assert result.ok is True


def test_tac_report_filename_includes_version(tmp_path, monkeypatch):
    """こうぢさん指定(2026-09-23): コンフィグバックアップと同様、TAC reportの
    ファイル名にもMSWのversionを含める(FGT/FAZも同様に統一、
    `config_backup.fortigate._fetch_tac_report()`のdocstring参照)。"""
    _patch(monkeypatch)
    target = MswTarget(fgt_addr='172.16.201.207', fgt_user='admin', fgt_password='pw', tac=True)

    fgt = FakeFgtCli()
    fgt.switch_entries = [_switch()]
    monkeypatch.setattr(msw_mod, 'FgtCli', lambda *a, **kw: fgt)

    result = backup_one_fgt(target, backup_dir=str(tmp_path))

    item = result.items[0]
    assert '_7.4.3_tacreport_' in Path(item.tac_saved_path).name


def test_tac_report_failure_marks_item_not_ok(tmp_path, monkeypatch):
    _patch(monkeypatch)
    target = MswTarget(fgt_addr='172.16.201.207', fgt_user='admin', fgt_password='pw', tac=True)

    fgt = FakeFgtCli()
    fgt.switch_entries = [_switch()]
    monkeypatch.setattr(msw_mod, 'FgtCli', lambda *a, **kw: fgt)
    FakeMswCli.behavior_by_addr = {'10.255.1.1': {'fail_tac': True}}

    result = backup_one_fgt(target, backup_dir=str(tmp_path))

    item = result.items[0]
    assert item.status == 'backup_ok'  # コンフィグ本体は成功
    assert item.tac_status == 'ng'
    assert 'msw tac failed' in item.tac_message
    assert item.ok is False
    assert result.ok is False


def test_tac_not_requested_leaves_status_empty(tmp_path, monkeypatch):
    _patch(monkeypatch)
    target = MswTarget(fgt_addr='172.16.201.207', fgt_user='admin', fgt_password='pw')

    fgt = FakeFgtCli()
    fgt.switch_entries = [_switch()]
    monkeypatch.setattr(msw_mod, 'FgtCli', lambda *a, **kw: fgt)

    result = backup_one_fgt(target, backup_dir=str(tmp_path))

    item = result.items[0]
    assert item.tac_requested is False
    assert item.tac_status == ''
    assert FakeMswCli.instances[0].execute.tac_report.calls == []


def test_progress_callback_reports_transitions(tmp_path, monkeypatch):
    _patch(monkeypatch)
    target = MswTarget(fgt_addr='172.16.201.207', fgt_user='admin', fgt_password='pw')

    fgt = FakeFgtCli()
    fgt.switch_entries = [_switch()]
    monkeypatch.setattr(msw_mod, 'FgtCli', lambda *a, **kw: fgt)

    statuses = []
    backup_one_fgt(
        target, backup_dir=str(tmp_path),
        progress_callback=lambda result: statuses.append(
            (result.status, [item.status for item in result.items])
        ),
    )

    assert statuses == [
        ('connecting', []),
        ('login_ok', []),
        ('login_ok', ['connecting']),
        ('login_ok', ['login_ok']),
        ('login_ok', ['backup_ok']),
        ('done', ['backup_ok']),
    ]


def test_run_msw_backup_processes_all_targets(tmp_path, monkeypatch):
    _patch(monkeypatch)
    targets = [
        MswTarget(fgt_addr='172.16.201.207', fgt_user='admin', fgt_password='pw'),
        MswTarget(fgt_addr='172.16.201.208', fgt_user='admin', fgt_password='pw'),
    ]

    def factory(*a, **kw):
        fgt = FakeFgtCli()
        fgt.switch_entries = [_switch()]
        return fgt

    monkeypatch.setattr(msw_mod, 'FgtCli', factory)

    results = run_msw_backup(targets, backup_dir=str(tmp_path))
    assert len(results) == 2
    assert all(r.ok for r in results)


# --- CSV loader -------------------------------------------------------------

def test_load_msw_list_csv(tmp_path):
    csv_path = tmp_path / 'target.csv'
    csv_path.write_text(
        'fgt_addr,fgt_user,fgt_password,alias,msw_user,msw_password,backup_dir,ssh_port,tac\n'
        '172.16.201.207,admin,P@ssw0rd!!!!,Lab-FG01,admin,P@ssw0rd,,,yes\n'
        '192.0.2.1,nw_admin,pw2\n',
        encoding='utf-8',
    )

    targets = load_msw_list_csv(csv_path)

    assert len(targets) == 2
    assert targets[0].fgt_addr == '172.16.201.207'
    assert targets[0].fgt_alias == 'Lab-FG01'
    assert targets[0].msw_user == 'admin'
    assert targets[0].msw_password == 'P@ssw0rd'
    assert targets[0].fgt_ssh_port == 22
    assert targets[0].tac is True

    assert targets[1].fgt_addr == '192.0.2.1'
    assert targets[1].msw_user == 'admin'  # 未指定時のデフォルト
    assert targets[1].tac is False


def test_load_msw_list_csv_missing_file(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        load_msw_list_csv(tmp_path / 'nope.csv')
