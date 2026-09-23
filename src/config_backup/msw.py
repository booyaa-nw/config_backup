"""Managed Switch(MSW、FortiLink管理スイッチ)のバックアップ/TAC reportオーケストレーション。

`net_config.ftnt.fgt.cli.FgtCli`でFortiGateへログインし、
`execute switch-controller get-conn-status`でぶら下がっているMSW一覧を取得、
Authorized/Upのもののみ`net_config.ftnt.msw.cli.MswCli`で多段SSHログイン→
バックアップ(・TAC report取得)を行う薄いオーケストレーション層。
`config_backup.fortigate`(FGT本体用)と対になるモジュール。

## 対象の絞り込み(こうぢ氏指定)

`execute switch-controller get-conn-status`の結果、認証状態/接続状態が
"Authorized/Up"であるものだけをバックアップ・TAC report取得の対象とする。
それ以外(Discovered/Down等)はログイン・バックアップを試みず、ステータスの
みを結果表示に含める。

## バックアップ先ディレクトリの既定値(こうぢ氏指定、FGT本体とは異なる例外)

FGT本体(`config_backup fgt`)の既定値`./booyaa_log/config`とは異なり、MSWは
**`./booyaa_log/<fgtのhostname>_msw/`** を既定値とする(FortiGateごとに
Managed Switchのバックアップをまとめるため)。`fgt`のhostnameは`get system status`
(FGTログイン時に取得済み)から得る。CLIの`-d/--directory`で明示指定された
場合、またはCSVの`backup_dir`列が指定された場合はそちらを優先する
(FGT本体側の`target_dir = target.backup_dir or backup_dir`という優先順位と
同じ考え方)。

## MSWの認証情報(旧実装踏襲、こうぢ氏確認済みの制約)

Managed Switchのユーザー名/パスワードは、対象のFortiGate配下の全スイッチで
共通の1組のみ指定できる(スイッチごとに異なる認証情報は現状非対応、旧
`msw_backup.py`/`readme_msw_backup.md`の制約をそのまま踏襲)。パスワード未指定時は
FortiGateと同じパスワードを使う(旧実装の`msw_password or fgt_password`という
デフォルト踏襲)。

## スイッチバックアップの同時実行(こうぢ氏指定、2026-09-23追加)

対象(Authorized/Up)スイッチのバックアップ(・TAC report取得)は、最大
`DEFAULT_MSW_CONCURRENCY`(既定5)台まで同時実行する。同時実行の開始タイミングは
`DEFAULT_MSW_STAGGER_DELAY`(既定1秒)間隔でずらす(FortiGateへ一斉にSSH接続要求が
飛ばないようにするため)。

MSWへの多段SSHは、FortiGate CLIセッション内から`execute ssh`を送り**同一チャネル上**
でプロンプトを待ち受ける方式(`net_config.ftnt.fgt.cli.FgtCli.execute_ssh()`)のため、
1つの`FgtCli`接続(=1つのSSHチャネル)を複数スレッドで共有すると、コマンド・応答が
混線してしまう。そのため、スイッチごとに**専用のFortiGate管理者SSHセッションを新規に
確立**した上で多段SSHする設計にした(`_backup_switch_worker()`)。これは、同時実行数
だけFortiGate側の同時管理者ログイン数を消費するということでもある(既定5。対象の
FortiGate側で同時ログイン数に制限がある場合は`concurrency`引数で調整可能)。

実装は`concurrent.futures.ThreadPoolExecutor`によるスレッド並列(旧実装
`msw_backup.py`が使っていた`ThreadPoolExecutor`/fire_and_forgetデコレータと同じ
考え方)。scrapliの`GenericDriver`(`paramiko`transport)はブロッキングI/Oのため、
`asyncio`のコルーチンではなくスレッドで並列化するのが自然な選択。進捗通知
(`progress_callback`)はスレッドをまたいで呼ばれるため、`threading.Lock`で直列化
した上で呼び出す(`rich.Live`の描画・`MswBackupResult.items`の読み取りが複数スレッド
から同時に行われて表示が乱れることを防ぐ)。
"""
from __future__ import annotations

