"""FortiGate(+Secondary)のバックアップオーケストレーション。

`net_config.ftnt.fgt.cli.FgtCli`(機器操作)を呼び出し、結果をファイルに保存する
薄い層。旧実装(`fgt_backup.py`)にあった、機種ごとのCLI操作の詳細は
`net_config`側に移設済みで、ここでは「対象一覧を回してバックアップを取り、
保存する」というオーケストレーションのみを担当する。

MSW(FortiLink管理スイッチ)のバックアップは今回のスコープ外(次フェーズで
`net_config.ftnt.msw.cli`実装後に対応予定)。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from net_config.ftnt.fgt.cli import FgtCli
from net_config.ftnt.fgt.model.fgt_info import FgtInfo

from config_backup.save import save_config


# 進捗通知コールバック。 backup_one()/run_fortigate_backup() の実行中、
# `result`(BackupResult)のいずれかのフィールドが更新される都度
# `callback(result)`の形で呼ばれる(同一オブジェクトを使い回しで渡す)。
# 呼び出し側は`result.status`/`result.secondary_status`を見て現在の表示状態を
# 判断する(値の一覧は`BackupResult`のコメントを参照)。
#
# 2026-09-16変更: 従来は`callback(target, stage)`という「stage名の通知」だったが、
# 「実行中の表示と結果表示を同じ描画関数にしたい」という要望に対応するため、
# 表示に必要な情報(hostname/secondary_hostname/message等)を持った`result`
# そのものを渡す形に変更した。CLI側は`result`単体からテーブルの行を組み立てられる。
ProgressCallback = Callable[['BackupResult'], None]


def _notify(callback: Optional[ProgressCallback], result: 'BackupResult') -> None:
    if callback is not None:
        callback(result)


CSV_HEADERS = ['addr', 'user', 'password', 'alias', 'get_secondary', 'backup_dir', 'ssh_port', 'tac']
CSV_HEADER_ALIASES = {'fg_addr', 'addr'}


@dataclass
class FortigateTarget:
    addr: str
    user: str
    password: str
    alias: str = ''
    ssh_port: int = 22
    get_secondary: bool = False
    backup_dir: str = ''
    # TAC report(`execute tac report`)も取得するか(2026-09-16追加)。
    tac: bool = False


@dataclass
class BackupResult:
    """1対象(Primary、および必要ならSecondary)分のバックアップ結果。

    `status`/`secondary_status`は表示専用の状態文字列で、CLI側の描画関数
    (`config_backup.cli._build_table`)が「Login」「Backup」列の文言を決めるのに
    使う。処理の途中経過(実行中のprogress_callback経由の通知)と、処理完了後の
    最終結果表示の両方で、同じ`result`オブジェクト・同じ描画関数を使うことで
    「実行中の表示と結果表示を同じUIにする」という要件を満たしている。

    status(Primary側)の取り得る値:
        ''            未着手
        'connecting'  接続中
        'login_ok'    ログイン成功、バックアップ取得中
        'login_ng'    ログイン失敗(終了状態)
        'backup_ok'   バックアップ成功(終了状態)
        'backup_ng'   バックアップ失敗(終了状態)

    secondary_status(Secondary側)の取り得る値:
        ''               Secondary対象外、またはまだ何も分かっていない
        'known'          ホスト名は判明したが、まだSecondaryへの接続は未着手
                         (`get system ha status`はPrimaryログイン直後に取得済みのため、
                         Secondaryへの接続自体を試みる前からホスト名だけは分かる)
        'connecting'     Secondaryへの接続中
        'not_available'  HA構成でない等、Secondary自体が存在しない(終了状態)
        'login_ng'       Secondaryへのログイン失敗(終了状態)
        'backup_ok'      Secondaryのバックアップ成功(終了状態)
        'backup_ng'      Secondaryへのログインは成功したがバックアップ取得に失敗(終了状態)

    tac_status/secondary_tac_status(TAC report、`--tac`指定時のみ、2026-09-16追加)の
    取り得る値:
        ''           TAC report未要求、またはまだ何も分かっていない
        'fetching'   `execute tac report`取得中(生成に数分かかることがある)
        'ok'         取得・保存成功(終了状態)
        'ng'         取得・保存失敗(終了状態)
    """
    target: FortigateTarget
    hostname: str = ''
    login_ok: bool = False
    backup_ok: bool = False
    saved_path: str = ''
    secondary_requested: bool = False
    secondary_ok: bool = False
    secondary_hostname: str = ''
    secondary_saved_path: str = ''
    # message/secondary_message: テーブルのMessage列にそのまま表示される文言。
    # 失敗時はエラーメッセージ、成功時は'success'固定文言が入る(2026-09-16、
    # 「成功時もPrimary/Secondaryそれぞれでsuccessを表示してほしい」との要望への
    # 対応)。Secondaryの行はテーブル上ではPrimary行の下の別行として表示される
    # ため、Secondary側のsuccessは自然に改行された状態で表示される。
    message: str = ''
    secondary_message: str = ''
    status: str = ''
    secondary_status: str = ''
    # TAC report(`execute tac report`)関連(`target.tac`指定時のみ使う、2026-09-16追加)。
    # Primary/Secondaryそれぞれ独立に取得するため、状態・保存先・メッセージも別々に持つ。
    tac_requested: bool = False
    tac_ok: bool = False
    tac_status: str = ''
    tac_saved_path: str = ''
    tac_message: str = ''
    secondary_tac_ok: bool = False
    secondary_tac_status: str = ''
    secondary_tac_saved_path: str = ''
    secondary_tac_message: str = ''

    @property
    def ok(self) -> bool:
        if not (self.login_ok and self.backup_ok):
            return False
        if self.tac_requested and not self.tac_ok:
            return False
        if self.secondary_requested:
            if not self.secondary_ok:
                return False
            if self.tac_requested and not self.secondary_tac_ok:
                return False
        return True


def load_fortigate_list_csv(csv_path: str | Path) -> list[FortigateTarget]:
    """旧`fgt_backup.py`のCSV形式(ヘッダ任意)を読み込む。

    列: addr,user,password,alias,get_secondary,backup_dir,ssh_port,tac
    (旧実装にあった`https_port`列は、API操作を行わない本実装では不要のため廃止。
    `tac`列はTAC report取得要否で、2026-09-16に追加。互換のため列数が多い行は
    末尾を切り捨て、少ない行は空文字で補う)。
    """
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        raise FileNotFoundError(f'target csv not found: {csv_path.resolve()}')

    targets: list[FortigateTarget] = []
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

        targets.append(FortigateTarget(
            addr=row_dict['addr'].strip(),
            user=row_dict['user'].strip(),
            password=row_dict['password'],
            alias=row_dict['alias'].strip(),
            get_secondary=row_dict['get_secondary'].strip().lower() in ('yes', 'true', '1'),
            backup_dir=row_dict['backup_dir'].strip(),
            ssh_port=int(row_dict['ssh_port']) if row_dict['ssh_port'].strip() else 22,
            tac=row_dict['tac'].strip().lower() in ('yes', 'true', '1'),
        ))

    return targets


def backup_one(target: FortigateTarget, backup_dir: str, full: bool = False,
                timeout: float = 60.0, tac_timeout: Optional[float] = None,
                progress_callback: Optional[ProgressCallback] = None) -> BackupResult:
    """1台のFortiGate(必要ならSecondaryも)をバックアップする。

    `tac_timeout`: `target.tac`指定時、`execute tac report`取得のタイムアウト(秒)。
    生成に数分かかることがあるコマンドのため、通常のコマンドタイムアウト(`timeout`)とは
    独立に指定できるようにしてある。未指定時はnet_config側の既定値(300秒)に委ねる。
    """
    result = BackupResult(target=target, secondary_requested=target.get_secondary,
                           tac_requested=target.tac, status='connecting')
    _notify(progress_callback, result)

    fgt_info = FgtInfo(alias=target.alias)
    cli = FgtCli(fgt_info, timeout=timeout)
    cli.set_target(
        fgt_addr=target.addr,
        fgt_user=target.user,
        fgt_password=target.password,
        fgt_alias=target.alias,
        fgt_ssh_port=target.ssh_port,
    )

    let = cli.login()
    if let['code'] != 0:
        result.message = let['msg']
        result.status = 'login_ng'
        _notify(progress_callback, result)
        return result

    result.login_ok = True
    result.hostname = cli.fgt_info.hostname
    result.status = 'login_ok'

    if target.get_secondary:
        # `get system ha status`はPrimaryのlogin()内(get_node_info())で既に
        # 取得済みのため、Secondaryへの接続を試みる前でもホスト名だけは判明している。
        # ユーザー要望により、判明した時点で(Secondaryへの接続を待たずに)UIへ反映する。
        if cli.fgt_info.exsist_secondary:
            result.secondary_hostname = cli.fgt_info.secondary_hostname
            result.secondary_status = 'known'
        else:
            result.secondary_status = 'not_available'
            result.secondary_message = '[Error] Secondary node not available (not in HA, or single-node cluster)'

    _notify(progress_callback, result)

    target_dir = target.backup_dir or backup_dir

    let = cli.show.get(full=full)
    if let['code'] != 0:
        result.message = let['msg']
        result.status = 'backup_ng'
        cli.logout()
        _notify(progress_callback, result)
        return result

    save_let = save_config(
        content=let['output'],
        hostname=cli.fgt_info.hostname,
        alias=cli.fgt_info.alias,
        version=cli.fgt_info.version,
        export_dir=target_dir,
    )
    if save_let['code'] != 0:
        result.message = save_let['msg']
        result.status = 'backup_ng'
        cli.logout()
        _notify(progress_callback, result)
        return result

    result.backup_ok = True
    result.saved_path = save_let['output']
    result.status = 'backup_ok'
    result.message = '[green]success[/]'
    _notify(progress_callback, result)

    if target.tac:
        result.tac_status = 'fetching'
        _notify(progress_callback, result)

        tac_ok, tac_saved, tac_msg = _fetch_tac_report(
            cli, hostname=cli.fgt_info.hostname, alias=cli.fgt_info.alias,
            export_dir=target_dir, timeout=tac_timeout,
        )
        result.tac_ok = tac_ok
        result.tac_status = 'ok' if tac_ok else 'ng'
        if tac_ok:
            result.tac_saved_path = tac_saved
        else:
            result.tac_message = tac_msg
        _notify(progress_callback, result)

    if target.get_secondary and cli.fgt_info.exsist_secondary:
        result.secondary_status = 'connecting'
        _notify(progress_callback, result)

        _backup_secondary(cli, result, backup_dir=target_dir, full=full,
                           tac=target.tac, tac_timeout=tac_timeout,
                           progress_callback=progress_callback)

    cli.logout()
    return result


def _fetch_tac_report(cli: FgtCli, hostname: str, alias: str, export_dir: str,
                       timeout: Optional[float]) -> tuple[bool, str, str]:
    """`execute tac report`を取得し、テキストファイルとして保存する(Primary/Secondary共用)。

    こうぢ氏へのヒアリング(2026-09-16)により判明した`execute tac report`の特性
    (moreページャ対象外・`|`使用不可)を踏まえ、net_config側では生コマンドを
    そのまま送信する(`net_config.ftnt.fgt.cli.execute.TacReport`のdocstring参照)。

    保存ファイル名は`<alias または hostname>_tacreport_<YYYYmmdd_HHMMSS>.log`
    (こうぢ氏指定、versionは含めない)。コンフィグバックアップと同じ`export_dir`に保存する。

    戻り値は`(ok, saved_path, message)`。
    """
    let = cli.execute.tac_report.get(expected_hostname=hostname, timeout=timeout)
    if let['code'] != 0:
        return False, '', let['msg']

    save_let = save_config(
        content=let['output'],
        hostname=hostname,
        alias=alias,
        export_dir=export_dir,
        suffix='tacreport',
        ext='log',
    )
    if save_let['code'] != 0:
        return False, '', save_let['msg']

    return True, save_let['output'], ''


def _backup_secondary(cli: FgtCli, result: BackupResult, backup_dir: str, full: bool,
                       tac: bool, tac_timeout: Optional[float],
                       progress_callback: Optional[ProgressCallback]) -> None:
    """Secondaryへの接続・バックアップ(・TAC report取得)を行い、`result`に反映する。

    Primary側と同様、状態が変わるたびに`result`を更新して`progress_callback`へ
    通知する(実行中UIと結果表示を同じ描画関数で扱うための設計、
    `BackupResult`のコメント参照)。戻り値は無く、`result`への破壊的更新のみ行う。
    """
    if not cli.fgt_info.exsist_secondary:
        result.secondary_status = 'not_available'
        result.secondary_message = '[Error] Secondary node not available (not in HA, or single-node cluster)'
        _notify(progress_callback, result)
        return

    let = cli.login_secondary()
    if let['code'] != 0:
        result.secondary_hostname = cli.fgt_info.secondary_hostname
        result.secondary_status = 'login_ng'
        result.secondary_message = let['msg']
        _notify(progress_callback, result)
        return

    result.secondary_hostname = cli.fgt_info.secondary_hostname

    let = cli.show.get(full=full)
    if let['code'] != 0:
        result.secondary_status = 'backup_ng'
        result.secondary_message = let['msg']
        cli.logout_secondary()
        _notify(progress_callback, result)
        return

    save_let = save_config(
        content=let['output'],
        hostname=cli.fgt_info.secondary_hostname,
        alias=cli.fgt_info.alias,
        version=cli.fgt_info.version,
        export_dir=backup_dir,
    )
    if save_let['code'] != 0:
        result.secondary_status = 'backup_ng'
        result.secondary_message = save_let['msg']
        cli.logout_secondary()
        _notify(progress_callback, result)
        return

    result.secondary_ok = True
    result.secondary_saved_path = save_let['output']
    result.secondary_status = 'backup_ok'
    result.secondary_message = '[green]success[/]'
    _notify(progress_callback, result)

    if tac:
        result.secondary_tac_status = 'fetching'
        _notify(progress_callback, result)

        tac_ok, tac_saved, tac_msg = _fetch_tac_report(
            cli, hostname=cli.fgt_info.secondary_hostname, alias=cli.fgt_info.alias,
            export_dir=backup_dir, timeout=tac_timeout,
        )
        result.secondary_tac_ok = tac_ok
        result.secondary_tac_status = 'ok' if tac_ok else 'ng'
        if tac_ok:
            result.secondary_tac_saved_path = tac_saved
        else:
            result.secondary_tac_message = tac_msg
        _notify(progress_callback, result)

    cli.logout_secondary()


def run_fortigate_backup(targets: list[FortigateTarget], backup_dir: str, full: bool = False,
                          timeout: float = 60.0, tac_timeout: Optional[float] = None,
                          progress_callback: Optional[ProgressCallback] = None) -> list[BackupResult]:
    """対象一覧を順次バックアップする(v1は逐次実行。並列化は将来検討)。

    `progress_callback`が指定された場合、各対象の処理状態が更新される都度
    `callback(result)`が呼ばれる(`result`は`BackupResult`、値の意味は
    `BackupResult`のコメントを参照)。CLI側でrich.Liveと組み合わせ、処理中の
    状態をリアルタイム表示するために追加した(逐次実行という設計自体は変更していない)。
    """
    return [
        backup_one(target, backup_dir=backup_dir, full=full, timeout=timeout,
                   tac_timeout=tac_timeout, progress_callback=progress_callback)
        for target in targets
    ]
