import os
import sys
import argparse
import time
import json
import shutil
import threading
import prometheus_client
from prometheus_client import core
from datetime import datetime

BARMAN_EXPORTER_VERSION = '1.0.10'
sys.tracebacklimit = 0

try:
    from sh import barman as barman_cli
except ImportError as e:
    raise ImportError('ERROR: Barman binary not found!') from e


class Barman:
    def __init__(self):
        self.check_barman_version()

    def diagnose(self):
        return self.cli('diagnose')

    def home_directory(self):
        try:
            return self.diagnose()['global']['config']['barman_home']
        except Exception:
            return '/var/lib/barman'

    def version_value(self):
        try:
            return self.diagnose()['global']['system_info']['barman_ver']
        except Exception:
            return self.version()

    def check_barman_version(self):
        barman_version = tuple(int(v) for v in self.version().split('.'))
        if barman_version < (2, 9):
            raise ValueError("Barman version 2.9+ required")

    @staticmethod
    def cli(*args, **kwargs):
        output = barman_cli('-f', 'json', *args, **kwargs)
        output = json.loads(str(output))
        return output

    def version(self):
        version = barman_cli('-v', _err_to_out=True).split()
        return version[0]

    def servers(self):
        servers = self.cli('list-server')
        return list(servers.keys())

    def server_status(self, server_name):
        status = self.cli('status', server_name)
        status = {k: v['message'] for k, v in status[server_name].items()}
        return status

    def server_check(self, server_name):
        check = self.cli('check', server_name, _ok_code=[0, 1])
        check = {k: 1 if v['status'] == "OK" else 0 for k,
                 v in check[server_name].items()}
        return check

    def list_backup(self, server_name):
        backups = self.cli('list-backup', server_name)
        backups_done = [backup for backup in backups[server_name]
                        if backup['status'] == 'DONE']
        backups_failed = [
            backup for backup in backups[server_name] if backup['status'] == 'FAILED']
        return backups_done, backups_failed

    def show_backup(self, server_name, backup_id):
        backup = self.cli('show-backup', server_name, backup_id)
        return backup[server_name]


class BarmanServer:

    def __init__(self, barman, server_name):
        self.barman = barman
        self.name = server_name
        self.status = barman.server_status(server_name)
        self.checks = barman.server_check(server_name)
        self.backups_done, self.backups_failed = barman.list_backup(
            server_name)

    def backup(self, backup_id):
        return self.barman.show_backup(self.name, backup_id)


