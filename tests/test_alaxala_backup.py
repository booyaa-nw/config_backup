"""`config_backup.alaxala`(Alaxala AX3000バックアップのオーケストレーション)のテスト。

`net_config.alaxala.ax3k.cli.AxCli`はフェイク(`FakeAxCli`)に差し替える。
"""
from __future__ import annotations

import ftplib
import io
import re
import threading
from pathlib import Path

import pytest

import config_backup.alaxala as ax_mod
from common.ftp import ReceivedFile
from config_backup.alaxala import (
    AxBackupResult,
    AxTarget,
    FtpContext,
    backup_one_ax,
    load_ax_list_csv,
    remote_filename,
    run_alaxala_backup,
    start_ftp_server,
)

TS = r'\d{8}_\d{6}'


class _Ok:
    ok = True
    value = '172.16.201.10'
    message = ''


class FakeAxInfo:
    def __init__(self):
        self.prompt_name = 'ax3660s-01'
        self.hostname = ''
        self.version = '12.1.W'


class FakeAxCli:
    """AxCliのフェイク。クラス属性で挙動を切り替える。"""
    login_result = {'code': 0, 'msg': '', 'output': ''}
    config_result = {'code': 0, 'msg': '', 'output': 'hostname "ax3660s-01"\n!\n'}
    config_hostname = 'ax3660s-01'
    backup_result = {'code': 0, 'msg': 'Backup information success!', 'output': ''}
    tac_result = {'code': 0, 'msg': '', 'output': '########## Tech-Support Log ##########\n'}
    instances: list['FakeAxCli'] = []

    def __init__(self, ax_info=None, timeout=30.0):
        self.ax_info = FakeAxInfo()
        self.timeout = timeout
        self.backup_calls = []
        self.tac_calls = []
        self.logged_out = False
        self.set_target_kwargs = {}
        outer = self

        class _Cfg:
            def get(self_inner, timeout=None):
                if outer.config_result['code'] == 0:
                    outer.ax_info.hostname = outer.config_hostname
                return dict(outer.config_result)

        class _Tac:
            def get(self_inner, timeout=None):
                outer.tac_calls.append(timeout)
                return dict(outer.tac_result)

        class _Show:
            configuration = _Cfg()
            tech_support = _Tac()

        class _Ftp:
            def run(self_inner, **kwargs):
                outer.backup_calls.append(kwargs)
                return dict(outer.backup_result)

        class _Backup:
            ftp = _Ftp()

        self.show = _Show()
        self.backup = _Backup()
        FakeAxCli.instances.append(self)

    def set_target(self, **kwargs):
        self.set_target_kwargs = kwargs

    def login(self):
        return dict(self.login_result)

    def logout(self):
        self.logged_out = True


class FakeFtpServer:
    def __init__(self, tmp_path: Path, missing: bool = False):
        self.tmp_path = tmp_path
        self.missing = missing
        self.waited = []

    def wait_for_file(self, timeout=60.0, poll_interval=0.5, filename=None):
        self.waited.append(filename)
        if self.missing:
            return None
        p = self.tmp_path / filename
        p.write_bytes(b'SYSTEMBACKUP')
        return ReceivedFile(filename=filename, path=p, size=12)

    def stop(self):
        pass


@pytest.fixture(autouse=True)
def patch_env(monkeypatch):
    for attr, val in (('login_result', {'code': 0, 'msg': '', 'output': ''}),
                      ('config_result', {'code': 0, 'msg': '', 'output': 'hostname "ax3660s-01"\n!\n'}),
                      ('config_hostname', 'ax3660s-01'),
                      ('backup_result', {'code': 0, 'msg': 'ok', 'output': ''}),
                      ('tac_result', {'code': 0, 'msg': '', 'output': 'REPORT\n'})):
        monkeypatch.setattr(FakeAxCli, attr, val)
    FakeAxCli.instances = []
    monkeypatch.setattr(ax_mod, 'AxCli', FakeAxCli)
    monkeypatch.setattr(ax_mod, 'get_src_addr', lambda addr: _Ok())


def _target(**kw):
    base = dict(addr='172.16.201.205', user='nwadmin', password='P@ssw0rd')
    base.update(kw)
    return AxTarget(**base)


