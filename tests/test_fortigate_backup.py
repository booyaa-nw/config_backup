"""backup_one()/run_fortigate_backup()のオーケストレーションロジックのテスト。

実機接続(net_config.ftnt.fgt.cli.FgtCli)はフェイクに差し替える。
"""
from types import SimpleNamespace

import config_backup.fortigate as fortigate_mod
from config_backup.fortigate import FortigateTarget, backup_one, run_fortigate_backup


class FakeShow:
    def __init__(self, outer):
        self.outer = outer

    def get(self, full=False):
        if self.outer.fail_backup:
            return {'code': 1, 'msg': '[Error] backup failed', 'output': ''}
        return {'code': 0, 'msg': '', 'output': 'config dump'}


class FakeTacReport:
    """net_config.ftnt.fgt.cli.execute.TacReportの必要最小限を模したフェイク。"""

    def __init__(self, outer):
        self.outer = outer
        self.calls = []
        self.hostnames = []

    def get(self, expected_hostname, timeout=None):
        self.calls.append(timeout)
        self.hostnames.append(expected_hostname)
        if self.outer.fail_tac:
            return {'code': 1, 'msg': '[Error] tac report failed', 'output': ''}
        return {'code': 0, 'msg': '', 'output': 'tac report dump'}


class FakeExecute:
    def __init__(self, outer):
        self.tac_report = FakeTacReport(outer)


class FakeFgtCli:
    """net_config.ftnt.fgt.cli.FgtCliの必要最小限を模したフェイク。"""

    instances = []

    def __init__(self, fgt_info=None, timeout=30.0):
        self.fgt_info = fgt_info or SimpleNamespace()
        self.fgt_info.hostname = ''
        self.fgt_info.version = '7.4.5'
        self.fgt_info.exsist_secondary = False
        self.fgt_info.secondary_hostname = ''

        self.fail_login = False
        self.fail_backup = False
        self.fail_secondary_login = False
        self.fail_secondary_backup = False
        self.fail_tac = False

        self.show = FakeShow(self)
        self.execute = FakeExecute(self)
        self.logged_out = False
        self.secondary_logged_out = False

        FakeFgtCli.instances.append(self)

    def set_target(self, **kwargs):
        self.fgt_info.alias = kwargs.get('fgt_alias', '')

    def login(self):
        if self.fail_login:
            return {'code': 1, 'msg': '[Error] login failed', 'output': ''}
        self.fgt_info.hostname = 'LABFG01'
        return {'code': 0, 'msg': '', 'output': ''}

    def login_secondary(self):
        if self.fail_secondary_login:
            return {'code': 1, 'msg': '[Error] secondary login failed', 'output': ''}
        self.fgt_info.secondary_hostname = 'LABFG02'
        return {'code': 0, 'msg': '', 'output': ''}

    def logout_secondary(self):
        self.secondary_logged_out = True
        return {'code': 0, 'msg': '', 'output': ''}

    def logout(self):
        self.logged_out = True
        return {'code': 0, 'msg': '', 'output': ''}


def _patch_fgtcli(monkeypatch):
    FakeFgtCli.instances = []
    monkeypatch.setattr(fortigate_mod, 'FgtCli', FakeFgtCli)


def test_backup_one_success(tmp_path, monkeypatch):
    _patch_fgtcli(monkeypatch)
    target = FortigateTarget(addr='172.16.201.201', user='admin', password='pw', alias='Lab-FG01')

    result = backup_one(target, backup_dir=str(tmp_path))

    assert result.ok is True
    assert result.login_ok is True
    assert result.backup_ok is True
    assert result.hostname == 'LABFG01'
    assert result.saved_path
    assert 'success' in result.message
    assert FakeFgtCli.instances[0].logged_out is True


def test_backup_one_login_failure(tmp_path, monkeypatch):
    _patch_fgtcli(monkeypatch)
    target = FortigateTarget(addr='172.16.201.201', user='admin', password='wrong')

    FakeFgtCli_orig_init = FakeFgtCli.__init__

    def failing_init(self, *a, **kw):
        FakeFgtCli_orig_init(self, *a, **kw)
        self.fail_login = True

    monkeypatch.setattr(FakeFgtCli, '__init__', failing_init)

    result = backup_one(target, backup_dir=str(tmp_path))

    assert result.ok is False
    assert result.login_ok is False
    assert result.backup_ok is False
    assert 'login failed' in result.message


def test_backup_one_backup_command_failure(tmp_path, monkeypatch):
    _patch_fgtcli(monkeypatch)

    orig_init = FakeFgtCli.__init__

    def init_with_backup_failure(self, *a, **kw):
        orig_init(self, *a, **kw)
        self.fail_backup = True

    monkeypatch.setattr(FakeFgtCli, '__init__', init_with_backup_failure)

    target = FortigateTarget(addr='172.16.201.201', user='admin', password='pw')
    result = backup_one(target, backup_dir=str(tmp_path))

    assert result.login_ok is True
    assert result.backup_ok is False
    assert result.ok is False
    # バックアップ失敗時もログアウトはされること
    assert FakeFgtCli.instances[0].logged_out is True


