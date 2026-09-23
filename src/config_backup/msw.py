"""Managed Switch(MSW、FortiLink管理スイッチ)のバックアップ/TAC reportオーケストレーション。

`net_config.ftnt.fgt.cli.FgtCli`でFortiGateへログインし、
`execute switch-controller get-conn-status`でぶら下がっているMSW一覧を取得、
Authorized/Upのもののみ`net_config.ftnt.msw.cli.MswCli`で多段SSHログイン→
バックアップ(・TAC report取得)を行う薄いオーケストレーション層。
`config_backup.fortigate`(FGT本体用)と対になるモジュール。

## 対象の絞り込み(こうぢさん指定)

`execute switch-controller get-conn-status`の結果、認証状態/接続状態が
"Authorized/Up"であるものだけをバックアップ・TAC report取得の対象とする。
それ以外(Discovered/Down等)はログイン・バックアップを試みず、ステータスの
みを結果表示に含める。

## バックアップ先ディレクトリの既定値(こうぢさん指定)

既定値は**`./booyaa_log/config/<fgtのhostname>_msw/`**とする(FortiGateごとに
Managed Switchのバックアップをまとめつつ、FGT本体(`config_backup fgt`)の
既定値`./booyaa_log/config`と同じ`config/`配下に統一する)。`fgt`のhostnameは
`get system status`(FGTログイン時に取得済み)から得る。CLIの`-d/--directory`で
明示指定された場合、またはCSVの`backup_dir`列が指定された場合はそちらを優先する
(FGT本体側の`target_dir = target.backup_dir or backup_dir`という優先順位と
同じ考え方)。

2026-09-22変更: 従来は`./booyaa_log/<fgtのhostname>_msw/`(FGT本体の
`./booyaa_log/config`とは別系統のトップレベルディレクトリ)だったが、
こうぢさんより「`booyaa_log\\config\\<fgtのhostname>_msw`に修正してほしい」
との依頼があり、FGT本体と同じ`config/`配下にまとめる形に変更した。

## MSWの認証情報(旧実装踏襲、こうぢさん確認済みの制約)

Managed Switchのユーザー名/パスワードは、対象のFortiGate配下の全スイッチで
共通の1組のみ指定できる(スイッチごとに異なる認証情報は現状非対応、旧
`msw_backup.py`/`readme_msw_backup.md`の制約をそのまま踏襲)。パスワード未指定時は
FortiGateと同じパスワードを使う(旧実装の`msw_password or fgt_password`という
デフォルト踏襲)。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from net_config.ftnt.fgt.cli import FgtCli
from net_config.ftnt.fgt.model.fgt_info import FgtInfo
from net_config.ftnt.msw.cli import MswCli
from net_config.ftnt.msw.model.msw_info import MswInfo

from config_backup.save import save_config


ProgressCallback = Callable[['MswBackupResult'], None]


def _notify(callback: Optional[ProgressCallback], result: 'MswBackupResult') -> None:
    if callback is not None:
        callback(result)


CSV_HEADERS = [
    'fgt_addr', 'fgt_user', 'fgt_password', 'alias',
    'msw_user', 'msw_password', 'backup_dir', 'ssh_port', 'tac',
]
CSV_HEADER_ALIASES = {'fgt_addr', 'addr'}

DEFAULT_MSW_USER = 'admin'

# `MswBackupItem.display_status`用のAuth state短縮表記(こうぢさん指定、2026-09-22追加)。
# テーブルの列幅の都合で"Authorized"/"Deauthorized"が省略表示されてしまう問題への対応。
_AUTH_STATE_ABBR: dict[str, str] = {
    'authorized': 'Auth',
    'deauthorized': 'Deauth',
}


@dataclass
class MswTarget:
    """処理対象のFortiGate 1台分(配下のMSWは実行時に`get-conn-status`で発見する)。"""
    fgt_addr: str
    fgt_user: str
    fgt_password: str
    fgt_alias: str = ''
    fgt_ssh_port: int = 22
    msw_user: str = DEFAULT_MSW_USER
    msw_password: str = ''
    backup_dir: str = ''
    tac: bool = False


@dataclass
class MswBackupItem:
    """1台のManaged Switch分の処理結果。

    status(表示用)の取り得る値:
        ''            未着手
        'skipped'     Authorized/Up以外のため対象外(終了状態)
        'connecting'  ログイン中
        'login_ok'    ログイン成功、バックアップ取得中
        'login_ng'    ログイン失敗(終了状態)
        'backup_ok'   バックアップ成功(終了状態)
        'backup_ng'   バックアップ失敗(終了状態)

    tac_status(`--tac`指定時のみ)の取り得る値はFGT側`BackupResult`と同じ
    (''/'fetching'/'ok'/'ng')。
    """
    serial: str = ''
    hostname: str = ''
    addr: str = ''
    auth_state: str = ''
    conn_status: str = ''
    eligible: bool = False
    status: str = ''
    message: str = ''
    saved_path: str = ''
    tac_requested: bool = False
    tac_status: str = ''
    tac_saved_path: str = ''
    tac_message: str = ''

    @property
    def display_status(self) -> str:
        """CLI表示用の"Auth/Up"形式の文字列(こうぢさん指定、2026-09-22)。

        テーブルの列幅の都合でAuth stateが省略表示されてしまう問題への対応として、
        "Authorized"→"Auth"、"Deauthorized"→"Deauth"と短縮する(それ以外の値
        (例: "Discovered")はそのまま表示する)。`conn_status`側は実機の表記
        (`Up`/`Down`)をそのまま使う。
        """
        auth = _AUTH_STATE_ABBR.get(self.auth_state.strip().lower(), self.auth_state)
        return f'{auth}/{self.conn_status}'

    @property
    def ok(self) -> bool:
        if not self.eligible:
            return True  # 対象外はそもそも失敗として扱わない
        if self.status != 'backup_ok':
            return False
        if self.tac_requested and self.tac_status != 'ok':
            return False
        return True


@dataclass
class MswBackupResult:
    """1台のFortiGate配下、全Managed Switchの処理結果。

    status(FGT接続、表示用)の取り得る値:
        'connecting'   FGTへ接続中
        'login_ok'     FGTログイン成功、スイッチ一覧取得中
        'login_ng'     FGTログイン失敗(終了状態)
        'listing_ng'   スイッチ一覧("execute switch-controller get-conn-status")取得失敗(終了状態)
        'done'         全スイッチの処理完了(終了状態、個々のスイッチの成否はitems参照)
    """
    target: MswTarget
    fgt_hostname: str = ''
    backup_dir: str = ''
    status: str = ''
    message: str = ''
    items: list[MswBackupItem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        if self.status not in ('done',):
            return False
        return all(item.ok for item in self.items)


def load_msw_list_csv(csv_path: str | Path) -> list[MswTarget]:
    """MSWバックアップ対象(FortiGate一覧)のCSVを読み込む。

    列: fgt_addr,fgt_user,fgt_password,alias,msw_user,msw_password,backup_dir,ssh_port,tac
    (`config_backup.fortigate.load_fortigate_list_csv`と同じ緩やかな列数調整を行う:
    列数が多い行は末尾を切り捨て、少ない行は空文字で補う)。
    """
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        raise FileNotFoundError(f'target csv not found: {csv_path.resolve()}')

    targets: list[MswTarget] = []
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

        targets.append(MswTarget(
            fgt_addr=row_dict['fgt_addr'].strip(),
            fgt_user=row_dict['fgt_user'].strip(),
            fgt_password=row_dict['fgt_password'],
            fgt_alias=row_dict['alias'].strip(),
            msw_user=row_dict['msw_user'].strip() or DEFAULT_MSW_USER,
            msw_password=row_dict['msw_password'],
            backup_dir=row_dict['backup_dir'].strip(),
            fgt_ssh_port=int(row_dict['ssh_port']) if row_dict['ssh_port'].strip() else 22,
            tac=row_dict['tac'].strip().lower() in ('yes', 'true', '1'),
        ))

    return targets


def _default_backup_dir(fgt_hostname: str, fgt_alias: str, fgt_addr: str) -> str:
    """MSW用バックアップ先の既定値: `./booyaa_log/config/<fgt_hostname>_msw`。

    `fgt_hostname`は`get system status`から取得したホスト名を使う(こうぢさんの
    指示通り)。万一ホスト名が(コマンド失敗等で)取得できていない場合のみ、
    alias、それも無ければaddrにフォールバックする(ディレクトリ名が空になる
    事故を避けるための保険で、通常経路では発生しない)。

    2026-09-22変更: 従来は`./booyaa_log/{name}_msw`(FGT本体の
    `./booyaa_log/config`とは別系統のトップレベルディレクトリ)だったが、
    こうぢさんより「`booyaa_log\\config\\<fgtのhostname>_msw`に修正してほしい」
    との依頼があり、FGT本体と同じ`config/`配下にまとめる形に変更した。
    """
    name = fgt_hostname or fgt_alias or fgt_addr
    return f'./booyaa_log/config/{name}_msw'


def backup_one_fgt(target: MswTarget, backup_dir: Optional[str], full: bool = False,
                    timeout: float = 60.0, tac_timeout: Optional[float] = None,
                    progress_callback: Optional[ProgressCallback] = None) -> MswBackupResult:
    """1台のFortiGate配下の、Authorized/UpなManaged Switchすべてをバックアップする。

    Args:
        backup_dir: CLIの`-d/--directory`で明示指定された値(未指定時はNone)。
            `target.backup_dir`(CSVの個別指定)が優先され、どちらも無い場合は
            `_default_backup_dir()`でFGTホスト名ベースのディレクトリを使う。
    """
    result = MswBackupResult(target=target, status='connecting')
    _notify(progress_callback, result)

    fgt_info = FgtInfo(alias=target.fgt_alias)
    cli = FgtCli(fgt_info, timeout=timeout)
    cli.set_target(
        fgt_addr=target.fgt_addr,
        fgt_user=target.fgt_user,
        fgt_password=target.fgt_password,
        fgt_alias=target.fgt_alias,
        fgt_ssh_port=target.fgt_ssh_port,
    )

    let = cli.login()
    if let['code'] != 0:
        result.message = let['msg']
        result.status = 'login_ng'
        _notify(progress_callback, result)
        return result

    result.fgt_hostname = cli.fgt_info.hostname
    result.status = 'login_ok'
    _notify(progress_callback, result)

    let = cli.get_switch_list()
    if let['code'] != 0:
        result.message = let['msg']
        result.status = 'listing_ng'
        cli.logout()
        _notify(progress_callback, result)
        return result

    target_dir = target.backup_dir or backup_dir or _default_backup_dir(
        result.fgt_hostname, target.fgt_alias, target.fgt_addr,
    )
    result.backup_dir = target_dir

    msw_password = target.msw_password or target.fgt_password

    items: list[MswBackupItem] = []
    for sw in cli.fgt_info.msw_list:
        item = MswBackupItem(
            serial=sw.serial, hostname=sw.hostname, addr=sw.addr,
            auth_state=sw.auth_state, conn_status=sw.status, eligible=sw.eligible,
            tac_requested=target.tac,
        )
        items.append(item)
        result.items = items

        if not item.eligible:
            item.status = 'skipped'
            # スキップ時のメッセージはオレンジで表示する(こうぢさん指定、2026-09-22追加)。
            item.message = f'[orange3][Status] {item.display_status} (skip)[/]'
            _notify(progress_callback, result)
            continue

        item.status = 'connecting'
        _notify(progress_callback, result)

        _backup_one_switch(cli, item, sw, msw_user=target.msw_user, msw_password=msw_password,
                            backup_dir=target_dir, full=full, tac=target.tac,
                            tac_timeout=tac_timeout, progress_callback=progress_callback,
                            result=result)

    cli.logout()
    result.status = 'done'
    _notify(progress_callback, result)
    return result


def _backup_one_switch(fgt_cli: FgtCli, item: MswBackupItem, sw, msw_user: str, msw_password: str,
                        backup_dir: str, full: bool, tac: bool, tac_timeout: Optional[float],
                        progress_callback: Optional[ProgressCallback], result: MswBackupResult) -> None:
    """1台のManaged Switchへの多段SSHログイン〜バックアップ(・TAC report)を行う。

    戻り値は無く、`item`(および`result`経由の通知)への破壊的更新のみ行う。
    """
    msw_info = MswInfo(addr=sw.addr, user=msw_user, password=msw_password,
                        auth_state=sw.auth_state, status=sw.status)
    msw_cli = MswCli(fgt_cli, msw_info)

    # 多段SSH自体が成功していても、実際に意図したスイッチへログインできているか
    # (`get system status`のシリアル番号が一致するか)をこうぢさん指定により照合する
    # (`net_config.ftnt.msw.cli.MswCli.login()`のdocstring参照、2026-09-22追加)。
    let = msw_cli.login(expected_serial=sw.serial)
    if let['code'] != 0:
        item.status = 'login_ng'
        item.message = let['msg']
        _notify(progress_callback, result)
        return

    item.status = 'login_ok'
    item.hostname = msw_cli.msw_info.hostname or item.hostname
    _notify(progress_callback, result)

    let = msw_cli.show.get(full=full)
    if let['code'] != 0:
        item.status = 'backup_ng'
        item.message = let['msg']
        msw_cli.logout()
        _notify(progress_callback, result)
        return

    save_let = save_config(
        content=let['output'],
        hostname=msw_cli.msw_info.hostname or item.serial,
        version=msw_cli.msw_info.version,
        export_dir=backup_dir,
    )
    if save_let['code'] != 0:
        item.status = 'backup_ng'
        item.message = save_let['msg']
        msw_cli.logout()
        _notify(progress_callback, result)
        return

    item.status = 'backup_ok'
    item.saved_path = save_let['output']
    item.message = '[green]success[/]'
    _notify(progress_callback, result)

    if tac:
        item.tac_status = 'fetching'
        _notify(progress_callback, result)

        tac_let = msw_cli.execute.tac_report.get(
            expected_hostname=msw_cli.msw_info.hostname or item.serial, timeout=tac_timeout,
        )
        if tac_let['code'] != 0:
            item.tac_status = 'ng'
            item.tac_message = tac_let['msg']
        else:
            # version=を指定: 2026-09-23、こうぢさんより「TAC reportにもコンフィグ/
            # システムバックアップと同様versionを付けて揃えたい」とのご要望があり、
            # FGT/MSW/FAZ全機種のTAC reportファイル名にversionを含めるよう統一した
            # (詳細は`config_backup.fortigate._fetch_tac_report()`のdocstring参照)。
            tac_save_let = save_config(
                content=tac_let['output'],
                hostname=msw_cli.msw_info.hostname or item.serial,
                version=msw_cli.msw_info.version,
                export_dir=backup_dir,
                suffix='tacreport',
                ext='log',
            )
            if tac_save_let['code'] != 0:
                item.tac_status = 'ng'
                item.tac_message = tac_save_let['msg']
            else:
                item.tac_status = 'ok'
                item.tac_saved_path = tac_save_let['output']
        _notify(progress_callback, result)

    msw_cli.logout()


def run_msw_backup(targets: list[MswTarget], backup_dir: Optional[str], full: bool = False,
                    timeout: float = 60.0, tac_timeout: Optional[float] = None,
                    progress_callback: Optional[ProgressCallback] = None) -> list[MswBackupResult]:
    """対象(FortiGate)一覧を順次処理する(v1は逐次実行、`fortigate.run_fortigate_backup`と同じ方針)。"""
    return [
        backup_one_fgt(target, backup_dir=backup_dir, full=full, timeout=timeout,
                        tac_timeout=tac_timeout, progress_callback=progress_callback)
        for target in targets
    ]
