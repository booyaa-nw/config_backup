"""バックアップ結果をファイルに保存する処理。

`save_config()`(テキスト保存、旧実装から踏襲)に加え、`save_binary_file()`
(バイナリファイルのコピー保存)を提供する。後者はFortiAnalyzerのシステム
バックアップ(`execute backup all-settings ftp ...`、`config_backup.faz`参照)
のように、機器側がFTP等で送りつけてきた既存のバイナリファイルを、他の
バックアップ種別と同じ命名規則で最終的な保存先ディレクトリへコピーする
用途向け(2026-09-22追加)。ファイル名の組み立てロジック(`_build_filename()`)は
両者で共通化している。
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path


def _build_filename(hostname: str, alias: str, version: str, suffix: str, ext: str,
                     timestamp: datetime | None) -> str:
    name = alias or hostname or 'unknown'
    ts = (timestamp or datetime.now()).strftime('%Y%m%d_%H%M%S')
    version_part = f'_{version}' if version else ''
    suffix_part = f'_{suffix}' if suffix else ''
    return f'{name}{version_part}{suffix_part}_{ts}.{ext}'


def _ensure_dir(export_dir: str | Path) -> tuple[Path | None, dict | None]:
    export_dir = Path(export_dir)
    try:
        export_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return None, {'code': 1, 'msg': f'[Error] Failed to create directory {export_dir}: {e}', 'output': ''}
    return export_dir, None


def save_config(content: str, hostname: str, export_dir: str | Path, alias: str = '',
                 version: str = '', encoding: str = 'utf-8', timestamp: datetime = None,
                 suffix: str = '', ext: str = 'conf') -> dict:
    """テキスト内容をファイルに保存する(設定バックアップ・TAC report共用)。

    ファイル名は `<alias または hostname>[_<version>][_<suffix>]_<YYYYmmdd_HHMMSS>.<ext>` とする
    (`version`/`suffix`は指定時のみ挿入される)。

    Returns:
        {'code', 'msg', 'output'}。成功時は`output`に保存先パスの文字列を入れる。
    """
    export_dir, err = _ensure_dir(export_dir)
    if err is not None:
        return err

    filename = _build_filename(hostname, alias, version, suffix, ext, timestamp)
    file_path = export_dir / filename

    try:
        file_path.write_text(content, encoding=encoding)
    except OSError as e:
        return {'code': 1, 'msg': f'[Error] Failed to write {file_path}: {e}', 'output': ''}

    return {'code': 0, 'msg': f'Saved to {file_path}', 'output': str(file_path)}


def save_binary_file(src_path: str | Path, hostname: str, export_dir: str | Path, alias: str = '',
                      version: str = '', timestamp: datetime = None,
                      suffix: str = '', ext: str = 'dat') -> dict:
    """既存のバイナリファイルを、`save_config()`と同じ命名規則で保存先ディレクトリへコピーする。

    FortiAnalyzerのシステムバックアップ(`execute backup all-settings ftp ...`)のように、
    機器側がFTPで送りつけてきたファイル(一時的な受信用ディレクトリに置かれている)を、
    他のバックアップファイルと同じ命名規則の下、最終的な保存先ディレクトリへ配置し直す
    ために使う。`save_config()`と異なり内容はテキストとして扱わず、バイト列のまま
    `shutil.copyfile()`でコピーする(コピー元が読めなくても壊れないよう、コピー先への
    書き込みが失敗した場合も含め、コピー元ファイル自体は変更・削除しない)。

    Returns:
        {'code', 'msg', 'output'}。成功時は`output`に保存先パスの文字列を入れる。
    """
    export_dir, err = _ensure_dir(export_dir)
    if err is not None:
        return err

    src_path = Path(src_path)
    if not src_path.is_file():
        return {'code': 1, 'msg': f'[Error] Source file not found: {src_path}', 'output': ''}

    filename = _build_filename(hostname, alias, version, suffix, ext, timestamp)
    file_path = export_dir / filename

    try:
        shutil.copyfile(src_path, file_path)
    except OSError as e:
        return {'code': 1, 'msg': f'[Error] Failed to copy {src_path} to {file_path}: {e}', 'output': ''}

    return {'code': 0, 'msg': f'Saved to {file_path}', 'output': str(file_path)}
