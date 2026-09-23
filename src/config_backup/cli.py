"""config_backup: NW機器のコンフィグバックアップツール(マルチベンダー対応)。

サブコマンドは`fgt`(FortiGate本体、旧`fortigate`から2026-09-22に識別子を
変更)・`msw`(FortiLink管理スイッチ、2026-09-22追加)・`faz`(FortiAnalyzer、
2026-09-22追加)の3つ。将来Alaxala等を追加する際は、`build_parser()`に
新しいサブコマンドを追加していく想定(既存サブコマンドの引数には影響しない)。

`./bin`シムの起動方式(`python -m <モジュール>`)に合わせ、本モジュールは
`if __name__ == '__main__':`からmain()を呼び出せるようにしてある。
"""
from __future__ import annotations

import argparse
import sys
from textwrap import dedent

from rich.console import Console
from rich.live import Live
from rich.table import Table

from config_backup.faz import (
    FTP_DEFAULT_PORT,
    FazBackupResult,
    FazTarget,
    load_faz_list_csv,
    run_faz_backup,
)
from config_backup.fortigate import (
    BackupResult,
    FortigateTarget,
    load_fortigate_list_csv,
    run_fortigate_backup,
)
from config_backup.msw import (
    MswBackupResult,
    MswTarget,
    load_msw_list_csv,
    run_msw_backup,
)


# BackupResult.status / secondary_status(値の意味は`fortigate.BackupResult`の
# コメントを参照)を、テーブルの(Login列, Backup列)の表示文言に変換する。
# _build_table()が実行中(rich.Live経由)・完了後の結果表示の両方で使う共通の
# マッピングであり、「実行中のUIと結果のUIを同じにする」ための土台になっている。
_STATUS_TEXT: dict[str, tuple[str, str]] = {
    'connecting': ('[yellow]Connecting...[/]', ''),
    'login_ok': ('[green]OK[/]', '[yellow]Backing up...[/]'),
    'login_ng': ('[red]NG[/]', ''),
    'backup_ok': ('[green]OK[/]', '[green]OK[/]'),
    'backup_ng': ('[green]OK[/]', '[red]NG[/]'),
}
_SECONDARY_STATUS_TEXT: dict[str, tuple[str, str]] = {
    'known': ('-', ''),
    'connecting': ('[yellow]Connecting...[/]', ''),
    'not_available': ('[red]NG[/]', ''),
    'login_ng': ('[red]NG[/]', ''),
    'backup_ok': ('[green]OK[/]', '[green]OK[/]'),
    'backup_ng': ('[green]OK[/]', '[red]NG[/]'),
}

# BackupResult.tac_status / secondary_tac_status を、テーブルのTAC列の表示文言に
# 変換する(`--tac`指定時のみ使う、2026-09-16追加)。未要求(''のまま)の場合は
# `_build_table()`側で固定の'-'を出すため、ここには含めない。
_TAC_STATUS_TEXT: dict[str, str] = {
    'fetching': '[yellow]Fetching...[/]',
    'ok': '[green]OK[/]',
    'ng': '[red]NG[/]',
}


def _build_table(results: list[BackupResult], title: str) -> Table:
    """バックアップ結果一覧のテーブルを組み立てる。

    実行中の`rich.Live`表示(`_run_with_live_progress()`)と、完了後の最終結果
    表示のどちらからも、この同じ関数を使う(2026-09-16、「実行中のUIは結果と
    同じUIにしてほしい」との要望への対応)。対象1件につき、Primary用の行1つと、
    Secondaryが判明・処理中・処理済みの場合はその行1つ(Addr列は空欄にして
    同じ対象であることを示す)を積む。
    """
    table = Table(title=title)
    table.add_column('Addr', style='cyan', no_wrap=True)
    table.add_column('Hostname', style='cyan')
    table.add_column('User', style='magenta')
    table.add_column('Login', style='green')
    table.add_column('Backup', style='green')
    table.add_column('TAC')
    table.add_column('Message', style='white')

    for r in results:
        login_txt, backup_txt = _STATUS_TEXT.get(r.status, ('', ''))
        tac_txt = _TAC_STATUS_TEXT.get(r.tac_status, '-') if r.tac_requested else '-'
        message_txt = _with_tac_message(r.message, r.tac_status, r.tac_message)
        table.add_row(r.target.addr, r.hostname, r.target.user, login_txt, backup_txt, tac_txt, message_txt)

        if r.secondary_requested and r.secondary_status:
            s_login_txt, s_backup_txt = _SECONDARY_STATUS_TEXT.get(r.secondary_status, ('', ''))
            s_tac_txt = _TAC_STATUS_TEXT.get(r.secondary_tac_status, '-') if r.tac_requested else '-'
            s_message_txt = _with_tac_message(r.secondary_message, r.secondary_tac_status, r.secondary_tac_message)
            table.add_row('', r.secondary_hostname, r.target.user, s_login_txt, s_backup_txt, s_tac_txt, s_message_txt)

    return table


