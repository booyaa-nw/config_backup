from config_backup.save import save_config


def test_save_creates_file_and_directory(tmp_path):
    export_dir = tmp_path / 'nested' / 'fg_config'
    let = save_config(content='config content here', hostname='LABFG01', export_dir=export_dir,
                       alias='Lab-FG01', version='7.4.5')

    assert let['code'] == 0
    saved_path = let['output']
    assert saved_path.startswith(str(export_dir))
    assert 'Lab-FG01' in saved_path
    assert '7.4.5' in saved_path

    from pathlib import Path
    assert Path(saved_path).read_text(encoding='utf-8') == 'config content here'


def test_save_falls_back_to_hostname_without_alias(tmp_path):
    let = save_config(content='x', hostname='LABFG01', export_dir=tmp_path)
    assert 'LABFG01' in let['output']
