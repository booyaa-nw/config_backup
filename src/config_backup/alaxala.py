"""Alaxala AX3000シリーズ(AX3660S等)のバックアップオーケストレーション
(コンフィグ+システム+TAC report)。2026-09-23追加(こうぢさん依頼)。

`net_config.alaxala.ax3k.cli.AxCli`(機器操作)を呼び出し、結果をファイルに保存する
薄い層。`config_backup.faz`と同じ構成で、1台につき以下を取得する。

1. コンフィグバックアップ: `show configuration`
2. システムバックアップ: `backup ftp <FTPサーバ> <ファイル名> [no-software]`
   (`no_software=True`/`--no-software`指定時はOSを除外)
3. TAC report(`tac=True`/`--tac`指定時のみ): `show tech-support`

## ファイル名(こうぢさん指定: FAZと同様)

FAZと同様、alias指定の有無に関わらず**ホスト名基準**とし、
`<hostname>_<version>[_<suffix>]_<YYYYmmdd_HHMMSS>.<ext>`とする。

- コンフィグ: `<hostname>_<version>_<ts>.conf`
- システム: `<hostname>_<version>_systembackup_<ts>.dat`
  (OS除外時は`_systembackup_noos`、こうぢさん指定)
- TAC report: `<hostname>_<version>_tacreport_<ts>.log`

ホスト名はコンフィグ上の`hostname "..."`、バージョンは`show version`の
`S/W: OS-L3M Ver. 12.1.W`の`12.1.W`部分。コンフィグからホスト名が取れなかった
場合はプロンプト上の装置名を使う。

## システムバックアップ用FTPサーバ(21番固定・全台共有)

Alaxalaの`backup ftp`コマンドは**ポート番号を指定できない**(こうぢさん確認)ため、
受信用の一時FTPサーバ(`common.ftp.SimpleFtpServer`)は21番でlistenする必要がある。
FAZのように対象ごとにサーバを立てると、複数台同時実行時に21番を取り合ってしまうため、
**1回の実行(`run_alaxala_backup()`)につき1つのサーバを起動し、全台で共有する**。
各装置には`<addr>_<hostname>_systembackup[_noos].dat`という装置ごとに一意な
ファイル名でアップロードさせ、`SimpleFtpServer.wait_for_file(filename=...)`で
自分宛てのファイルだけを待ち受ける。

21番がbindできない場合(既に使用中、Linuxでroot権限が無い等)は、システム
バックアップのみ全台失敗として記録し、コンフィグ・TAC reportは続行する。

FTPログイン情報は、FAZと同様、明示指定が無ければ実行ごとにランダム生成する。

## 同時実行(こうぢさん指定)

複数台実行時は最大5台を同時実行し、1台ごとに1秒のdelayを入れる
(`DEFAULT_CONCURRENCY`/`DEFAULT_STAGGER_DELAY`)。装置ごとに独立したSSH
セッションを使うため、`config_backup.msw`のようなセッション共有の制約は無い。
"""
from __future__ import annotations

import csv
import secrets
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from common.ftp import SimpleFtpServer
from common.iptools.host.routing import get_src_addr

from net_config.alaxala.ax3k.cli import AxCli
from net_config.alaxala.ax3k.model.ax_info import AxInfo

from config_backup.save import save_binary_file, save_config

ProgressCallback = Callable[['AxBackupResult'], None]

CSV_HEADERS = [
    'addr', 'user', 'password', 'enable_password', 'alias',
    'backup_dir', 'ssh_port', 'tac', 'no_software',
]
CSV_HEADER_ALIASES = {'addr', 'ax_addr'}

DEFAULT_BACKUP_DIR = './booyaa_log/config'

# `backup ftp`はポート番号を指定できないため、受信用FTPサーバは21番固定。
FTP_PORT = 21

DEFAULT_CONCURRENCY = 5
DEFAULT_STAGGER_DELAY = 1.0

# `backup ftp`が"Backup information success!"を返した後、FTPサーバ側で
# アップロード完了(on_file_received)が記録されるまでの待ち時間。
_FTP_RECEIVE_WAIT_TIMEOUT = 30.0


def _random_ftp_credential(nbytes: int = 12) -> str:
    return secrets.token_urlsafe(nbytes)