class BarmanCollector:

    def __init__(self, barman, servers):
        self.barman = barman
        self.servers = servers
        self.collectors = dict(
            barman_version_info=core.GaugeMetricFamily(
                'barman_version_info', "Barman version information",
                labels=['version']),
            barman_home_filesystem_size_bytes=core.GaugeMetricFamily(
                'barman_home_filesystem_size_bytes',
                "Total size of the filesystem for the Barman home directory"),
            barman_home_free_bytes=core.GaugeMetricFamily(
                'barman_home_free_bytes',
                "Free space available to non-root users on the Barman home filesystem"),
            barman_home_avail_bytes=core.GaugeMetricFamily(
                'barman_home_avail_bytes',
                "Free space available on the Barman home filesystem"),
            barman_home_files=core.GaugeMetricFamily(
                'barman_home_files',
                "Total number of inodes on the Barman home filesystem"),
            barman_home_free=core.GaugeMetricFamily(
                'barman_home_free',
                "Free inodes on the Barman home filesystem"),
            barman_backup_size=core.GaugeMetricFamily(
                'barman_backup_size', "Size of available backups",
                labels=['server', 'number', 'backup_type']),
            barman_backup_wal_size=core.GaugeMetricFamily(
                'barman_backup_wal_size', "WAL size of available backups",
                labels=['server', 'number', 'backup_type']),
            barman_backups_total=core.GaugeMetricFamily(
                "barman_backups_total", "Total number of backups",
                labels=["server"]),
            barman_backups_failed=core.GaugeMetricFamily(
                "barman_backups_failed", "Number of failed backups",
                labels=["server"]),
            barman_backups_by_type_total=core.GaugeMetricFamily(
                "barman_backups_by_type_total", "Number of backups by type",
                labels=["server", "backup_type"]),
            barman_server_active=core.GaugeMetricFamily(
                "barman_server_active", "Whether the Barman server is active",
                labels=["server"]),
            barman_server_disabled=core.GaugeMetricFamily(
                "barman_server_disabled", "Whether the Barman server is disabled",
                labels=["server"]),
            barman_server_in_recovery=core.GaugeMetricFamily(
                "barman_server_in_recovery", "Whether the Barman server is in recovery",
                labels=["server"]),
            barman_minimum_redundancy_satisfied=core.GaugeMetricFamily(
                "barman_minimum_redundancy_satisfied", "Whether minimum redundancy is satisfied",
                labels=["server"]),
            barman_retention_policy_enforced=core.GaugeMetricFamily(
                "barman_retention_policy_enforced", "Whether retention policy is enforced",
                labels=["server"]),
            barman_last_backup=core.GaugeMetricFamily(
                "barman_last_backup", "Last successful backup timestamp",
                labels=["server"]),
            barman_last_backup_copy_time=core.GaugeMetricFamily(
                "barman_last_backup_copy_time", "Last successful backup copy time",
                labels=["server"]),
            barman_first_backup=core.GaugeMetricFamily(
                "barman_first_backup", "First successful backup timestamp",
                labels=["server"]),
            barman_up=core.GaugeMetricFamily(
                "barman_up", "Barman status checks",
                labels=["server", "check"]),
            barman_metrics_update=core.GaugeMetricFamily(
                "barman_metrics_update", "Barman metrics update timestamp",
                labels=["server"])
        )

    def collect(self):
        self.collect_barman_version_info()
        self.collect_barman_home_filesystem_metrics(self.barman.home_directory())

        for server_name in self.barman_servers():
            barman_server = BarmanServer(self.barman, server_name)
            self.collect_first_backup(barman_server)
            self.collect_last_backup(barman_server)
            self.collect_backups_total(barman_server)
            self.collect_backups_failed(barman_server)
            self.collect_backups_by_type_total(barman_server)
            self.collect_server_status_flags(barman_server)
            self.collect_last_backup_copy_time(barman_server)
            self.collect_barman_backup_size(barman_server)
            self.collect_barman_backup_wal_size(barman_server)
            self.collect_barman_up(barman_server)
            self.collect_barman_metrics_update(barman_server)

        for collector in self.collectors.values():
            yield collector

    def barman_servers(self):
        if self.servers[0] == "all":
            return self.barman.servers()
        else:
            return self.servers

    def collect_barman_version_info(self):
        self.collectors['barman_version_info'].add_metric(
            [self.barman.version_value()], 1)

    def collect_barman_home_filesystem_metrics(self, path):
        stats = os.statvfs(path)
        block_size = stats.f_frsize if hasattr(stats, 'f_frsize') else stats.f_bsize
        total_bytes = stats.f_blocks * block_size
        free_bytes = stats.f_bfree * block_size
        avail_bytes = stats.f_bavail * block_size
        total_files = stats.f_files
        free_files = stats.f_ffree

        self.collectors['barman_home_filesystem_size_bytes'].add_metric([], total_bytes)
        self.collectors['barman_home_free_bytes'].add_metric([], free_bytes)
        self.collectors['barman_home_avail_bytes'].add_metric([], avail_bytes)
        self.collectors['barman_home_files'].add_metric([], total_files)
        self.collectors['barman_home_free'].add_metric([], free_files)

    def collect_first_backup(self, barman_server):
        if barman_server.status['first_backup'] and barman_server.status['first_backup'] != 'None':
            first_backup = datetime.strptime(
                barman_server.status['first_backup'], "%Y%m%dT%H%M%S")
            self.collectors['barman_first_backup'].add_metric(
                [barman_server.name], first_backup.strftime("%s"))

    def collect_last_backup(self, barman_server):
        if barman_server.status['last_backup'] and barman_server.status['last_backup'] != 'None':
            last_backup = datetime.strptime(
                barman_server.status['last_backup'], "%Y%m%dT%H%M%S")
            self.collectors['barman_last_backup'].add_metric(
                [barman_server.name], last_backup.strftime("%s"))

    def collect_backups_total(self, barman_server):
        self.collectors['barman_backups_total'].add_metric([barman_server.name], len(
            barman_server.backups_done) + len(barman_server.backups_failed))

    def collect_backups_failed(self, barman_server):
        self.collectors['barman_backups_failed'].add_metric(
            [barman_server.name], len(barman_server.backups_failed))

    def collect_backups_by_type_total(self, barman_server):
        counts = {}
        for backup in barman_server.backups_done + barman_server.backups_failed:
            backup_type = backup.get('backup_type', 'unknown')
            counts[backup_type] = counts.get(backup_type, 0) + 1

        for backup_type, count in counts.items():
            self.collectors['barman_backups_by_type_total'].add_metric(
                [barman_server.name, backup_type], count)

    def collect_server_status_flags(self, barman_server):
        status = barman_server.status

        self.collectors['barman_server_active'].add_metric(
            [barman_server.name], 1 if str(status.get('active', '')).lower() == 'true' else 0)
        self.collectors['barman_server_disabled'].add_metric(
            [barman_server.name], 1 if str(status.get('disabled', '')).lower() == 'true' else 0)

        is_in_recovery = str(status.get('is_in_recovery', '')).lower() in ('true', 'in production', 'production')
        self.collectors['barman_server_in_recovery'].add_metric(
            [barman_server.name], 1 if is_in_recovery else 0)

        redundancy = str(status.get('minimum_redundancy', '')).lower()
        self.collectors['barman_minimum_redundancy_satisfied'].add_metric(
            [barman_server.name], 1 if 'satisfied' in redundancy else 0)

        retention = str(status.get('retention_policies', '')).lower()
        self.collectors['barman_retention_policy_enforced'].add_metric(
            [barman_server.name], 1 if 'enforced' in retention else 0)

    def backup_details(self, barman_server):
        details = {}
        for backup in barman_server.backups_done:
            try:
                details[backup['backup_id']] = barman_server.backup(
                    backup['backup_id'])
            except Exception:
                details[backup['backup_id']] = {}
        return details

    def collect_last_backup_copy_time(self, barman_server):
        last_backup_copy_time = 0
        if len(barman_server.backups_done) > 0:
            backup_id = barman_server.backups_done[0]['backup_id']
            last_backup = barman_server.backup(backup_id)
            last_backup_copy_time = last_backup['base_backup_information']['copy_time_seconds']

        self.collectors['barman_last_backup_copy_time'].add_metric(
            [barman_server.name], last_backup_copy_time)

    def collect_barman_backup_size(self, barman_server):
        backup_details = self.backup_details(barman_server)
        for number, backup in enumerate(barman_server.backups_done, 1):
            backup_type = backup_details.get(backup['backup_id'], {}).get(
                'backup_type', 'unknown')
            self.collectors['barman_backup_size'].add_metric(
                [barman_server.name, str(number), backup_type],
                backup['size_bytes'])

    def collect_barman_backup_wal_size(self, barman_server):
        backup_details = self.backup_details(barman_server)
        for number, backup in enumerate(barman_server.backups_done, 1):
            backup_type = backup_details.get(backup['backup_id'], {}).get(
                'backup_type', 'unknown')
            self.collectors['barman_backup_wal_size'].add_metric(
                [barman_server.name, str(number), backup_type],
                backup['wal_size_bytes'])

    def collect_barman_up(self, barman_server):
        for check_name, check_value in barman_server.checks.items():
            self.collectors['barman_up'].add_metric(
                [barman_server.name, check_name], check_value)

    def collect_barman_metrics_update(self, barman_server):
        self.collectors['barman_metrics_update'].add_metric(
            [barman_server.name], int(time.time()))


