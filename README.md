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

## 今回のスコープ外(次フェーズ以降)

* MSW(FortiLink配下Managed Switch)のバックアップ(`net_config.ftnt.msw.cli`実装後)
* 他ベンダー(Alaxala AX3660S等)のサブコマンド追加
* 並列実行・進捗のリアルタイム表示(`rich.Live`。現状は逐次実行+最後にサマリ表示)
* APIの代替としてのヘッドレスブラウザ経由バックアップ
