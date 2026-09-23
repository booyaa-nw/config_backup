"""FortiAnalyzer(FAZ)のバックアップオーケストレーション(コンフィグ+システム全体+TAC report)。

`net_config.ftnt.faz.cli.FazCli`(機器操作)を呼び出し、結果をファイルに保存する薄い層。
`config_backup.fortigate`(FGT本体用)・`config_backup.msw`(Managed Switch用)と対になる
モジュールだが、FAZは以下の点でそれらと異なる(こうぢさん指定、2026-09-22実装):

1. **2種類のバックアップを両方取得する**: コンフィグバックアップ(`show`)と、
   システム全体バックアップ(`execute backup all-settings ftp ...`)の両方を実行する。
   前者は他機種同様のテキスト取得(`FazCli.show.get()`)だが、後者はFAZ自身が
   FTPクライアントとして動作し、こちら側が用意したFTPサーバへファイルを
   "push"してくる方式のため、機器へのコマンド送信だけでは完結しない
   (`common.ftp.SimpleFtpServer`を一時的に起動して受信する。後述)。
2. **ファイル名はホスト名基準**: FGT/MSWは`<alias または hostname>`だが、
   FAZは「ファイル名は、ホスト名を基にしてください」とのこうぢさん指定により、
   `alias`を渡さず常に`hostname`基準のファイル名にする
   (`config_backup.save.save_config()`/`save_binary_file()`に`alias=''`で呼ぶ)。
   `version`(FortiAnalyzerOSバージョン)もFGT/MSWと同様に含め、コンフィグ・
   システムバックアップ双方とも`<hostname>_<version>_<timestamp>.<ext>`という
   ファイル名にする(2026-09-23、こうぢさん指摘により統一)。TAC reportにも
   同日、同じくこうぢさんのご要望によりversionを含めるよう統一した(FGT/MSWも
   同様。`config_backup.fortigate._fetch_tac_report()`のdocstring参照)。
3. **HA/VDOM/MSWに相当する複雑さが無い**: `net_config.ftnt.faz.cli.FazCli`が
   `FgtCli`を継承せず独立実装なのと同じ理由で、本モジュールも`FortigateTarget`/
   `MswTarget`を継承・流用せず、独立した`FazTarget`/`FazBackupResult`を定義する。

## システムバックアップの実装方式(FTPサーバの一時起動)

`execute backup all-settings ftp <ftp_server_address> <filename> <ftp_user> <ftp_password>
<config_password>`は、FAZ自身が指定されたFTPサーバへファイルをアップロードしてくる
という、これまでのバックアップコマンド(コンフィグ取得等、機器→こちらへの応答を
そのまま受け取るだけの形式)には無かった方式である。そのため、本ツール実行時に
一時的な受信専用FTPサーバを立てる必要がある(「systemバックアップは、ftpサーバが
必要です。ですので、本ツール実行時にpythonで簡易ftpサーバを実装してほしいです」
というこうぢさんの依頼)。

FTPサーバの実装自体は`config_backup`固有ではなく`common.ftp.SimpleFtpServer`に
ある。「同様に簡易FTPサーバを使う場合があるので、commonでの実装としてください」
というこうぢさんの指示による(`architecture/plugin-architecture-consideration.md`が
示す、ベンダー非依存の汎用ユーティリティは`common`に置くという方針とも合致する)。
本モジュール(`_backup_system_settings()`)は、`SimpleFtpServer`を対象ごとに
一時ディレクトリ向けに起動・停止するだけの薄い呼び出し元になる。

FTPサーバのアドレス(FAZへ伝える、こちら側のアドレス)は、`common.iptools.host.routing.
get_src_addr(target.addr)`でFAZへの経路上こちらが使うことになるアドレスを動的に
決定する(マルチホームな実行環境でも、FAZから実際に到達可能なアドレスを渡すため。
決め打ちのインタフェース指定はしない)。ポート番号は21番以外を使う場合
`<アドレス>:<ポート番号>`という形式でコマンドに渡す(「FAZのバックアップコマンドでの
FTPポート番号指定は`FTPサーバIPアドレス:ポート番号`となります」というこうぢさんの
指定、`net_config.ftnt.faz.cli.execute.backup_all_settings.BackupAllSettings.run()`
側で実装済み)。

## FTPログイン情報について(このツールの設計判断、要確認)

`FazTarget.ftp_user`/`ftp_password`を指定しない場合、対象ごとに`secrets.token_urlsafe()`で
ランダムな使い捨ての認証情報を生成する(固定の既定値を使わないことで、同一セグメント上の
第三者がバックアップ内容を盗聴・改ざんする隙を減らすための判断)。これは今回の依頼に
明示的な指定が無かったための実装側の判断のため、運用上固定の認証情報を使いたい場合は
`FazTarget.ftp_user`/`ftp_password`(CLIの`--ftp-user`/`--ftp-password`)で明示指定できる。

## バックアップ先ディレクトリの既定値

FAZはFGT本体と同様、MSWのように特定の親機器にぶら下がる構造を持たないため、
既定値はFGT本体(`config_backup fgt`)と同じ`./booyaa_log/config`とした
(`DEFAULT_BACKUP_DIR`。MSWのようにサブディレクトリを切る必要が無いという判断。
今回の依頼に明示的な指定は無かったため、これも実装側の判断)。
"""
from __future__ import annotations

