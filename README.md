# config_backup

NW機器のコンフィグバックアップツール。マルチベンダー化を見据え、機種ごとの
個別実行スクリプト(旧`fgt_backup.py`/`msw_backup.py`)を統合し、サブコマンドで
対象を指定する構成にリニューアルした。現時点では`fortigate`サブコマンドのみ
実装（MSW・他ベンダーは今後のフェーズ）。

機器への実際のCLI操作(SSH接続・コマンド実行・出力パース)は`net_config`
(`net_config.ftnt.fgt.cli`)に委譲し、本ツールは「対象一覧を読み込み、
バックアップを実行し、ファイルに保存し、結果を表示する」という
オーケストレーションに専念する薄い層。

## Install

`C:\opt\booyaa`のuv workspace内のツールとして、`scripts/new-tool.ps1`で
雛形作成後、`net_config`とあわせて配置する想定
(詳細はClaude Projectの`architecture/config-backup-design.md`を参照)。

## Usage

```
# 単体指定
config_backup fortigate -t 172.16.201.201 -u admin -p P@ssw0rd

# Primary + Secondary(HA)
config_backup fortigate -t 172.16.201.201 -u admin -p P@ssw0rd -s

# CSVファイルで複数指定
config_backup fortigate -f target.csv
```

CSVフォーマット(ヘッダ行省略可):
```
addr,user,password,alias,get_secondary,backup_dir,ssh_port
172.16.201.201,admin,P@ssw0rd,Lab-FG01,yes
192.0.2.1,nw_admin,nw_admin_password
```

旧`fgt_backup.py`のCSV形式から`https_port`列を削除している(API操作を行わない
本実装では不要なため)。

## Alaxala AX3000シリーズ(`ax`サブコマンド、2026-09-23追加)

1台につき、コンフィグ(`show configuration`)・システム(`backup ftp`)・
TAC report(`show tech-support`、`--tac`指定時のみ)を取得する。

```
# 単体指定(enableパスワード未指定時は -p と同じ値。enableパスワード無しの装置では使われない)
config_backup ax -t 172.16.201.205 -u nwadmin -p P@ssw0rd [-e <enable_password>]

# システムバックアップからOSを除外 + TAC report取得
config_backup ax -t 172.16.201.205 -u nwadmin -p P@ssw0rd --no-software --tac

# 複数台(カンマ区切り、またはCSV)。最大5台同時実行・1台ごとに1秒ずつずらして開始
config_backup ax -t 172.16.201.205,172.16.201.206 -u nwadmin -p P@ssw0rd
config_backup ax -f target.csv
```

CSVフォーマット(ヘッダ行省略可):
```
addr,user,password,enable_password,alias,backup_dir,ssh_port,tac,no_software
172.16.201.205,nwadmin,P@ssw0rd,,,,,yes,no
```

* ファイル名はFAZと同様ホスト名基準: `<hostname>_<version>_<ts>.conf` /
  `<hostname>_<version>_systembackup[_noos]_<ts>.dat` / `<hostname>_<version>_tacreport_<ts>.log`
* `backup ftp`はポート番号を指定できないため、実行中だけ受信用FTPサーバを**21番**で
  起動し全台で共有する(21番が使用中・bind不可の場合はシステムバックアップのみNG)。
  初回実行時にWindowsファイアウォールの許可ダイアログが出る場合がある。

## 今回のスコープ外(次フェーズ以降)

* MSW(FortiLink配下Managed Switch)のバックアップ(`net_config.ftnt.msw.cli`実装後)
* 他ベンダー(Alaxala AX3660S等)のサブコマンド追加
* 並列実行・進捗のリアルタイム表示(`rich.Live`。現状は逐次実行+最後にサマリ表示)
* APIの代替としてのヘッドレスブラウザ経由バックアップ
