"""バックアップ結果をファイルに保存する処理。

旧実装は`booyaa.common.export.save_file.save_config`(現行`common`ツールには
未移植)を使っていたが、今回は「net_configは機器操作に専念し、ファイル出力
ポリシーはconfig_backup側が持つ」という責務分割で、config_backup内に
自己完結させた。複数ツールで同じ保存ロジックが必要になった時点で、
`common`または`net_config`への切り出しを検討する。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path


def save_config(content: str, hostname: str, export_dir: str | Path, alias: str = '',
                 version: str = '', encoding: str = 'utf-8', timestamp: datetime = None,
                 suffix: str = '', ext: str = 'conf') -> dict:
    """内容をファイルに保存する(設定バックアップ・TAC report共用)。

    ファイル名は `<alias または hostname>[_<version>][_<suffix>]_<YYYYmmdd_HHMMSS>.<ext>` とする
    (`version`/`suffix`は指定時のみ挿入される)。既定の`conf`(コンフィグバックアップ用)に
    加え、TAC report保存時は`suffix='tacreport', ext='log'`を指定する
    (2026-09-16、`execute tac report`取得機能追加時にこうぢ氏よりファイル名指定あり:
    `<hostname>_tacreport_<YYYYmmdd_HHMMSS>.log`。TAC reportにはversionを含めない)。

    Returns:
        {'code', 'msg', 'output'}。成功時は`output`に保存先パスの文字列を入れる。
    """
    let = {'code': 0, 'msg': '', 'output': ''}

    export_dir = Path(export_dir)
    try:
        export_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {'code': 1, 'msg': f'[Error] Failed to create directory {export_dir}: {e}', 'output': ''}

    name = alias or hostname or 'unknown'
    ts = (timestamp or datetime.now()).strftime('%Y%m%d_%H%M%S')
    version_part = f'_{version}' if version else ''
    suffix_part = f'_{suffix}' if suffix else ''
    filename = f'{name}{version_part}{suffix_part}_{ts}.{ext}'
    file_path = export_dir / filename

    try:
        file_path.write_text(content, encoding=encoding)
    except OSError as e:
        return {'code': 1, 'msg': f'[Error] Failed to write {file_path}: {e}', 'output': ''}

    let['msg'] = f'Saved to {file_path}'
    let['output'] = str(file_path)
    return let