import csv
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from common.ftp import DEFAULT_PORT as FTP_DEFAULT_PORT
from common.ftp import SimpleFtpServer
from common.iptools.host.routing import get_src_addr

from net_config.ftnt.faz.cli import FazCli
from net_config.ftnt.faz.model.faz_info import FazInfo

from config_backup.save import save_binary_file, save_config

ProgressCallback = Callable[['FazBackupResult'], None]


def _notify(callback: Optional[ProgressCallback], result: 'FazBackupResult') -> None:
    if callback is not None:
        callback(result)


CSV_HEADERS = [
    'addr', 'user', 'password', 'alias', 'config_password',
    'ftp_user', 'ftp_password', 'ftp_port', 'backup_dir', 'ssh_port', 'tac',
]
CSV_HEADER_ALIASES = {'faz_addr', 'addr'}

# FGT本体(`config_backup fgt`)と同じ既定値。MSWと異なりFAZはFGTにぶら下がる
# 構造を持たないため、専用のサブディレクトリは切らない(モジュールdocstring参照)。
DEFAULT_BACKUP_DIR = './booyaa_log/config'

# システムバックアップの完了(`execute backup all-settings`の"Ok."/"Failed."判定)後、
# FTPサーバへの実際のアップロード完了を確認するための待ち時間。コマンド自体が
# "Backup all settings...Ok."を返した時点で転送は完了しているはずのため
# (実機サンプルでも転送進捗表示の直後に完了メッセージが出る)、ファイル書き込みの
# 遅延を吸収する程度の短い時間で十分という判断。
_FTP_RECEIVE_WAIT_TIMEOUT = 30.0


def _random_ftp_credential(nbytes: int = 16) -> str:
    return secrets.token_urlsafe(nbytes)


@dataclass
class FazTarget:
    addr: str
    user: str
    password: str
    alias: str = ''
    ssh_port: int = 22
    # バックアップファイルの暗号化パスワード(FAZログインパスワードとは別引数)。
    # 未指定時はログインパスワードと同じ値を使う(`backup_one_faz()`参照)。
    config_password: str = ''
    # 一時FTPサーバの認証情報。未指定時は対象ごとにランダム生成する
    # (モジュールdocstring「FTPログイン情報について」参照)。
    ftp_user: str = ''
    ftp_password: str = ''
    ftp_port: int = FTP_DEFAULT_PORT
    backup_dir: str = ''
    tac: bool = False


