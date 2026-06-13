import importlib
import sys
import types


class FakeBarmanCli:
    def __call__(self, *args, **kwargs):
        if '-v' in args:
            return '3.19.1\n'
        if 'diagnose' in args:
            return '{"global": {"config": {"barman_home": "/var/lib/barman"}, "system_info": {"barman_ver": "3.19.1"}}}'
        return '{"pgsql-test": {}}'


fake_sh = types.ModuleType('sh')
fake_sh.barman = FakeBarmanCli()
sys.modules.setdefault('sh', fake_sh)

exporter = importlib.import_module('barman_exporter.barman_exporter')


class DummyBarman(exporter.Barman):
    def __init__(self):
        self._diagnose = {
            "global": {
                "config": {"barman_home": "/var/lib/barman"},
                "system_info": {"barman_ver": "3.19.1"},
            }
        }

    def diagnose(self):
        return self._diagnose


def test_barman_version_and_home_are_read_from_diagnose():
    barman = DummyBarman()

    assert barman.version_value() == "3.19.1"
    assert barman.home_directory() == "/var/lib/barman"


def test_collect_adds_barman_home_filesystem_metrics(monkeypatch):
    class FakeStatvfs(types.SimpleNamespace):
        f_frsize = 4096
        f_blocks = 1000
        f_bavail = 200
        f_bfree = 300
        f_bsize = 4096
        f_files = 5000
        f_ffree = 4000

    collector = exporter.BarmanCollector(DummyBarman(), ["all"])

    monkeypatch.setattr(exporter.os, "statvfs", lambda path: FakeStatvfs())

    collector.collect_barman_home_filesystem_metrics("/var/lib/barman")

    family = collector.collectors["barman_home_filesystem_size_bytes"]
    assert family.samples
    assert family.samples[0].value == 1000 * 4096

    family = collector.collectors["barman_home_free_bytes"]
    assert family.samples[0].value == 300 * 4096

    family = collector.collectors["barman_home_avail_bytes"]
    assert family.samples[0].value == 200 * 4096

    family = collector.collectors["barman_home_files"]
    assert family.samples[0].value == 5000

    family = collector.collectors["barman_home_free"]
    assert family.samples[0].value == 4000