def _notify(callback: Optional[ProgressCallback], result: 'AxBackupResult') -> None:
    if callback is not None:
        callback(result)


def _make_locked_callback(callback: Optional[ProgressCallback],
                          lock: threading.Lock) -> Optional[ProgressCallback]:
    """複数スレッドからの進捗通知を直列化する(`config_backup.msw`と同じ考え方)。"""
    if callback is None:
        return None

    def _wrapped(result: 'AxBackupResult') -> None:
        with lock:
            callback(result)

    return _wrapped


def _truthy(value: str) -> bool:
    return value.strip().lower() in ('yes', 'true', '1')


@dataclass
class AxTarget:
    addr: str
    user: str
    password: str
    # 空の場合はログインパスワードを使う(enableパスワード未設定の装置では
    # そもそも入力を求められないため無害)。
    enable_password: str = ''
    alias: str = ''
    ssh_port: int = 22
    backup_dir: str = ''
    tac: bool = False
    no_software: bool = False


@dataclass
class AxBackupResult:
    """1台分の処理結果。各statusの値は`config_backup.faz.FazBackupResult`と同じ。

    status: ''/'waiting'/'connecting'/'login_ng'/'login_ok'/'done'
    config_status / sys_status: ''/'backing_up'/'ok'/'ng'
    tac_status: ''/'fetching'/'ok'/'ng'
    """
    target: AxTarget
    hostname: str = ''
    version: str = ''
    backup_dir: str = ''
    status: str = ''
    message: str = ''
    config_status: str = ''
    config_saved_path: str = ''
    sys_status: str = ''
    sys_saved_path: str = ''
    sys_message: str = ''
    tac_requested: bool = False
    tac_status: str = ''
    tac_saved_path: str = ''
    tac_message: str = ''

    @property
    def ok(self) -> bool:
        if self.status != 'done':
            return False
        if self.config_status != 'ok' or self.sys_status != 'ok':
            return False
        if self.tac_requested and self.tac_status != 'ok':
            return False
        return True


@dataclass
class FtpContext:
    """全台で共有するシステムバックアップ用FTPサーバの情報。

    `server`が`None`の場合は起動に失敗しており、`error`にその理由が入る。
    """
    server: Optional[SimpleFtpServer]
    user: str = ''
    password: str = ''
    error: str = ''


def load_ax_list_csv(csv_path: str | Path) -> list[AxTarget]:
    """Alaxalaバックアップ対象一覧のCSVを読み込む。

    列: addr,user,password,enable_password,alias,backup_dir,ssh_port,tac,no_software
    (他機種のローダ同様、列数が多い行は末尾を切り捨て、少ない行は空文字で補う)。
    """
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        raise FileNotFoundError(f'target csv not found: {csv_path.resolve()}')

    with open(csv_path, newline='', encoding='utf-8') as f:
        rows = list(csv.reader(f))

    targets: list[AxTarget] = []
    max_cols = len(CSV_HEADERS)
    for i, row in enumerate(rows):
        if i == 0 and row and row[0].strip() in CSV_HEADER_ALIASES:
            continue
        if not row or not row[0].strip() or row[0].strip().startswith('#'):
            continue
        if len(row) < max_cols:
            row = row + [''] * (max_cols - len(row))
        elif len(row) > max_cols:
            row = row[:max_cols]
        d = dict(zip(CSV_HEADERS, row))
        targets.append(AxTarget(
            addr=d['addr'].strip(),
            user=d['user'].strip(),
            password=d['password'],
            enable_password=d['enable_password'],
            alias=d['alias'].strip(),
            backup_dir=d['backup_dir'].strip(),
            ssh_port=int(d['ssh_port']) if d['ssh_port'].strip() else 22,
            tac=_truthy(d['tac']),
            no_software=_truthy(d['no_software']),
        ))
    return targets


def start_ftp_server(directory: str, ftp_user: str = '', ftp_password: str = '',
                     port: int = FTP_PORT) -> FtpContext:
    """全台共有の受信用FTPサーバを起動する(失敗しても例外を投げずFtpContextで返す)。"""
    user = ftp_user or _random_ftp_credential()
    password = ftp_password or _random_ftp_credential()
    try:
        server = SimpleFtpServer(directory=directory, user=user, password=password, port=port)
        server.start()
    except OSError as e:
        return FtpContext(server=None, error=f'[Error] Failed to start FTP server on port {port}: {e}')
    return FtpContext(server=server, user=user, password=password)