@dataclass
class FazBackupResult:
    """1台のFortiAnalyzer分の処理結果。

    status(ログイン、表示用)の取り得る値:
        ''            未着手
        'connecting'  接続中
        'login_ng'    ログイン失敗(終了状態)
        'login_ok'    ログイン成功、各バックアップ取得中
        'done'        全処理完了(終了状態、成否は各status参照)

    config_status/sys_status(コンフィグ/システムバックアップ、表示用)の
    取り得る値:
        ''            未着手(ログイン未完了、または対象外)
        'backing_up'  取得中
        'ok'          取得・保存成功(終了状態)
        'ng'          取得・保存失敗(終了状態)

    tac_status(`tac=True`指定時のみ)の取り得る値は他機種と同じ
    (''/'fetching'/'ok'/'ng')。
    """
    target: FazTarget
    hostname: str = ''
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
        if self.config_status != 'ok':
            return False
        if self.sys_status != 'ok':
            return False
        if self.tac_requested and self.tac_status != 'ok':
            return False
        return True


def load_faz_list_csv(csv_path: str | Path) -> list[FazTarget]:
    """FAZバックアップ対象一覧のCSVを読み込む。

    列: addr,user,password,alias,config_password,ftp_user,ftp_password,ftp_port,
        backup_dir,ssh_port,tac
    (他機種のローダ同様、列数が多い行は末尾を切り捨て、少ない行は空文字で補う)。
    """
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        raise FileNotFoundError(f'target csv not found: {csv_path.resolve()}')

    targets: list[FazTarget] = []
    max_cols = len(CSV_HEADERS)

    with open(csv_path, newline='', encoding='utf-8') as f:
        rows = list(csv.reader(f))

    for i, row in enumerate(rows):
        if i == 0 and row and row[0].strip() in CSV_HEADER_ALIASES:
            continue
        if not row or not row[0].strip():
            continue

        if len(row) < max_cols:
            row = row + [''] * (max_cols - len(row))
        elif len(row) > max_cols:
            row = row[:max_cols]

        row_dict = dict(zip(CSV_HEADERS, row))

        targets.append(FazTarget(
            addr=row_dict['addr'].strip(),
            user=row_dict['user'].strip(),
            password=row_dict['password'],
            alias=row_dict['alias'].strip(),
            config_password=row_dict['config_password'],
            ftp_user=row_dict['ftp_user'].strip(),
            ftp_password=row_dict['ftp_password'],
            ftp_port=int(row_dict['ftp_port']) if row_dict['ftp_port'].strip() else FTP_DEFAULT_PORT,
            backup_dir=row_dict['backup_dir'].strip(),
            ssh_port=int(row_dict['ssh_port']) if row_dict['ssh_port'].strip() else 22,
            tac=row_dict['tac'].strip().lower() in ('yes', 'true', '1'),
        ))

    return targets