import csv
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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


def _make_locked_callback(callback: Optional[ProgressCallback],
                           lock: threading.Lock) -> Optional[ProgressCallback]:
    """`callback`を`lock`で直列化したものに包んで返す(こうぢ氏指定の同時実行対応、
    2026-09-23追加)。

    スイッチのバックアップを複数スレッドで並列実行するため、進捗通知
    (`rich.Live`の再描画等)が複数スレッドから同時に呼ばれる可能性がある。
    呼び出し元(ユーザー指定の`progress_callback`)がスレッドセーフである保証は
    無いため、ここで直列化してから渡す。
    """
    if callback is None:
        return None

    def _wrapped(result: 'MswBackupResult') -> None:
        with lock:
            callback(result)

    return _wrapped


CSV_HEADERS = [
    'fgt_addr', 'fgt_user', 'fgt_password', 'alias',
    'msw_user', 'msw_password', 'backup_dir', 'ssh_port', 'tac',
]
CSV_HEADER_ALIASES = {'fgt_addr', 'addr'}

DEFAULT_MSW_USER = 'admin'

# スイッチバックアップの同時実行数・起動間隔の既定値(こうぢ氏指定、2026-09-23追加)。
# モジュールdocstring「スイッチバックアップの同時実行」参照。
DEFAULT_MSW_CONCURRENCY = 5
DEFAULT_MSW_STAGGER_DELAY = 1.0