class BarmanCollectorCache:
    def __init__(self, barman, servers, cache_time):
        self.barman = barman
        self.servers = servers
        self.cache_time = cache_time
        self._collect = []
        self.start_collect_thread()

    def start_collect_thread(self):
        t = threading.Thread(target=self.collect_loop)
        t.daemon = True
        t.start()

    def collect_loop(self):
        while True:
            barman_collector = BarmanCollector(self.barman, self.servers)
            self._collect = list(barman_collector.collect())
            time.sleep(self.cache_time)

    def collect(self):
        return self._collect


def main():
    args = parse_args()

    if args.version:
        show_version()
    elif args.debug:
        sys.tracebacklimit = 1
        print_metrics_to_stdout(args)
    elif args.file:
        write_metrics_to_file(args)
    else:
        start_exporter_service(args)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Barman exporter",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument('servers', nargs="*", default=['all'],
                        help="Space separated list of "
                             "servers to check")
    parser.add_argument('-u', '--user', metavar='USER',
                        default='prometheus', help="Textfile owner")
    parser.add_argument('-g', '--group', metavar='GROUP',
                        default='prometheus', help="Textfile group")
    parser.add_argument('-m', '--mode', metavar='MODE',
                        default='0644', help="Textfile mode")
    parser.add_argument('-c', '--cache-time', metavar='SECONDS', type=int,
                        default=3600, help='Number of seconds to cache barman output for')
    parser.add_argument('-v', '--version', action='store_true', 
                        help='Show barman exporter version')

    group = parser.add_mutually_exclusive_group()
    group.add_argument('-f', '--file',
                       metavar="TEXTFILE_PATH",
                       help="Save output to textfile")
    group.add_argument('-l', '--listen-address',
                       default='127.0.0.1:9780',
                       metavar="HOST:PORT",
                       help="Address to listen on")
    group.add_argument('-d', '--debug',
                       action='store_true',
                       help="Print output to stdout")

    return parser.parse_args()


def show_version():
    print(BARMAN_EXPORTER_VERSION)
    sys.exit(0)


def write_metrics_to_file(args):
    registry = BarmanCollector(Barman(), args.servers)
    prometheus_client.write_to_textfile(args.file, registry)
    shutil.chown(args.file, user=args.user, group=args.group)
    os.chmod(args.file, mode=int(args.mode, 8))


def start_exporter_service(args):
    try:
        addr, port = args.listen_address.split(":")
    except ValueError as e:
        raise ValueError("Incorrect '--listen-address' value: '{}'.".format(
            args.listen_address), "Use HOST:PORT.") from e

    registry = BarmanCollectorCache(Barman(), args.servers, args.cache_time)
    core.REGISTRY.register(registry)

    print("Listening on " + args.listen_address)
    prometheus_client.start_http_server(int(port), addr)

    while True:
        time.sleep(1)


def print_metrics_to_stdout(args):
    registry = BarmanCollector(Barman(), args.servers)
    print(prometheus_client.generate_latest(registry).decode())


if __name__ == "__main__":
    main()