def _ftp(tmp_path, missing=False):
    return FtpContext(server=FakeFtpServer(tmp_path / 'staging', missing=missing),
                      user='fu', password='fp')


@pytest.fixture
def staging(tmp_path):
    (tmp_path / 'staging').mkdir()
    return tmp_path


def test_success_saves_all_files_with_hostname_based_names(staging):
    out = staging / 'out'
    r = backup_one_ax(_target(tac=True, alias='ALIAS'), backup_dir=str(out), ftp=_ftp(staging))

    assert r.ok, r
    assert r.hostname == 'ax3660s-01'
    assert re.search(rf'ax3660s-01_12\.1\.W_{TS}\.conf$', r.config_saved_path)
    assert re.search(rf'ax3660s-01_12\.1\.W_systembackup_{TS}\.dat$', r.sys_saved_path)
    assert re.search(rf'ax3660s-01_12\.1\.W_tacreport_{TS}\.log$', r.tac_saved_path)
    # aliasはファイル名に使わない(FAZと同様)
    assert 'ALIAS' not in r.config_saved_path
    assert Path(r.sys_saved_path).read_bytes() == b'SYSTEMBACKUP'
    assert FakeAxCli.instances[0].logged_out is True


def test_no_software_uses_noos_suffix_and_command_flag(staging):
    r = backup_one_ax(_target(no_software=True), backup_dir=str(staging / 'out'), ftp=_ftp(staging))
    assert r.ok
    assert re.search(rf'ax3660s-01_12\.1\.W_systembackup_noos_{TS}\.dat$', r.sys_saved_path)
    call = FakeAxCli.instances[0].backup_calls[0]
    assert call['no_software'] is True
    assert call['filename'].endswith('_systembackup_noos.dat')


def test_backup_ftp_receives_ftp_credentials_and_local_addr(staging):
    ftp = _ftp(staging)
    backup_one_ax(_target(), backup_dir=str(staging / 'out'), ftp=ftp)
    call = FakeAxCli.instances[0].backup_calls[0]
    assert call['ftp_addr'] == '172.16.201.10'
    assert call['ftp_user'] == 'fu' and call['ftp_password'] == 'fp'
    # 共有FTPサーバ上で自分宛てのファイル名だけを待つ
    assert ftp.server.waited == [call['filename']]
    assert call['filename'] == '172.16.201.205_ax3660s-01_systembackup.dat'


def test_tac_not_requested(staging):
    r = backup_one_ax(_target(), backup_dir=str(staging / 'out'), ftp=_ftp(staging))
    assert r.ok
    assert r.tac_status == ''
    assert FakeAxCli.instances[0].tac_calls == []


def test_login_failure(staging):
    FakeAxCli.login_result = {'code': 1, 'msg': '[Error] enable failed: wrong enable password (Sorry)', 'output': ''}
    r = backup_one_ax(_target(), backup_dir=str(staging / 'out'), ftp=_ftp(staging))
    assert r.status == 'login_ng'
    assert 'Sorry' in r.message
    assert not r.ok
    assert r.config_status == '' and r.sys_status == ''


def test_system_backup_failure_does_not_stop_tac(staging):
    FakeAxCli.backup_result = {'code': 1, 'msg': '[Error] backup ftp: Login failed. / ftp transfer failed.', 'output': ''}
    r = backup_one_ax(_target(tac=True), backup_dir=str(staging / 'out'), ftp=_ftp(staging))
    assert r.config_status == 'ok'
    assert r.sys_status == 'ng'
    assert 'Login failed' in r.sys_message
    assert r.tac_status == 'ok'
    assert not r.ok


def test_system_backup_success_but_file_not_received(staging):
    r = backup_one_ax(_target(), backup_dir=str(staging / 'out'), ftp=_ftp(staging, missing=True))
    assert r.sys_status == 'ng'
    assert 'no file was received' in r.sys_message


def test_ftp_server_unavailable_marks_only_system_ng(staging):
    ftp = FtpContext(server=None, error='[Error] Failed to start FTP server on port 21: denied')
    r = backup_one_ax(_target(tac=True), backup_dir=str(staging / 'out'), ftp=ftp)
    assert r.config_status == 'ok'
    assert r.sys_status == 'ng' and 'port 21' in r.sys_message
    assert r.tac_status == 'ok'
    assert FakeAxCli.instances[0].backup_calls == []