def remote_filename(target: AxTarget, hostname: str) -> str:
    """装置にアップロードさせる一時ファイル名(共有FTPサーバ上で装置ごとに一意)。"""
    safe_addr = target.addr.replace(':', '_').replace('/', '_')
    suffix = '_noos' if target.no_software else ''
    return f"{safe_addr}_{hostname or 'unknown'}_systembackup{suffix}.dat"


def backup_one_ax(target: AxTarget, backup_dir: Optional[str], ftp: Optional[FtpContext],
                  timeout: float = 60.0, sys_backup_timeout: Optional[float] = None,
                  tac_timeout: Optional[float] = None,
                  progress_callback: Optional[ProgressCallback] = None,
                  result: Optional[AxBackupResult] = None) -> AxBackupResult:
    """1台のAlaxala装置について、コンフィグ+システム(+TAC report)を取得する。"""
    if result is None:
        result = AxBackupResult(target=target, tac_requested=target.tac)
    result.status = 'connecting'
    _notify(progress_callback, result)

    cli = AxCli(AxInfo(alias=target.alias), timeout=timeout)
    cli.set_target(addr=target.addr, user=target.user, password=target.password,
                   enable_password=target.enable_password, alias=target.alias,
                   ssh_port=target.ssh_port)

    let = cli.login()
    if let['code'] != 0:
        # enable失敗等、SSHログイン自体は成功していれば装置名は判明している
        result.hostname = cli.ax_info.prompt_name
        result.message = let['msg']
        result.status = 'login_ng'
        _notify(progress_callback, result)
        return result

    info = cli.ax_info
    result.hostname = info.prompt_name
    result.version = info.version
    result.status = 'login_ok'
    target_dir = target.backup_dir or backup_dir or DEFAULT_BACKUP_DIR
    result.backup_dir = target_dir
    _notify(progress_callback, result)

    try:
        # --- 1. コンフィグバックアップ ---
        result.config_status = 'backing_up'
        _notify(progress_callback, result)
        let = cli.show.configuration.get()
        if info.hostname:
            result.hostname = info.hostname
        hostname = info.hostname or info.prompt_name
        if let['code'] != 0:
            result.config_status = 'ng'
            result.message = let['msg']
        else:
            save_let = save_config(content=let['output'], hostname=hostname,
                                   version=info.version, export_dir=target_dir)
            if save_let['code'] != 0:
                result.config_status = 'ng'
                result.message = save_let['msg']
            else:
                result.config_status = 'ok'
                result.config_saved_path = save_let['output']
        _notify(progress_callback, result)

        # --- 2. システムバックアップ ---
        result.sys_status = 'backing_up'
        _notify(progress_callback, result)
        sys_ok, sys_saved, sys_msg = _backup_system(
            cli, target=target, hostname=hostname, version=info.version,
            export_dir=target_dir, ftp=ftp, timeout=sys_backup_timeout,
        )
        result.sys_status = 'ok' if sys_ok else 'ng'
        result.sys_saved_path = sys_saved
        result.sys_message = sys_msg
        _notify(progress_callback, result)

        # --- 3. TAC report ---
        if target.tac:
            result.tac_status = 'fetching'
            _notify(progress_callback, result)
            tac_let = cli.show.tech_support.get(timeout=tac_timeout)
            if tac_let['code'] != 0:
                result.tac_status = 'ng'
                result.tac_message = tac_let['msg']
            else:
                tac_save = save_config(content=tac_let['output'], hostname=hostname,
                                       version=info.version, export_dir=target_dir,
                                       suffix='tacreport', ext='log')
                if tac_save['code'] != 0:
                    result.tac_status = 'ng'
                    result.tac_message = tac_save['msg']
                else:
                    result.tac_status = 'ok'
                    result.tac_saved_path = tac_save['output']
            _notify(progress_callback, result)
    finally:
        cli.logout()

    result.status = 'done'
    _notify(progress_callback, result)
    return result