# `MswBackupItem.display_status`用のAuth state短縮表記(こうぢ氏指定、2026-09-22追加)。
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
        """CLI表示用の"Auth/Up"形式の文字列(こうぢ氏指定、2026-09-22)。

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
    """MSW用バックアップ先の既定値: `./booyaa_log/<fgt_hostname>_msw`(こうぢ氏指定の例外)。

    `fgt_hostname`は`get system status`から取得したホスト名を使う(こうぢ氏の
    指示通り)。万一ホスト名が(コマンド失敗等で)取得できていない場合のみ、
    alias、それも無ければaddrにフォールバックする(ディレクトリ名が空になる
    事故を避けるための保険で、通常経路では発生しない)。
    """
    name = fgt_hostname or fgt_alias or fgt_addr
    return f'./booyaa_log/{name}_msw'


def backup_one_fgt(target: MswTarget, backup_dir: Optional[str], full: bool = False,
                    timeout: float = 60.0, tac_timeout: Optional[float] = None,
                    progress_callback: Optional[ProgressCallback] = None,
                    concurrency: int = DEFAULT_MSW_CONCURRENCY,
                    stagger_delay: float = DEFAULT_MSW_STAGGER_DELAY) -> MswBackupResult:
    """1台のFortiGate配下の、Authorized/UpなManaged Switchすべてをバックアップする。

    Args:
        backup_dir: CLIの`-d/--directory`で明示指定された値(未指定時はNone)。
            `target.backup_dir`(CSVの個別指定)が優先され、どちらも無い場合は
            `_default_backup_dir()`でFGTホスト名ベースのディレクトリを使う。
        concurrency: 対象スイッチのバックアップを同時実行する最大数(こうぢ氏指定、
            既定5)。モジュールdocstring「スイッチバックアップの同時実行」参照。
        stagger_delay: 同時実行の開始間隔(秒、こうぢ氏指定、既定1秒)。
    """
    result = MswBackupResult(target=target, status='connecting')

    # 進捗通知は複数スレッド(スイッチ並列実行時)から呼ばれる可能性があるため、
    # ここで一度だけロックに包み、以降(FGT接続部分・スイッチ処理部分いずれも)
    # このロック付きコールバックを使う(モジュールdocstring参照)。
    _lock = threading.Lock()
    notify_cb = _make_locked_callback(progress_callback, _lock)

    _notify(notify_cb, result)

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
        _notify(notify_cb, result)
        return result

    result.fgt_hostname = cli.fgt_info.hostname
    result.status = 'login_ok'
    _notify(notify_cb, result)

    let = cli.get_switch_list()
    if let['code'] != 0:
        result.message = let['msg']
        result.status = 'listing_ng'
        cli.logout()
        _notify(notify_cb, result)
        return result

    target_dir = target.backup_dir or backup_dir or _default_backup_dir(
        result.fgt_hostname, target.fgt_alias, target.fgt_addr,
    )
    result.backup_dir = target_dir

    msw_password = target.msw_password or target.fgt_password

    # 対象スイッチ全行(スキップ含む)を先に組み立てて`result.items`へ反映する。
    # スキップ対象は即座に終了状態にし、Authorized/Upなものだけを`eligible_entries`に
    # 集めて後段の同時実行に渡す(こうぢ氏指定の同時実行対応、2026-09-23変更。従来は
    # 1台ずつ順に`_backup_one_switch()`を呼んでいたが、並列実行に合わせて「一覧構築」
    # と「バックアップ実行」を分離した)。
    items: list[MswBackupItem] = []
    eligible_entries: list[tuple[MswBackupItem, object]] = []
    for sw in cli.fgt_info.msw_list:
        item = MswBackupItem(
            serial=sw.serial, hostname=sw.hostname, addr=sw.addr,
            auth_state=sw.auth_state, conn_status=sw.status, eligible=sw.eligible,
            tac_requested=target.tac,
        )
        items.append(item)

        if not item.eligible:
            item.status = 'skipped'
            # スキップ時のメッセージはオレンジで表示する(こうぢ氏指定、2026-09-22追加)。
            item.message = f'[orange3][Status] {item.display_status} (skip)[/]'
        else:
            eligible_entries.append((item, sw))

    result.items = items
    _notify(notify_cb, result)

    if eligible_entries:
        _run_switch_backups_concurrently(
            target=target, fgt_hostname=result.fgt_hostname, entries=eligible_entries,
            msw_user=target.msw_user, msw_password=msw_password, backup_dir=target_dir,
            full=full, tac=target.tac, tac_timeout=tac_timeout, timeout=timeout,
            progress_callback=notify_cb, result=result,
            concurrency=concurrency, stagger_delay=stagger_delay,
        )

    cli.logout()
    result.status = 'done'
    _notify(notify_cb, result)
    return result


def _run_switch_backups_concurrently(target: MswTarget, fgt_hostname: str, entries: list,
                                      msw_user: str, msw_password: str, backup_dir: str,
                                      full: bool, tac: bool, tac_timeout: Optional[float],
                                      timeout: float, progress_callback: Optional[ProgressCallback],
                                      result: MswBackupResult,
                                      concurrency: int = DEFAULT_MSW_CONCURRENCY,
                                      stagger_delay: float = DEFAULT_MSW_STAGGER_DELAY) -> None:
    """対象(Authorized/Up)スイッチのバックアップを、最大`concurrency`台まで同時実行する
    (こうぢ氏指定、2026-09-23追加)。モジュールdocstring「スイッチバックアップの
    同時実行」参照。

    Args:
        entries: `(MswBackupItem, MswConnStatus)`のタプルのリスト(バックアップ対象のみ)。
        concurrency: 同時実行数の上限(既定`DEFAULT_MSW_CONCURRENCY`)。
        stagger_delay: ワーカーの起動(`executor.submit()`)間隔(秒、既定
            `DEFAULT_MSW_STAGGER_DELAY`)。`concurrency`を超える分は
            `ThreadPoolExecutor`のキューで順に処理される。
    """
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        futures = []
        for i, (item, sw) in enumerate(entries):
            if i > 0 and stagger_delay > 0:
                time.sleep(stagger_delay)
            futures.append(executor.submit(
                _backup_switch_worker, target, fgt_hostname, item, sw, msw_user, msw_password,
                backup_dir, full, tac, tac_timeout, timeout, progress_callback, result,
            ))

        # ワーカー内の想定外の例外(scrapli/paramikoの未知の例外等)を握り潰さず、
        # 呼び出し元まで伝播させる(`_backup_switch_worker()`自身も主要な失敗経路は
        # `item.status`/`item.message`に記録するが、二重の安全策として)。
        for f in futures:
            f.result()


def _backup_switch_worker(target: MswTarget, fgt_hostname: str, item: MswBackupItem, sw,
                           msw_user: str, msw_password: str, backup_dir: str, full: bool,
                           tac: bool, tac_timeout: Optional[float], timeout: float,
                           progress_callback: Optional[ProgressCallback],
                           result: MswBackupResult) -> None:
    """1台のスイッチ用に、FortiGateへの**専用の**管理者SSHセッションを新規に確立し、
    そのセッション上で多段SSH〜バックアップ(・TAC report取得)を行う
    (こうぢ氏指定の同時実行対応、2026-09-23追加)。

    `backup_one_fgt()`が使う共有の`FgtCli`接続(1本のSSHチャネル)を複数スレッドで
    使い回すことはできない(コマンド・応答が混線する)ため、スイッチごとに新規の
    FortiGateログインを行う。ノード情報(`get system status`)は既に判明済みのため
    再取得しない(`node_info=False`)が、`execute_ssh()`の自己ホスト名チェック
    (誤って自分自身のプロンプトのままになっていないかの確認)のために
    `fgt_info.hostname`のみ手動で設定する。
    """
    item.status = 'connecting'
    _notify(progress_callback, result)

    fgt_info = FgtInfo(alias=target.fgt_alias)
    fgt_info.hostname = fgt_hostname
    cli = FgtCli(fgt_info, timeout=timeout)
    cli.set_target(
        fgt_addr=target.fgt_addr,
        fgt_user=target.fgt_user,
        fgt_password=target.fgt_password,
        fgt_alias=target.fgt_alias,
        fgt_ssh_port=target.fgt_ssh_port,
    )

    let = cli.login(node_info=False)
    if let['code'] != 0:
        item.status = 'login_ng'
        item.message = f'[Error] failed to open dedicated FGT session for switch backup: {let["msg"]}'
        _notify(progress_callback, result)
        return

    try:
        _backup_one_switch(cli, item, sw, msw_user=msw_user, msw_password=msw_password,
                            backup_dir=backup_dir, full=full, tac=tac,
                            tac_timeout=tac_timeout, progress_callback=progress_callback,
                            result=result)
    except Exception as e:
        # 想定外の例外で1台の処理が落ちても、他のスイッチの並列処理・呼び出し元の
        # ThreadPoolExecutorを止めないよう、ここで捕捉してitemに反映する。
        item.status = 'backup_ng'
        item.message = f'[Error] unexpected error during switch backup: {e}'
        _notify(progress_callback, result)
    finally:
        cli.logout()


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
    # (`get system status`のシリアル番号が一致するか)をこうぢ氏指定により照合する
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
            tac_save_let = save_config(
                content=tac_let['output'],
                hostname=msw_cli.msw_info.hostname or item.serial,
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
                    progress_callback: Optional[ProgressCallback] = None,
                    concurrency: int = DEFAULT_MSW_CONCURRENCY,
                    stagger_delay: float = DEFAULT_MSW_STAGGER_DELAY) -> list[MswBackupResult]:
    """対象(FortiGate)一覧を順次処理する(FGT間は逐次、`fortigate.run_fortigate_backup`と
    同じ方針。各FGT配下のスイッチ同時実行数は`concurrency`/`stagger_delay`で指定する、
    こうぢ氏指定、2026-09-23追加)。"""
    return [
        backup_one_fgt(target, backup_dir=backup_dir, full=full, timeout=timeout,
                        tac_timeout=tac_timeout, progress_callback=progress_callback,
                        concurrency=concurrency, stagger_delay=stagger_delay)
        for target in targets
    ]