def test_tac_failure(staging):
    FakeAxCli.tac_result = {'code': 1, 'msg': '[Error] show tech-support: password invalid (3 times)', 'output': ''}
    r = backup_one_ax(_target(tac=True), backup_dir=str(staging / 'out'), ftp=_ftp(staging))
    assert r.tac_status == 'ng'
    assert 'password invalid' in r.tac_message
    assert r.config_status == 'ok' and r.sys_status == 'ok'


def test_hostname_falls_back_to_prompt_name(staging):
    FakeAxCli.config_hostname = ''
    r = backup_one_ax(_target(), backup_dir=str(staging / 'out'), ftp=_ftp(staging))
    assert r.hostname == 'ax3660s-01'
    assert Path(r.config_saved_path).name.startswith('ax3660s-01_12.1.W_')


def test_default_and_target_backup_dir(staging, monkeypatch):
    monkeypatch.chdir(staging)
    r = backup_one_ax(_target(), backup_dir=None, ftp=_ftp(staging))
    assert r.backup_dir == './booyaa_log/config'
    r2 = backup_one_ax(_target(backup_dir=str(staging / 'csvdir')), backup_dir='ignored', ftp=_ftp(staging))
    assert r2.backup_dir == str(staging / 'csvdir')


def test_progress_callback_transitions(staging):
    seen = []
    backup_one_ax(_target(tac=True), backup_dir=str(staging / 'out'), ftp=_ftp(staging),
                  progress_callback=lambda r: seen.append((r.status, r.config_status, r.sys_status, r.tac_status)))
    assert seen[0] == ('connecting', '', '', '')
    assert ('login_ok', 'backing_up', '', '') in seen
    assert ('login_ok', 'ok', 'backing_up', '') in seen
    assert ('login_ok', 'ok', 'ok', 'fetching') in seen
    assert seen[-1] == ('done', 'ok', 'ok', 'ok')


def test_remote_filename():
    assert remote_filename(_target(), 'sw1') == '172.16.201.205_sw1_systembackup.dat'
    assert remote_filename(_target(no_software=True), '') == '172.16.201.205_unknown_systembackup_noos.dat'


# ----------------------------------------------------------------------
# 複数台・同時実行
# ----------------------------------------------------------------------
def test_run_respects_concurrency_and_stagger(monkeypatch, tmp_path):
    lock = threading.Lock()
    state = {'cur': 0, 'max': 0}
    barrier_release = threading.Event()
    sleeps = []

    def fake_backup_one(target, backup_dir, ftp, timeout, sys_backup_timeout, tac_timeout,
                        progress_callback, result):
        with lock:
            state['cur'] += 1
            state['max'] = max(state['max'], state['cur'])
        barrier_release.wait(0.2)
        with lock:
            state['cur'] -= 1
        result.status = 'done'
        return result

    monkeypatch.setattr(ax_mod, 'backup_one_ax', fake_backup_one)
    monkeypatch.setattr(ax_mod.time, 'sleep', lambda s: sleeps.append(s))
    monkeypatch.setattr(ax_mod, 'start_ftp_server',
                        lambda d, ftp_user='', ftp_password='', port=21: FtpContext(server=None, error='x'))

    targets = [_target(addr=f'10.0.0.{i}') for i in range(7)]
    results = run_alaxala_backup(targets, backup_dir=str(tmp_path), concurrency=5, stagger_delay=1.0)

    assert [r.target.addr for r in results] == [t.addr for t in targets]
    assert state['max'] <= 5
    assert sleeps == [1.0] * 6