def test_backup_one_with_secondary_success(tmp_path, monkeypatch):
    _patch_fgtcli(monkeypatch)

    orig_init = FakeFgtCli.__init__

    def init_with_secondary(self, *a, **kw):
        orig_init(self, *a, **kw)
        self.fgt_info.exsist_secondary = True

    monkeypatch.setattr(FakeFgtCli, '__init__', init_with_secondary)

    target = FortigateTarget(addr='172.16.201.201', user='admin', password='pw', get_secondary=True)
    result = backup_one(target, backup_dir=str(tmp_path))

    assert result.ok is True
    assert result.secondary_requested is True
    assert result.secondary_ok is True
    assert result.secondary_hostname == 'LABFG02'
    assert result.secondary_saved_path
    assert 'success' in result.message
    assert 'success' in result.secondary_message
    assert FakeFgtCli.instances[0].secondary_logged_out is True


def test_backup_one_secondary_requested_but_not_available(tmp_path, monkeypatch):
    _patch_fgtcli(monkeypatch)
    # exsist_secondary はデフォルトFalseのまま
    target = FortigateTarget(addr='172.16.201.201', user='admin', password='pw', get_secondary=True)
    result = backup_one(target, backup_dir=str(tmp_path))

    assert result.login_ok is True
    assert result.backup_ok is True
    assert result.secondary_requested is True
    assert result.secondary_ok is False
    assert result.ok is False
    # 2026-09-16変更: Secondary固有のメッセージはPrimaryの`message`ではなく
    # `secondary_message`に入る(テーブル上でPrimary行とSecondary行に同じ
    # メッセージが重複表示されるのを避けるため)。
    assert 'Secondary node not available' in result.secondary_message
    # Primary自体のバックアップは成功しているので、Primary行には
    # successが表示される(2026-09-16、成功時のmessage表示要望への対応)。
    assert 'success' in result.message


def test_run_fortigate_backup_processes_all_targets(tmp_path, monkeypatch):
    _patch_fgtcli(monkeypatch)
    targets = [
        FortigateTarget(addr='172.16.201.201', user='admin', password='pw'),
        FortigateTarget(addr='172.16.201.202', user='admin', password='pw'),
    ]
    results = run_fortigate_backup(targets, backup_dir=str(tmp_path))
    assert len(results) == 2
    assert all(r.ok for r in results)


def test_backup_one_progress_callback_reports_status_transitions(tmp_path, monkeypatch):
    # rich.LiveによるリアルタイムTUI表示(2026-09-16対応)の土台となる
    # progress_callback が、result.status の遷移を想定した順序で通知すること。
    # 2026-09-16変更: callbackの引数は`(target, stage)`から、表示に必要な情報
    # (hostname等)を持つ`result`そのもの1つに変更した(「実行中の表示と結果表示を
    # 同じ描画関数にしたい」という要望への対応、fortigate.ProgressCallback参照)。
    _patch_fgtcli(monkeypatch)
    target = FortigateTarget(addr='172.16.201.201', user='admin', password='pw')

    statuses = []
    backup_one(
        target, backup_dir=str(tmp_path),
        progress_callback=lambda result: statuses.append((result.target.addr, result.status)),
    )

    assert statuses == [
        ('172.16.201.201', 'connecting'),
        ('172.16.201.201', 'login_ok'),
        ('172.16.201.201', 'backup_ok'),
    ]


def test_backup_one_progress_callback_reports_secondary_status_transitions(tmp_path, monkeypatch):
    _patch_fgtcli(monkeypatch)

    orig_init = FakeFgtCli.__init__

    def init_with_secondary(self, *a, **kw):
        orig_init(self, *a, **kw)
        self.fgt_info.exsist_secondary = True
        self.fgt_info.secondary_hostname = 'LABFG02'

    monkeypatch.setattr(FakeFgtCli, '__init__', init_with_secondary)

    target = FortigateTarget(addr='172.16.201.201', user='admin', password='pw', get_secondary=True)

    snapshots = []
    backup_one(
        target, backup_dir=str(tmp_path),
        progress_callback=lambda result: snapshots.append(
            (result.status, result.secondary_status, result.secondary_hostname)
        ),
    )

    assert snapshots == [
        ('connecting', '', ''),
        ('login_ok', 'known', 'LABFG02'),
        ('backup_ok', 'known', 'LABFG02'),
        ('backup_ok', 'connecting', 'LABFG02'),
        ('backup_ok', 'backup_ok', 'LABFG02'),
    ]


def test_backup_one_progress_callback_reports_secondary_not_available(tmp_path, monkeypatch):
    # Secondaryが存在しない場合、ホスト名が判明するのを待たずに
    # 'not_available'として即座に通知されること。
    _patch_fgtcli(monkeypatch)
    target = FortigateTarget(addr='172.16.201.201', user='admin', password='pw', get_secondary=True)

    statuses = []
    backup_one(
        target, backup_dir=str(tmp_path),
        progress_callback=lambda result: statuses.append(result.secondary_status),
    )

    assert statuses == ['', 'not_available', 'not_available']