def _with_tac_message(message: str, tac_status: str, tac_message: str) -> str:
    """Message列の文言に、TAC report取得失敗時のエラーを付記する(2026-09-16追加)。

    TAC reportの成否は独立の状態(`tac_status`)としてTAC列で示すため、
    Message列にはコンフィグバックアップ本体の結果(`message`、成功時は'success')は
    そのまま残し、TAC reportが失敗した場合のみ改行して`[TAC] ...`を追記する。
    """
    if tac_status == 'ng' and tac_message:
        tac_line = f'[red][TAC] {tac_message}[/]'
        return f'{message}\n{tac_line}' if message else tac_line
    return message


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='config_backup',
        description='NW機器のコンフィグバックアップツール(マルチベンダー対応)',
    )
    subparsers = parser.add_subparsers(dest='vendor', required=True)

    _add_fgt_subcommand(subparsers)
    _add_msw_subcommand(subparsers)
    _add_faz_subcommand(subparsers)

    return parser


def _add_fgt_subcommand(subparsers) -> None:
    msg = dedent("""\
        ~~~ FortiGate Config Backup ~~~
        ## 単体指定
        config_backup fgt -t 172.16.201.201 -u admin -p P@ssw0rd

        ## Primary + Secondary(HA)
        config_backup fgt -t 172.16.201.201 -u admin -p P@ssw0rd -s

        ## TAC report(execute tac report)も取得
        config_backup fgt -t 172.16.201.201 -u admin -p P@ssw0rd --tac

        ## CSVファイル指定
        config_backup fgt -f target.csv

        ### target csv format
        * [optional]header line: addr,user,password,alias,get_secondary,backup_dir,ssh_port,tac
        * data line: <addr>,<user>,<password>,[alias],[yes|no],[backup_dir],[ssh_port],[yes|no]
    """)

    p = subparsers.add_parser(
        'fgt',
        help='FortiGate本体のコンフィグバックアップ',
        description=msg,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    target_group = p.add_mutually_exclusive_group(required=True)
    target_group.add_argument('-t', '--target', help='ipaddr or hostname')
    target_group.add_argument('-f', '--file', help='target csv file')

    target_only = p.add_argument_group('Target Mode (-t 指定時に必要)')
    target_only.add_argument('-u', '--user', help='login user name')
    target_only.add_argument('-p', '--password', help='login password')
    target_only.add_argument('-n', '--name', default='', help='[optional] alias(ログファイル名等に使用)')
    target_only.add_argument('--ssh-port', type=int, default=22, help='SSH port (default: 22)')
    target_only.add_argument('-s', '--secondary', action='store_true',
                              help='Secondary(HA)ノードもバックアップする')
    target_only.add_argument('--tac', action='store_true',
                              help='TAC report("execute tac report")も取得する'
                                   '(-s指定時はSecondary側も取得する。生成に数分かかる場合あり)')

    p.add_argument('-d', '--directory', default='./booyaa_log/config',
                    help='backup directory (default: ./booyaa_log/config)')
    p.add_argument('--full', action='store_true',
                    help='"show full-configuration"(デフォルト値込みの全量)で取得する。指定なしは"show"(差分のみ)')
    p.add_argument('--timeout', type=float, default=60.0, help='コマンドタイムアウト(秒、default: 60)')
    p.add_argument('--tac-timeout', type=float, default=None,
                    help='TAC report取得のタイムアウト(秒、default: net_config側の既定値=600)')
    p.add_argument('--nomask', action='store_true', help='結果一覧表示でパスワードをマスクしない')
    p.add_argument('--debug-log', metavar='PATH',
                    help='scrapliのCLIセッション(送受信内容・プロンプトマッチング等)を'
                         'PATHにデバッグログとして出力する(Secondaryログインのタイムアウト等の'
                         '調査用。通常運用では指定不要)')

    p.set_defaults(handler=_handle_fgt)


def _add_msw_subcommand(subparsers) -> None:
    msg = dedent("""\
        ~~~ Managed Switch (FortiLink) Config Backup ~~~
        FortiGateにログインし、"execute switch-controller get-conn-status"で
        判明した配下のManaged Switchのうち、Authorized/Upのもののみバックアップ
        (・TAC report取得)する。それ以外(Discovered/Down等)はステータスのみ表示する。

        ## 単体指定(FortiGate 1台、配下のSwitchは自動検出)
        config_backup msw -t 172.16.201.201 -u admin -p P@ssw0rd

        ## Managed Switchのユーザー/パスワードを指定(既定: user=admin, password=FortiGateと同じ)
        config_backup msw -t 172.16.201.201 -u admin -p P@ssw0rd --msw-user admin --msw-password P@ssw0rd

        ## TAC report(FortiSwitchでは"diagnose debug report")も取得
        config_backup msw -t 172.16.201.201 -u admin -p P@ssw0rd --tac

        ## CSVファイル指定
        config_backup msw -f target.csv

        ### target csv format
        * [optional]header line: fgt_addr,fgt_user,fgt_password,alias,msw_user,msw_password,backup_dir,ssh_port,tac
        * data line: <fgt_addr>,<fgt_user>,<fgt_password>,[alias],[msw_user],[msw_password],[backup_dir],[ssh_port],[yes|no]

        ### backup directory (既定値)
        既定では"./booyaa_log/config/<FortiGateのhostname>_msw/"配下に保存する
        (-d/--directoryまたはCSVのbackup_dir列で明示指定した場合はそちらを優先)。
    """)

    p = subparsers.add_parser(
        'msw',
        help='Managed Switch(FortiLink)のコンフィグバックアップ',
        description=msg,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    target_group = p.add_mutually_exclusive_group(required=True)
    target_group.add_argument('-t', '--target', help='FortiGateのipaddr or hostname')
    target_group.add_argument('-f', '--file', help='target csv file')

    target_only = p.add_argument_group('Target Mode (-t 指定時に必要)')
    target_only.add_argument('-u', '--user', help='FortiGate login user name')
    target_only.add_argument('-p', '--password', help='FortiGate login password')
    target_only.add_argument('-n', '--name', default='', help='[optional] FortiGateのalias(ログファイル名等に使用)')
    target_only.add_argument('--ssh-port', type=int, default=22, help='FortiGate SSH port (default: 22)')
    target_only.add_argument('--msw-user', default='admin',
                              help='Managed Switchのログインユーザー名(既定: admin)')
    target_only.add_argument('--msw-password',
                              help='Managed Switchのログインパスワード(未指定時はFortiGateと同じパスワード)')
    target_only.add_argument('--tac', action='store_true',
                              help='TAC report("diagnose debug report")も取得する'
                                   '(対象は配下の全Authorized/Upスイッチ)')

    p.add_argument('-d', '--directory', default=None,
                    help='backup directory (既定: "./booyaa_log/config/<FortiGateのhostname>_msw")')
    p.add_argument('--full', action='store_true',
                    help='"show full-configuration"(デフォルト値込みの全量)で取得する。指定なしは"show"(差分のみ)')
    p.add_argument('--timeout', type=float, default=60.0, help='コマンドタイムアウト(秒、default: 60)')
    p.add_argument('--tac-timeout', type=float, default=None,
                    help='TAC report取得のタイムアウト(秒、default: net_config側の既定値=600)')
    p.add_argument('--nomask', action='store_true', help='結果一覧表示でパスワードをマスクしない')
    p.add_argument('--debug-log', metavar='PATH',
                    help='scrapliのCLIセッション(送受信内容・プロンプトマッチング等)を'
                         'PATHにデバッグログとして出力する(調査用。通常運用では指定不要)')

    p.set_defaults(handler=_handle_msw)


def _add_faz_subcommand(subparsers) -> None:
    msg = dedent(f"""\
        ~~~ FortiAnalyzer Backup (Config + System) ~~~
        コンフィグバックアップ("show")とシステム全体バックアップ
        ("execute backup all-settings")の両方を取得する(こうぢさん指定)。
        システムバックアップはFAZ自身がFTPクライアントとしてファイルを
        送りつけてくる方式のため、本ツール実行時に一時的な受信用FTPサーバを
        起動する(`common.ftp.SimpleFtpServer`)。

        ## 単体指定
        config_backup faz -t 172.16.201.210 -u admin -p P@ssw0rd

        ## TAC report("execute tac report")も取得
        config_backup faz -t 172.16.201.210 -u admin -p P@ssw0rd --tac

        ## FTPサーバのポート番号を変更(既定: {FTP_DEFAULT_PORT})
        config_backup faz -t 172.16.201.210 -u admin -p P@ssw0rd --ftp-port 2222

        ## CSVファイル指定
        config_backup faz -f target.csv

        ### target csv format
        * [optional]header line: addr,user,password,alias,config_password,ftp_user,ftp_password,ftp_port,backup_dir,ssh_port,tac
        * data line: <addr>,<user>,<password>,[alias],[config_password],[ftp_user],[ftp_password],[ftp_port],[backup_dir],[ssh_port],[yes|no]

        ### backup directory (既定値)
        既定ではFortiGate本体と同じ"./booyaa_log/config/"配下に保存する
        (-d/--directoryまたはCSVのbackup_dir列で明示指定した場合はそちらを優先)。
        ファイル名はFortiGate/MSWと異なり、alias指定の有無に関わらず常に
        ホスト名基準になる(こうぢさん指定)。

        ### FTPログイン情報について
        --ftp-user/--ftp-passwordを指定しない場合、対象ごとにランダムな
        使い捨ての認証情報を生成する(実装側の判断。固定運用したい場合は明示指定する)。
    """)

    p = subparsers.add_parser(
        'faz',
        help='FortiAnalyzerのコンフィグ+システムバックアップ',
        description=msg,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    target_group = p.add_mutually_exclusive_group(required=True)
    target_group.add_argument('-t', '--target', help='FortiAnalyzerのipaddr or hostname')
    target_group.add_argument('-f', '--file', help='target csv file')

    target_only = p.add_argument_group('Target Mode (-t 指定時に必要)')
    target_only.add_argument('-u', '--user', help='FortiAnalyzer login user name')
    target_only.add_argument('-p', '--password', help='FortiAnalyzer login password')
    target_only.add_argument('-n', '--name', default='', help='[optional] alias(結果表示にのみ使用、ファイル名には使わない)')
    target_only.add_argument('--ssh-port', type=int, default=22, help='FortiAnalyzer SSH port (default: 22)')
    target_only.add_argument('--config-password',
                              help='システムバックアップファイルの暗号化パスワード'
                                   '(未指定時はログインパスワードと同じ値を使う)')
    target_only.add_argument('--ftp-user', default='',
                              help='一時FTPサーバのログインユーザー名(未指定時はランダム生成)')
    target_only.add_argument('--ftp-password', default='',
                              help='一時FTPサーバのログインパスワード(未指定時はランダム生成)')
    target_only.add_argument('--ftp-port', type=int, default=FTP_DEFAULT_PORT,
                              help=f'一時FTPサーバのポート番号 (default: {FTP_DEFAULT_PORT})')
    target_only.add_argument('--tac', action='store_true',
                              help='TAC report("execute tac report")も取得する')

    p.add_argument('-d', '--directory', default=None,
                    help='backup directory (既定: "./booyaa_log/config")')
    p.add_argument('--timeout', type=float, default=60.0,
                    help='ログイン/コンフィグ取得/TAC report以外のコマンドタイムアウト(秒、default: 60)')
    p.add_argument('--sys-backup-timeout', type=float, default=None,
                    help='システムバックアップ("execute backup all-settings")取得のタイムアウト'
                         '(秒、非同期の転送完了まで含む。default: net_config側の既定値=1800)')
    p.add_argument('--tac-timeout', type=float, default=None,
                    help='TAC report取得のタイムアウト(秒、default: net_config側の既定値=600)')
    p.add_argument('--nomask', action='store_true', help='結果一覧表示でパスワードをマスクしない')
    p.add_argument('--debug-log', metavar='PATH',
                    help='scrapliのCLIセッション(送受信内容・プロンプトマッチング等)を'
                         'PATHにデバッグログとして出力する(調査用。通常運用では指定不要)')

    p.set_defaults(handler=_handle_faz)


def _handle_fgt(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.target and (not args.user or not args.password):
        parser.error('-u/--user と -p/--password は -t/--target 指定時に必須です。')

    if args.debug_log:
        # scrapliのchannel/driver/transportログ(送受信内容・プロンプトマッチング等)を
        # ファイルへ出力する。実機でのみ再現する接続系の不具合(例: Secondaryログインの
        # タイムアウト)調査用(2026-09-16対応)。
        from net_config.debug import enable_debug_logging
        enable_debug_logging(args.debug_log)

    if args.target:
        targets = [FortigateTarget(
            addr=args.target,
            user=args.user,
            password=args.password,
            alias=args.name,
            ssh_port=args.ssh_port,
            get_secondary=args.secondary,
            tac=args.tac,
        )]
    else:
        try:
            targets = load_fortigate_list_csv(args.file)
        except FileNotFoundError as e:
            parser.error(str(e))
            return 2

    if not targets:
        parser.error(f'no valid target found in {args.file}')
        return 2

    results = _run_with_live_progress(
        targets,
        backup_dir=args.directory,
        full=args.full,
        timeout=args.timeout,
        tac_timeout=args.tac_timeout,
    )

    _print_saved_paths(results)

    return 0 if all(r.ok for r in results) else 1


def _run_with_live_progress(targets: list[FortigateTarget], backup_dir: str, full: bool,
                             timeout: float, tac_timeout: float | None = None) -> list[BackupResult]:
    """`run_fortigate_backup()`をrich.Liveの進捗表示付きで実行する。

    実行自体は従来通り逐次(直列)だが、`_build_table()`で対象ごとの現在状態
    (接続中/ログインOK/バックアップ中/Secondary処理中、等)をリアルタイムに
    反映したテーブルを表示する(「コマンド実行から完了までTUIが表示されない」
    という問題(2026-09-16報告)への対応)。

    `transient=False`にしているため、Live表示は完了時に消えずそのまま画面に
    残る。つまり実行中に更新され続けていたテーブルが、最後の更新のまま
    「結果表示」を兼ねる形になり、実行中と結果とで見た目が変わらない
    (2026-09-16、「実行中のUIは結果と同じUIにしてほしい」との要望への対応)。
    """
    console = Console()
    state: dict[str, BackupResult] = {
        t.addr: BackupResult(target=t, secondary_requested=t.get_secondary,
                              tac_requested=t.tac, status='connecting')
        for t in targets
    }

    def render() -> Table:
        return _build_table([state[t.addr] for t in targets], title='FortiGate Config Backup')

    def on_progress(result: BackupResult) -> None:
        state[result.target.addr] = result
        live.update(render())

    with Live(render(), console=console, refresh_per_second=8, transient=False) as live:
        results = run_fortigate_backup(
            targets,
            backup_dir=backup_dir,
            full=full,
            timeout=timeout,
            tac_timeout=tac_timeout,
            progress_callback=on_progress,
        )
        live.update(_build_table(results, title='FortiGate Config Backup'))

    return results


def _print_saved_paths(results: list[BackupResult]) -> None:
    console = Console()
    for r in results:
        if r.saved_path:
            console.print(f'[green]saved[/]: {r.saved_path}')
        if r.tac_saved_path:
            console.print(f'[green]saved[/]: {r.tac_saved_path}')
        if r.secondary_saved_path:
            console.print(f'[green]saved[/]: {r.secondary_saved_path}')
        if r.secondary_tac_saved_path:
            console.print(f'[green]saved[/]: {r.secondary_tac_saved_path}')


# MswBackupResult.status(FGT接続部分)を、テーブルの"Login"列の表示文言に変換する。
# 'listing_ng'/'done'はいずれも"FGTへのログイン自体"は成功しているのでOK表示にする
# (スイッチ一覧取得の失敗はFGT行のMessage列に表示する)。
_MSW_FGT_STATUS_TEXT: dict[str, str] = {
    'connecting': '[yellow]Connecting...[/]',
    'login_ng': '[red]NG[/]',
    'login_ok': '[green]OK[/]',
    'listing_ng': '[green]OK[/]',
    'done': '[green]OK[/]',
}

# MswBackupItem.status(個々のスイッチ)を、テーブルの(Login列, Backup列)の
# 表示文言に変換する。`config_backup.cli._STATUS_TEXT`(FGT用)と対応する構成。
# 'skipped'(対象外)はLogin列にオレンジで"Skip"と表示する(こうぢさん指定、2026-09-22追加)。
_MSW_ITEM_STATUS_TEXT: dict[str, tuple[str, str]] = {
    'skipped': ('[orange3]Skip[/]', '-'),
    'connecting': ('[yellow]Connecting...[/]', ''),
    'login_ok': ('[green]OK[/]', '[yellow]Backing up...[/]'),
    'login_ng': ('[red]NG[/]', ''),
    'backup_ok': ('[green]OK[/]', '[green]OK[/]'),
    'backup_ng': ('[green]OK[/]', '[red]NG[/]'),
}


def _build_msw_table(results: list[MswBackupResult], title: str) -> Table:
    """MSWバックアップ結果一覧のテーブルを組み立てる(`_build_table()`のMSW版)。

    対象のFortiGate 1台につき、FGT接続状態を示す行を1つ、配下の各Managed
    Switch(発見できた場合)につき1行(FGT Addr列は空欄にして同じ対象である
    ことを示す)を積む。Authorized/Up以外のスイッチも行として表示するが、
    Login/Backup列は'-'(対象外)にし、Message列にステータスのみ表示する
    (こうぢさん指定: 「それ以外は、CLIのメッセージにステータスのみ表示」)。
    """
    table = Table(title=title)
    table.add_column('FGT Addr', style='cyan', no_wrap=True)
    table.add_column('FGT Hostname', style='cyan')
    table.add_column('Switch Serial', style='magenta')
    table.add_column('Switch Hostname', style='magenta')
    table.add_column('Switch Addr', style='magenta')
    # Status/Messageは列幅の都合で省略(...)表示されないよう、折り返し(fold)にする
    # (こうぢさん指定、2026-09-22追加。no_wrap=Falseの既定のままだと画面幅次第で
    # 末尾が"..."に切り詰められてしまうことがあったため)。
    table.add_column('Status', no_wrap=False, overflow='fold')
    table.add_column('Login', style='green')
    table.add_column('Backup', style='green')
    table.add_column('TAC')
    table.add_column('Message', style='white', no_wrap=False, overflow='fold')

    for r in results:
        fgt_login_txt = _MSW_FGT_STATUS_TEXT.get(r.status, '')
        fgt_message = r.message if r.status in ('login_ng', 'listing_ng') else ''
        table.add_row(r.target.fgt_addr, r.fgt_hostname, '', '', '', '', fgt_login_txt, '', '', fgt_message)

        for item in r.items:
            login_txt, backup_txt = _MSW_ITEM_STATUS_TEXT.get(item.status, ('', ''))
            if item.eligible:
                tac_txt = _TAC_STATUS_TEXT.get(item.tac_status, '-') if item.tac_requested else '-'
            else:
                tac_txt = '-'
            message_txt = _with_tac_message(item.message, item.tac_status, item.tac_message)
            table.add_row('', '', item.serial, item.hostname, item.addr,
                          item.display_status, login_txt, backup_txt, tac_txt, message_txt)

    return table


def _print_msw_saved_paths(results: list[MswBackupResult]) -> None:
    console = Console()
    for r in results:
        for item in r.items:
            if item.saved_path:
                console.print(f'[green]saved[/]: {item.saved_path}')
            if item.tac_saved_path:
                console.print(f'[green]saved[/]: {item.tac_saved_path}')


def _handle_msw(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.target and (not args.user or not args.password):
        parser.error('-u/--user と -p/--password は -t/--target 指定時に必須です。')

    if args.debug_log:
        from net_config.debug import enable_debug_logging
        enable_debug_logging(args.debug_log)

    if args.target:
        targets = [MswTarget(
            fgt_addr=args.target,
            fgt_user=args.user,
            fgt_password=args.password,
            fgt_alias=args.name,
            fgt_ssh_port=args.ssh_port,
            msw_user=args.msw_user,
            msw_password=args.msw_password or '',
            tac=args.tac,
        )]
    else:
        try:
            targets = load_msw_list_csv(args.file)
        except FileNotFoundError as e:
            parser.error(str(e))
            return 2

    if not targets:
        parser.error(f'no valid target found in {args.file}')
        return 2

    results = _run_msw_with_live_progress(
        targets,
        backup_dir=args.directory,
        full=args.full,
        timeout=args.timeout,
        tac_timeout=args.tac_timeout,
    )

    _print_msw_saved_paths(results)

    return 0 if all(r.ok for r in results) else 1


def _run_msw_with_live_progress(targets: list[MswTarget], backup_dir: str | None, full: bool,
                                 timeout: float, tac_timeout: float | None = None) -> list[MswBackupResult]:
    """`run_msw_backup()`をrich.Liveの進捗表示付きで実行する(`_run_with_live_progress()`のMSW版)。"""
    console = Console()
    state: dict[str, MswBackupResult] = {
        t.fgt_addr: MswBackupResult(target=t, status='connecting')
        for t in targets
    }

    def render() -> Table:
        return _build_msw_table([state[t.fgt_addr] for t in targets], title='Managed Switch Config Backup')

    def on_progress(result: MswBackupResult) -> None:
        state[result.target.fgt_addr] = result
        live.update(render())

    with Live(render(), console=console, refresh_per_second=8, transient=False) as live:
        results = run_msw_backup(
            targets,
            backup_dir=backup_dir,
            full=full,
            timeout=timeout,
            tac_timeout=tac_timeout,
            progress_callback=on_progress,
        )
        live.update(_build_msw_table(results, title='Managed Switch Config Backup'))

    return results


# FazBackupResult.status(ログイン)を、テーブルの"Login"列の表示文言に変換する。
_FAZ_STATUS_TEXT: dict[str, str] = {
    'connecting': '[yellow]Connecting...[/]',
    'login_ng': '[red]NG[/]',
    'login_ok': '[green]OK[/]',
    'done': '[green]OK[/]',
}

# FazBackupResult.config_status/sys_status(コンフィグ/システムバックアップ)を、
# テーブルの各列の表示文言に変換する。未着手('')は`_faz_stage_text()`側で
# 固定の'-'にする。
_FAZ_STAGE_STATUS_TEXT: dict[str, str] = {
    'backing_up': '[yellow]Backing up...[/]',
    'ok': '[green]OK[/]',
    'ng': '[red]NG[/]',
}


def _faz_stage_text(status: str) -> str:
    if not status:
        return '-'
    return _FAZ_STAGE_STATUS_TEXT.get(status, status)


def _faz_message(r: FazBackupResult) -> str:
    """Message列の文言を組み立てる(コンフィグバックアップのエラー本体+システム/TACの補記)。

    `message`にはコンフィグバックアップ("show")側の結果(失敗時のみエラー文言、
    成功時は空)を入れる。システムバックアップ・TAC reportは専用の列(System/TAC)で
    成否を示すため、それらが失敗した場合のみ`[System] ...`/`[TAC] ...`という
    形でMessage列に補記する(`config_backup.cli._with_tac_message()`と同じ考え方)。
    """
    parts = []
    if r.message:
        parts.append(r.message)
    if r.sys_status == 'ng' and r.sys_message:
        parts.append(f'[red][System] {r.sys_message}[/]')
    if r.tac_requested and r.tac_status == 'ng' and r.tac_message:
        parts.append(f'[red][TAC] {r.tac_message}[/]')
    return '\n'.join(parts)


def _build_faz_table(results: list[FazBackupResult], title: str) -> Table:
    """FAZバックアップ結果一覧のテーブルを組み立てる(`_build_table()`のFAZ版)。"""
    table = Table(title=title)
    table.add_column('Addr', style='cyan', no_wrap=True)
    table.add_column('Hostname', style='cyan')
    table.add_column('User', style='magenta')
    table.add_column('Login', style='green')
    table.add_column('Config', style='green')
    table.add_column('System', style='green')
    table.add_column('TAC')
    table.add_column('Message', style='white', no_wrap=False, overflow='fold')

    for r in results:
        login_txt = _FAZ_STATUS_TEXT.get(r.status, '')
        config_txt = _faz_stage_text(r.config_status)
        sys_txt = _faz_stage_text(r.sys_status)
        tac_txt = _TAC_STATUS_TEXT.get(r.tac_status, '-') if r.tac_requested else '-'
        message_txt = _faz_message(r)
        table.add_row(r.target.addr, r.hostname, r.target.user, login_txt,
                      config_txt, sys_txt, tac_txt, message_txt)

    return table


def _print_faz_saved_paths(results: list[FazBackupResult]) -> None:
    console = Console()
    for r in results:
        if r.config_saved_path:
            console.print(f'[green]saved[/]: {r.config_saved_path}')
        if r.sys_saved_path:
            console.print(f'[green]saved[/]: {r.sys_saved_path}')
        if r.tac_saved_path:
            console.print(f'[green]saved[/]: {r.tac_saved_path}')


def _handle_faz(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.target and (not args.user or not args.password):
        parser.error('-u/--user と -p/--password は -t/--target 指定時に必須です。')

    if args.debug_log:
        from net_config.debug import enable_debug_logging
        enable_debug_logging(args.debug_log)

    if args.target:
        targets = [FazTarget(
            addr=args.target,
            user=args.user,
            password=args.password,
            alias=args.name,
            ssh_port=args.ssh_port,
            config_password=args.config_password or '',
            ftp_user=args.ftp_user,
            ftp_password=args.ftp_password,
            ftp_port=args.ftp_port,
            tac=args.tac,
        )]
    else:
        try:
            targets = load_faz_list_csv(args.file)
        except FileNotFoundError as e:
            parser.error(str(e))
            return 2

    if not targets:
        parser.error(f'no valid target found in {args.file}')
        return 2

    results = _run_faz_with_live_progress(
        targets,
        backup_dir=args.directory,
        timeout=args.timeout,
        sys_backup_timeout=args.sys_backup_timeout,
        tac_timeout=args.tac_timeout,
    )

    _print_faz_saved_paths(results)

    return 0 if all(r.ok for r in results) else 1


def _run_faz_with_live_progress(targets: list[FazTarget], backup_dir: str | None, timeout: float,
                                 sys_backup_timeout: float | None = None,
                                 tac_timeout: float | None = None) -> list[FazBackupResult]:
    """`run_faz_backup()`をrich.Liveの進捗表示付きで実行する(`_run_with_live_progress()`のFAZ版)。"""
    console = Console()
    state: dict[str, FazBackupResult] = {
        t.addr: FazBackupResult(target=t, tac_requested=t.tac, status='connecting')
        for t in targets
    }

    def render() -> Table:
        return _build_faz_table([state[t.addr] for t in targets], title='FortiAnalyzer Backup')

    def on_progress(result: FazBackupResult) -> None:
        state[result.target.addr] = result
        live.update(render())

    with Live(render(), console=console, refresh_per_second=8, transient=False) as live:
        results = run_faz_backup(
            targets,
            backup_dir=backup_dir,
            timeout=timeout,
            sys_backup_timeout=sys_backup_timeout,
            tac_timeout=tac_timeout,
            progress_callback=on_progress,
        )
        live.update(_build_faz_table(results, title='FortiAnalyzer Backup'))

    return results


def main(argv: list[str] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    handler = getattr(args, 'handler', None)
    if handler is None:
        parser.error(f'unsupported vendor: {args.vendor}')
        return 2

    try:
        return handler(args, parser)
    except KeyboardInterrupt:
        # 接続不可な対象への接続待ち等でCtrl+Cされた場合、tracebackを出さずに
        # ユーザ切断として終了する(2026-09-16報告への対応)。
        # 慣例的な終了コード128+SIGINT(2) = 130を返す。
        Console().print('\n[yellow]Interrupted by user.[/]')
        return 130


if __name__ == '__main__':
    sys.exit(main())