def _backup_system(cli: AxCli, target: AxTarget, hostname: str, version: str, export_dir: str,
                   ftp: Optional[FtpContext], timeout: Optional[float]) -> tuple[bool, str, str]:
    """`backup ftp`によるシステムバックアップ。戻り値は`(ok, saved_path, message)`。"""
    if ftp is None or ftp.server is None:
        return False, '', (ftp.error if ftp is not None else '[Error] FTP server is not available')

    src = get_src_addr(target.addr)
    if not src.ok:
        return False, '', (f'[Error] Failed to determine local address reachable from '
                           f'{target.addr}: {src.message}')

    filename = remote_filename(target, hostname)
    let = cli.backup.ftp.run(ftp_addr=src.value, filename=filename, ftp_user=ftp.user,
                             ftp_password=ftp.password, no_software=target.no_software,
                             timeout=timeout)
    if let['code'] != 0:
        return False, '', let['msg']

    received = ftp.server.wait_for_file(timeout=_FTP_RECEIVE_WAIT_TIMEOUT, filename=filename)
    if received is None:
        return False, '', ('[Error] backup ftp reported success, '
                           'but no file was received via FTP')

    suffix = 'systembackup_noos' if target.no_software else 'systembackup'
    save_let = save_binary_file(src_path=received.path, hostname=hostname, version=version,
                                export_dir=export_dir, suffix=suffix)
    if save_let['code'] != 0:
        return False, '', save_let['msg']
    return True, save_let['output'], ''


def run_alaxala_backup(targets: list[AxTarget], backup_dir: Optional[str], timeout: float = 60.0,
                       sys_backup_timeout: Optional[float] = None,
                       tac_timeout: Optional[float] = None,
                       ftp_user: str = '', ftp_password: str = '',
                       progress_callback: Optional[ProgressCallback] = None,
                       concurrency: int = DEFAULT_CONCURRENCY,
                       stagger_delay: float = DEFAULT_STAGGER_DELAY,
                       ftp_port: int = FTP_PORT) -> list[AxBackupResult]:
    """対象一覧をバックアップする(最大`concurrency`台同時、起動間隔`stagger_delay`秒)。

    受信用FTPサーバは本関数の中で1つだけ起動し、全台で共有する(モジュールdocstring参照)。
    `ftp_port`はテスト用(実機の`backup ftp`はポート指定不可のため21番以外では動かない)。
    """
    notify = _make_locked_callback(progress_callback, threading.Lock())
    results = [AxBackupResult(target=t, tac_requested=t.tac, status='waiting') for t in targets]
    for r in results:
        _notify(notify, r)

    with tempfile.TemporaryDirectory(prefix='booyaa_ax_systembackup_') as staging_dir:
        ftp = start_ftp_server(staging_dir, ftp_user=ftp_user, ftp_password=ftp_password,
                               port=ftp_port)
        try:
            with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
                futures = []
                for i, (target, result) in enumerate(zip(targets, results)):
                    if i > 0 and stagger_delay > 0:
                        time.sleep(stagger_delay)
                    futures.append(executor.submit(
                        _worker, target, backup_dir, ftp, timeout, sys_backup_timeout,
                        tac_timeout, notify, result,
                    ))
                for f in futures:
                    f.result()
        finally:
            if ftp.server is not None:
                ftp.server.stop()

    return results


def _worker(target: AxTarget, backup_dir: Optional[str], ftp: FtpContext, timeout: float,
            sys_backup_timeout: Optional[float], tac_timeout: Optional[float],
            notify: Optional[ProgressCallback], result: AxBackupResult) -> None:
    """1台分の処理。想定外の例外が他の装置の処理を巻き込まないよう、ここで捕捉する。"""
    try:
        backup_one_ax(target, backup_dir=backup_dir, ftp=ftp, timeout=timeout,
                      sys_backup_timeout=sys_backup_timeout, tac_timeout=tac_timeout,
                      progress_callback=notify, result=result)
    except Exception as e:  # noqa: BLE001
        result.message = f'[Error] unexpected: {type(e).__name__}: {e}'
        if result.status in ('', 'waiting', 'connecting'):
            result.status = 'login_ng'
        else:
            result.status = 'done'
        _notify(notify, result)