def backup_one_faz(target: FazTarget, backup_dir: Optional[str], timeout: float = 60.0,
                    sys_backup_timeout: Optional[float] = None, tac_timeout: Optional[float] = None,
                    progress_callback: Optional[ProgressCallback] = None) -> FazBackupResult:
    """1台のFortiAnalyzerについて、コンフィグ+システム全体(+TAC report)を取得する。

    Args:
        sys_backup_timeout: `execute backup all-settings`(非同期の転送完了まで含む)の
            タイムアウト(秒)。未指定時はnet_config側の既定値(1800秒)に委ねる。
        tac_timeout: `target.tac`指定時、`execute tac report`取得のタイムアウト(秒)。
            未指定時はnet_config側の既定値(600秒)に委ねる。
    """
    result = FazBackupResult(target=target, tac_requested=target.tac, status='connecting')
    _notify(progress_callback, result)

    faz_info = FazInfo(alias=target.alias)
    cli = FazCli(faz_info, timeout=timeout)
    cli.set_target(
        faz_addr=target.addr,
        faz_user=target.user,
        faz_password=target.password,
        faz_alias=target.alias,
        faz_ssh_port=target.ssh_port,
    )

    let = cli.login()
    if let['code'] != 0:
        result.message = let['msg']
        result.status = 'login_ng'
        _notify(progress_callback, result)
        return result

    result.hostname = cli.faz_info.hostname
    result.status = 'login_ok'
    _notify(progress_callback, result)

    target_dir = target.backup_dir or backup_dir or DEFAULT_BACKUP_DIR
    result.backup_dir = target_dir

    # --- 1. コンフィグバックアップ("show") ---
    result.config_status = 'backing_up'
    _notify(progress_callback, result)

    let = cli.show.get()
    if let['code'] != 0:
        result.config_status = 'ng'
        result.message = let['msg']
    else:
        # alias=''を明示: FAZはファイル名をホスト名基準にする(モジュールdocstring参照)。
        # version=を指定: FGT/MSWと同様に`<hostname>_<version>_<timestamp>.<ext>`という
        # ファイル名にする(こうぢさん指摘、2026-09-23。TAC reportにもversionを含める
        # 点もFGT/MSWと同じ。下の`tac_save_let`呼び出し参照)。
        save_let = save_config(content=let['output'], hostname=cli.faz_info.hostname,
                                version=cli.faz_info.version, export_dir=target_dir)
        if save_let['code'] != 0:
            result.config_status = 'ng'
            result.message = save_let['msg']
        else:
            result.config_status = 'ok'
            result.config_saved_path = save_let['output']
    _notify(progress_callback, result)

    # --- 2. システム全体バックアップ("execute backup all-settings") ---
    result.sys_status = 'backing_up'
    _notify(progress_callback, result)

    sys_ok, sys_saved, sys_msg = _backup_system_settings(
        cli, target=target, hostname=cli.faz_info.hostname, version=cli.faz_info.version,
        export_dir=target_dir, timeout=sys_backup_timeout,
    )
    result.sys_status = 'ok' if sys_ok else 'ng'
    if sys_ok:
        result.sys_saved_path = sys_saved
    else:
        result.sys_message = sys_msg
    _notify(progress_callback, result)

    # --- 3. TAC report(`tac=True`指定時のみ) ---
    if target.tac:
        result.tac_status = 'fetching'
        _notify(progress_callback, result)

        tac_let = cli.execute.tac_report.get(expected_hostname=cli.faz_info.hostname, timeout=tac_timeout)
        if tac_let['code'] != 0:
            result.tac_status = 'ng'
            result.tac_message = tac_let['msg']
        else:
            tac_save_let = save_config(
                content=tac_let['output'], hostname=cli.faz_info.hostname,
                version=cli.faz_info.version,
                export_dir=target_dir, suffix='tacreport', ext='log',
            )
            if tac_save_let['code'] != 0:
                result.tac_status = 'ng'
                result.tac_message = tac_save_let['msg']
            else:
                result.tac_status = 'ok'
                result.tac_saved_path = tac_save_let['output']
        _notify(progress_callback, result)

    cli.logout()
    result.status = 'done'
    _notify(progress_callback, result)
    return result