# --- TAC report(`--tac`, 2026-09-16追加)---------------------------------

def test_backup_one_with_tac_success(tmp_path, monkeypatch):
    _patch_fgtcli(monkeypatch)
    target = FortigateTarget(addr='172.16.201.201', user='admin', password='pw', tac=True)

    result = backup_one(target, backup_dir=str(tmp_path))

    assert result.ok is True
    assert result.tac_requested is True
    assert result.tac_ok is True
    assert result.tac_status == 'ok'
    assert result.tac_saved_path
    assert 'tacreport' in result.tac_saved_path
    from pathlib import Path
    assert Path(result.tac_saved_path).read_text(encoding='utf-8') == 'tac report dump'


def test_tac_report_filename_includes_version(tmp_path, monkeypatch):
    """こうぢさん指定(2026-09-23): コンフィグバックアップと同様、TAC reportの
    ファイル名にもversionを含める(MSW/FAZも同様に統一、
    `config_backup.fortigate._fetch_tac_report()`のdocstring参照)。"""
    _patch_fgtcli(monkeypatch)
    target = FortigateTarget(addr='172.16.201.201', user='admin', password='pw', tac=True)

    result = backup_one(target, backup_dir=str(tmp_path))

    from pathlib import Path
    assert '_7.4.5_tacreport_' in Path(result.tac_saved_path).name


def test_backup_one_with_tac_failure(tmp_path, monkeypatch):
    _patch_fgtcli(monkeypatch)

    orig_init = FakeFgtCli.__init__

    def init_with_tac_failure(self, *a, **kw):
        orig_init(self, *a, **kw)
        self.fail_tac = True

    monkeypatch.setattr(FakeFgtCli, '__init__', init_with_tac_failure)

    target = FortigateTarget(addr='172.16.201.201', user='admin', password='pw', tac=True)
    result = backup_one(target, backup_dir=str(tmp_path))

    # コンフィグバックアップ自体は成功しているが、TAC report取得が失敗しているため
    # 全体としては失敗扱いになること。
    assert result.backup_ok is True
    assert result.tac_ok is False
    assert result.tac_status == 'ng'
    assert 'tac report failed' in result.tac_message
    assert result.ok is False


def test_backup_one_with_secondary_and_tac(tmp_path, monkeypatch):
    _patch_fgtcli(monkeypatch)

    orig_init = FakeFgtCli.__init__

    def init_with_secondary(self, *a, **kw):
        orig_init(self, *a, **kw)
        self.fgt_info.exsist_secondary = True

    monkeypatch.setattr(FakeFgtCli, '__init__', init_with_secondary)

    target = FortigateTarget(addr='172.16.201.201', user='admin', password='pw',
                              get_secondary=True, tac=True)
    result = backup_one(target, backup_dir=str(tmp_path))

    assert result.ok is True
    assert result.tac_ok is True
    assert result.secondary_ok is True
    assert result.secondary_tac_ok is True
    assert result.secondary_tac_status == 'ok'
    assert result.secondary_tac_saved_path
    assert 'tacreport' in result.secondary_tac_saved_path
    # Primary/Secondaryそれぞれ独立にexecute tac reportを実行していること
    assert len(FakeFgtCli.instances[0].execute.tac_report.calls) == 2
    # 2026-09-16回帰防止: 各呼び出しに、そのセッション時点で実際に
    # ログインしているホスト名(Primary/Secondary)を渡していること
    # (scrapliのcomms_prompt_pattern誤マッチ対策として必須のパラメータ)。
    assert FakeFgtCli.instances[0].execute.tac_report.hostnames == ['LABFG01', 'LABFG02']


def test_backup_one_tac_not_requested_leaves_status_empty(tmp_path, monkeypatch):
    _patch_fgtcli(monkeypatch)
    target = FortigateTarget(addr='172.16.201.201', user='admin', password='pw')

    result = backup_one(target, backup_dir=str(tmp_path))

    assert result.tac_requested is False
    assert result.tac_status == ''
    assert FakeFgtCli.instances[0].execute.tac_report.calls == []


def test_backup_one_progress_callback_reports_tac_status_transitions(tmp_path, monkeypatch):
    _patch_fgtcli(monkeypatch)
    target = FortigateTarget(addr='172.16.201.201', user='admin', password='pw', tac=True)

    statuses = []
    backup_one(
        target, backup_dir=str(tmp_path),
        progress_callback=lambda result: statuses.append(result.tac_status),
    )

    assert statuses == ['', '', '', 'fetching', 'ok']


def test_backup_one_passes_tac_timeout_through(tmp_path, monkeypatch):
    _patch_fgtcli(monkeypatch)
    target = FortigateTarget(addr='172.16.201.201', user='admin', password='pw', tac=True)

    backup_one(target, backup_dir=str(tmp_path), tac_timeout=45.0)

    assert FakeFgtCli.instances[0].execute.tac_report.calls == [45.0]
