import pytest

from config_backup.fortigate import load_fortigate_list_csv


def _write_csv(tmp_path, content):
    p = tmp_path / 'target.csv'
    p.write_text(content, encoding='utf-8')
    return p


def test_load_with_header(tmp_path):
    content = (
        'addr,user,password,alias,get_secondary,backup_dir,ssh_port\n'
        '172.16.201.201,admin,P@ssw0rd,Lab-FG01,yes,,22\n'
        '192.0.2.1,nw_admin,nw_admin_password\n'
    )
    csv_path = _write_csv(tmp_path, content)
    targets = load_fortigate_list_csv(csv_path)

    assert len(targets) == 2
    assert targets[0].addr == '172.16.201.201'
    assert targets[0].alias == 'Lab-FG01'
    assert targets[0].get_secondary is True
    assert targets[0].ssh_port == 22

    assert targets[1].addr == '192.0.2.1'
    assert targets[1].get_secondary is False
    assert targets[1].ssh_port == 22  # 未指定時のデフォルト


def test_load_without_header(tmp_path):
    content = '172.16.201.201,admin,P@ssw0rd\n'
    csv_path = _write_csv(tmp_path, content)
    targets = load_fortigate_list_csv(csv_path)

    assert len(targets) == 1
    assert targets[0].addr == '172.16.201.201'
    assert targets[0].user == 'admin'


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_fortigate_list_csv(tmp_path / 'does_not_exist.csv')


def test_blank_lines_are_skipped(tmp_path):
    content = '172.16.201.201,admin,P@ssw0rd\n\n192.0.2.1,admin,P@ssw0rd\n'
    csv_path = _write_csv(tmp_path, content)
    targets = load_fortigate_list_csv(csv_path)
    assert len(targets) == 2