def test_run_isolates_unexpected_exception(monkeypatch, tmp_path):
    def fake_backup_one(target, **kwargs):
        if target.addr == '10.0.0.1':
            raise RuntimeError('boom')
        kwargs['result'].status = 'done'
        return kwargs['result']

    monkeypatch.setattr(ax_mod, 'backup_one_ax', fake_backup_one)
    monkeypatch.setattr(ax_mod, 'start_ftp_server',
                        lambda d, ftp_user='', ftp_password='', port=21: FtpContext(server=None, error='x'))

    results = run_alaxala_backup([_target(addr='10.0.0.1'), _target(addr='10.0.0.2')],
                                 backup_dir=str(tmp_path), stagger_delay=0)
    assert results[0].status == 'login_ng'
    assert 'RuntimeError: boom' in results[0].message
    assert results[1].status == 'done'


def test_run_starts_single_shared_ftp_server(monkeypatch, tmp_path):
    started = []
    orig = ax_mod.start_ftp_server

    def spy(directory, ftp_user='', ftp_password='', port=21):
        ctx = orig(directory, ftp_user=ftp_user, ftp_password=ftp_password, port=0)
        started.append(ctx)
        return ctx

    monkeypatch.setattr(ax_mod, 'start_ftp_server', spy)
    monkeypatch.setattr(ax_mod, '_FTP_RECEIVE_WAIT_TIMEOUT', 0.3)
    results = run_alaxala_backup([_target(addr='10.0.0.1'), _target(addr='10.0.0.2')],
                                 backup_dir=str(tmp_path / 'out'), stagger_delay=0,
                                 ftp_user='u', ftp_password='p')
    assert len(started) == 1
    assert started[0].user == 'u' and started[0].password == 'p'
    # FakeAxCliはFTPへ実際にはアップロードしないため、ファイル未着でNGになる
    # (共有サーバで自分宛てのファイル名を待っていることの確認)
    assert all('no file was received' in r.sys_message for r in results)


def test_start_ftp_server_real_upload_by_filename(tmp_path):
    ctx = start_ftp_server(str(tmp_path), ftp_user='u', ftp_password='p', port=0)
    try:
        assert ctx.server is not None
        ftp = ftplib.FTP()
        ftp.connect('127.0.0.1', ctx.server.port, timeout=10)
        ftp.login('u', 'p')
        ftp.storbinary('STOR a_systembackup.dat', io.BytesIO(b'A'))
        ftp.storbinary('STOR b_systembackup.dat', io.BytesIO(b'B'))
        ftp.quit()
        got = ctx.server.wait_for_file(timeout=10, filename='b_systembackup.dat')
        assert got is not None and got.path.read_bytes() == b'B'
    finally:
        ctx.server.stop()


def test_start_ftp_server_bind_failure_is_reported(tmp_path, monkeypatch):
    def boom(**kwargs):
        raise PermissionError(13, 'Permission denied')

    monkeypatch.setattr(ax_mod, 'SimpleFtpServer', boom)
    ctx = start_ftp_server(str(tmp_path))
    assert ctx.server is None
    assert 'port 21' in ctx.error


def test_start_ftp_server_generates_random_credentials(tmp_path):
    ctx = start_ftp_server(str(tmp_path), port=0)
    try:
        assert ctx.user and ctx.password and ctx.user != ctx.password
    finally:
        ctx.server.stop()


# ----------------------------------------------------------------------
# CSV
# ----------------------------------------------------------------------
def test_load_csv(tmp_path):
    p = tmp_path / 't.csv'
    p.write_text(
        'addr,user,password,enable_password,alias,backup_dir,ssh_port,tac,no_software\n'
        '172.16.201.205,nwadmin,P@ssw0rd,EnPass,core1,,,yes,yes\n'
        '# comment line\n'
        '172.16.201.206,nwadmin,P@ssw0rd\n'
        '\n',
        encoding='utf-8',
    )
    targets = load_ax_list_csv(p)
    assert len(targets) == 2
    t0, t1 = targets
    assert (t0.enable_password, t0.alias, t0.tac, t0.no_software, t0.ssh_port) == ('EnPass', 'core1', True, True, 22)
    assert (t1.enable_password, t1.tac, t1.no_software) == ('', False, False)


def test_load_csv_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_ax_list_csv(tmp_path / 'none.csv')


def test_result_ok_requires_tac_only_when_requested():
    r = AxBackupResult(target=_target(), status='done', config_status='ok', sys_status='ok')
    assert r.ok
    r.tac_requested = True
    assert not r.ok
    r.tac_status = 'ok'
    assert r.ok