def _backup_system_settings(cli: FazCli, target: FazTarget, hostname: str, version: str,
                             export_dir: str, timeout: Optional[float]) -> tuple[bool, str, str]:
    """`execute backup all-settings`によるシステム全体バックアップを1回実行する。

    手順:
        1. 一時ディレクトリを受信先にした`common.ftp.SimpleFtpServer`を起動する。
        2. `get_src_addr(target.addr)`で、FAZから見て到達可能なこちら側のアドレスを
           決定する。
        3. `cli.execute.backup_all_settings.run()`でコマンドを実行し、完了(成功/失敗)
           まで待つ(内部で非同期の転送完了メッセージまで待ち受ける。
           `net_config.ftnt.faz.cli.execute.backup_all_settings`参照)。
        4. 成功していれば、FTPサーバへ実際にアップロードされたファイルを
           `wait_for_file()`で確認し、`save_binary_file()`で最終的な保存先
           (`export_dir`、ファイル名はコンフィグバックアップと同じくホスト名・
           バージョン基準)へコピーする。

    戻り値は`(ok, saved_path, message)`。

    2026-09-23、こうぢさんより「コンフィグバックアップとシステムバックアップで
    ホスト名の処理が違っている」とのご指摘があり修正した経緯がある。以前は
    ここで`hostname`が空の場合に`target.alias or target.addr`という独自の
    フォールバックを行っていたが、これだと`hostname`取得に失敗した際、
    コンフィグバックアップ側(`save_config()`の`alias or hostname or 'unknown'`)は
    `unknown_...`になるのに対し、システムバックアップ側だけ`<addr>_...`になる、
    という不統一が生じていた。両者とも`save_config()`/`save_binary_file()`の
    共通フォールバックロジック(`config_backup.save._build_filename()`)に
    委ねることで統一した(`hostname`が空のまま渡された場合は、下の
    `save_binary_file()`呼び出し側で`alias=''`のため`'unknown'`になる)。

    2026-09-23追加: `version`(FortiAnalyzerOSバージョン、`major.minor.patch`形式)も
    コンフィグバックアップ側と同様に`save_binary_file()`へ渡すようにした
    (こうぢさん指摘: 「他のバックアップファイル名と同様に`ホスト名_バージョン_~`と
    なるように」。FGT本体の`save_config(..., version=cli.fgt_info.version)`と
    同じ考え方。TAC reportにもversionを含めるよう統一した点もFGT/MSWと同じ、
    詳細は`config_backup.fortigate._fetch_tac_report()`のdocstring参照)。
    """
    ftp_user = target.ftp_user or _random_ftp_credential()
    ftp_password = target.ftp_password or _random_ftp_credential()

    src_addr_result = get_src_addr(target.addr)
    if not src_addr_result.ok:
        return False, '', (
            f'[Error] Failed to determine local address reachable from FAZ '
            f'({target.addr}): {src_addr_result.message}'
        )
    local_addr = src_addr_result.value

    with tempfile.TemporaryDirectory(prefix='booyaa_faz_systembackup_') as staging_dir:
        with SimpleFtpServer(directory=staging_dir, user=ftp_user, password=ftp_password,
                              port=target.ftp_port) as ftp_server:
            # このファイル名はFTPで一時受信する際の名前でしかなく(最終的な保存先の
            # ファイル名は下の`save_binary_file()`が別途組み立てる)、単に空文字列に
            # よる不自然な名前を避けるための最小限のフォールバック。
            remote_filename = f"{hostname or 'unknown'}_systembackup.dat"
            config_password = target.config_password or target.password

            let = cli.execute.backup_all_settings.run(
                ftp_addr=local_addr,
                filename=remote_filename,
                ftp_user=ftp_user,
                ftp_password=ftp_password,
                config_password=config_password,
                ftp_port=ftp_server.port,
                timeout=timeout,
            )
            if let['code'] != 0:
                return False, '', let['msg']

            received = ftp_server.wait_for_file(timeout=_FTP_RECEIVE_WAIT_TIMEOUT)
            if received is None:
                return False, '', (
                    '[Error] execute backup all-settings reported success, '
                    'but no file was received via FTP'
                )

            save_let = save_binary_file(
                src_path=received.path, hostname=hostname, version=version,
                export_dir=export_dir, suffix='systembackup',
            )
            if save_let['code'] != 0:
                return False, '', save_let['msg']

            return True, save_let['output'], ''


def run_faz_backup(targets: list[FazTarget], backup_dir: Optional[str], timeout: float = 60.0,
                    sys_backup_timeout: Optional[float] = None, tac_timeout: Optional[float] = None,
                    progress_callback: Optional[ProgressCallback] = None) -> list[FazBackupResult]:
    """対象一覧を順次バックアップする(他機種同様、v1は逐次実行)。"""
    return [
        backup_one_faz(target, backup_dir=backup_dir, timeout=timeout,
                        sys_backup_timeout=sys_backup_timeout, tac_timeout=tac_timeout,
                        progress_callback=progress_callback)
        for target in targets
    ]
