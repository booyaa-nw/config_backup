"""config_backup: NW機器のコンフィグバックアップツール(マルチベンダー対応)。

現時点では`fortigate`サブコマンドのみ実装。将来Alaxala等を追加する際は、
`build_parser()`に新しいサブコマンドを追加していく想定
(既存の`fortigate`サブコマンドの引数には影響しない)。

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

from config_backup.fortigate import (
    BackupResult,
    FortigateTarget,
    load_fortigate_list_csv,
    run_fortigate_backup,
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
        description='NW機器のコンフィグバックアップツール(マルチベンダー対応、当面はFortiGateのみ)',
    )
    subparsers = parser.add_subparsers(dest='vendor', required=True)

    _add_fortigate_subcommand(subparsers)

    return parser


def _add_fortigate_subcommand(subparsers) -> None:
    msg = dedent("""\
        ~~~ FortiGate Config Backup ~~~
        ## 単体指定
        config_backup fortigate -t 172.16.201.201 -u admin -p P@ssw0rd

        ## Primary + Secondary(HA)
        config_backup fortigate -t 172.16.201.201 -u admin -p P@ssw0rd -s

        ## TAC report(execute tac report)も取得
        config_backup fortigate -t 172.16.201.201 -u admin -p P@ssw0rd --tac

        ## CSVファイル指定
        config_backup fortigate -f target.csv

        ### target csv format
        * [optional]header line: addr,user,password,alias,get_secondary,backup_dir,ssh_port,tac
        * data line: <addr>,<user>,<password>,[alias],[yes|no],[backup_dir],[ssh_port],[yes|no]
    """)

    p = subparsers.add_parser(
        'fortigate',
        help='FortiGateのコンフィグバックアップ',
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

    p.set_defaults(handler=_handle_fortigate)


def _handle_fortigate(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
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
