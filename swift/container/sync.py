# Copyright (c) 2010-2011 OpenStack, LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import time
import random

from swift.container import server as container_server
from swift.common import client, direct_client
from swift.common.ring import Ring
from swift.common.db import ContainerBroker
from swift.common.utils import audit_location_generator, get_logger, \
    normalize_timestamp, TRUE_VALUES, validate_sync_to
from swift.common.daemon import Daemon


class _Iter2FileLikeObject(object):
    """
    Returns an iterator's contents via :func:`read`, making it look like a file
    object.
    """

    def __init__(self, iterator):
        self.iterator = iterator
        self._chunk = ''

    def read(self, size=-1):
        """
        read([size]) -> read at most size bytes, returned as a string.

        If the size argument is negative or omitted, read until EOF is reached.
        Notice that when in non-blocking mode, less data than what was
        requested may be returned, even if no size parameter was given.
        """
        if size < 0:
            chunk = self._chunk
            self._chunk = ''
            return chunk + ''.join(self.iterator)
        chunk = ''
        try:
            chunk = self.iterator.next()
        except StopIteration:
            pass
        if len(chunk) <= size:
            return chunk
        self._chunk = chunk[size:]
        return chunk[:size]


class ContainerSync(Daemon):
    """
    Daemon to sync syncable containers.

    This is done by scanning the local devices for container databases and
    checking for x-container-sync-to and x-container-sync-key metadata values.
    If they exist, the last known synced ROWID is retreived from the container
    broker via get_info()['x_container_sync_row']. All newer rows trigger PUTs
    or DELETEs to the other container.

    .. note::

        This does not sync standard object POSTs, as those do not cause
        container row updates. A workaround is to do X-Copy-From POSTs. We're
        considering solutions to this limitation but leaving it as is for now
        since POSTs are fairly uncommon.

    :param conf: The dict of configuration values from the [container-sync]
                 section of the container-server.conf
    :param object_ring: If None, the <swift_dir>/object.ring.gz will be loaded.
                        This is overridden by unit tests.
    """

    def __init__(self, conf, object_ring=None):
        #: The dict of configuration values from the [container-sync] section
        #: of the container-server.conf.
        self.conf = conf
        #: Logger to use for container-sync log lines.
        self.logger = get_logger(conf, log_route='container-sync')
        #: Path to the local device mount points.
        self.devices = conf.get('devices', '/srv/node')
        #: Indicates whether mount points should be verified as actual mount
        #: points (normally true, false for tests and SAIO).
        self.mount_check = \
            conf.get('mount_check', 'true').lower() in TRUE_VALUES
        #: Minimum time between full scans. This is to keep the daemon from
        #: running wild on near empty systems.
        self.interval = int(conf.get('interval', 300))
        #: Maximum amount of time to spend syncing a container before moving on
        #: to the next one. If a conatiner sync hasn't finished in this time,
        #: it'll just be resumed next scan.
        self.container_time = int(conf.get('container_time', 60))
        #: The list of hosts we're allowed to send syncs to.
        self.allowed_sync_hosts = [h.strip()
            for h in conf.get('allowed_sync_hosts', '127.0.0.1').split(',')
            if h.strip()]
        #: Number of containers with sync turned on that were successfully
        #: synced.
        self.container_syncs = 0
        #: Number of successful DELETEs triggered.
        self.container_deletes = 0
        #: Number of successful PUTs triggered.
        self.container_puts = 0
        #: Number of containers that didn't have sync turned on.
        self.container_skips = 0
        #: Number of containers that had a failure of some type.
        self.container_failures = 0
        #: Time of last stats report.
        self.reported = time.time()
        swift_dir = conf.get('swift_dir', '/etc/swift')
        #: swift.common.ring.Ring for locating objects.
        self.object_ring = object_ring or \
            Ring(os.path.join(swift_dir, 'object.ring.gz'))

    def run_forever(self):
        """
        Runs container sync scans until stopped.
        """
        time.sleep(random.random() * self.interval)
        while True:
            begin = time.time()
            all_locs = audit_location_generator(self.devices,
                                                container_server.DATADIR,
                                                mount_check=self.mount_check,
                                                logger=self.logger)
            for path, device, partition in all_locs:
                self.container_sync(path)
                if time.time() - self.reported >= 3600:  # once an hour
                    self.report()
            elapsed = time.time() - begin
            if elapsed < self.interval:
                time.sleep(self.interval - elapsed)

    def run_once(self):
        """
        Runs a single container sync scan.
        """
        self.logger.info(_('Begin container sync "once" mode'))
        begin = time.time()
        all_locs = audit_location_generator(self.devices,
                                            container_server.DATADIR,
                                            mount_check=self.mount_check,
                                            logger=self.logger)
        for path, device, partition in all_locs:
            self.container_sync(path)
            if time.time() - self.reported >= 3600:  # once an hour
                self.report()
        self.report()
        elapsed = time.time() - begin
        self.logger.info(
            _('Container sync "once" mode completed: %.02fs'), elapsed)

    def report(self):
        """
        Writes a report of the stats to the logger and resets the stats for the
        next report.
        """
        self.logger.info(
            _('Since %(time)s: %(sync)s synced [%(delete)s deletes, %(put)s '
              'puts], %(skip)s skipped, %(fail)s failed'),
            {'time': time.ctime(self.reported),
             'sync': self.container_syncs,
             'delete': self.container_deletes,
             'put': self.container_puts,
             'skip': self.container_skips,
             'fail': self.container_failures})
        self.reported = time.time()
        self.container_syncs = 0
        self.container_deletes = 0
        self.container_puts = 0
        self.container_skips = 0
        self.container_failures = 0

    def container_sync(self, path):
        """
        Checks the given path for a container database, determines if syncing
        is turned on for that database and, if so, sends any updates to the
        other container.

        :param path: the path to a container db
        """
        try:
            if not path.endswith('.db'):
                return
            broker = ContainerBroker(path)
            info = broker.get_info()
            if not broker.is_deleted():
                sync_to = None
                sync_key = None
                sync_row = info['x_container_sync_row']
                for key, (value, timestamp) in broker.metadata.iteritems():
                    if key.lower() == 'x-container-sync-to':
                        sync_to = value
                    elif key.lower() == 'x-container-sync-key':
                        sync_key = value
                if not sync_to or not sync_key:
                    self.container_skips += 1
                    return
                sync_to = sync_to.rstrip('/')
                err = validate_sync_to(sync_to, self.allowed_sync_hosts)
                if err:
                    self.logger.info(
                        _('ERROR %(db_file)s: %(validate_sync_to_err)s'),
                        {'db_file': broker.db_file,
                         'validate_sync_to_err': err})
                    self.container_failures += 1
                    return
                stop_at = time.time() + self.container_time
                while time.time() < stop_at:
                    rows = broker.get_items_since(sync_row, 1)
                    if not rows:
                        break
                    if not self.container_sync_row(rows[0], sync_to, sync_key,
                                                   broker, info):
                        return
                    sync_row = rows[0]['ROWID']
                    broker.set_x_container_sync_row(sync_row)
                self.container_syncs += 1
        except Exception:
            self.container_failures += 1
            self.logger.exception(_('ERROR Syncing %s'), (broker.db_file))

    def container_sync_row(self, row, sync_to, sync_key, broker, info):
        """
        Sends the update the row indicates to the sync_to container.

        :param row: The updated row in the local database triggering the sync
                    update.
        :param sync_to: The URL to the remote container.
        :param sync_key: The X-Container-Sync-Key to use when sending requests
                         to the other container.
        :param broker: The local container database broker.
        :param info: The get_info result from the local container database
                     broker.
        :returns: True on success
        """
        try:
            if row['deleted']:
                try:
                    client.delete_object(sync_to, name=row['name'],
                        headers={'X-Timestamp': row['created_at'],
                                 'X-Container-Sync-Key': sync_key})
                except client.ClientException, err:
                    if err.http_status != 404:
                        raise
                self.container_deletes += 1
            else:
                part, nodes = self.object_ring.get_nodes(
                    info['account'], info['container'],
                    row['name'])
                random.shuffle(nodes)
                exc = None
                for node in nodes:
                    try:
                        headers, body = \
                            direct_client.direct_get_object(node, part,
                                info['account'], info['container'],
                                row['name'], resp_chunk_size=65536)
                        break
                    except client.ClientException, err:
                        exc = err
                else:
                    if exc:
                        raise exc
                    raise Exception(_('Unknown exception trying to GET: '
                        '%(node)r %(account)r %(container)r %(object)r'),
                        {'node': node, 'part': part,
                         'account': info['account'],
                         'container': info['container'],
                         'object': row['name']})
                for key in ('date', 'last-modified'):
                    if key in headers:
                        del headers[key]
                if 'etag' in headers:
                    headers['etag'] = headers['etag'].strip('"')
                headers['X-Timestamp'] = row['created_at']
                headers['X-Container-Sync-Key'] = sync_key
                client.put_object(sync_to, name=row['name'],
                                headers=headers,
                                contents=_Iter2FileLikeObject(body))
                self.container_puts += 1
        except client.ClientException, err:
            if err.http_status == 401:
                self.logger.info(_('Unauth %(sync_from)r '
                    '=> %(sync_to)r key: %(sync_key)r'),
                    {'sync_from': '%s/%s' %
                        (client.quote(info['account']),
                         client.quote(info['container'])),
                     'sync_to': sync_to,
                     'sync_key': sync_key})
            elif err.http_status == 404:
                self.logger.info(_('Not found %(sync_from)r '
                    '=> %(sync_to)r key: %(sync_key)r'),
                    {'sync_from': '%s/%s' %
                        (client.quote(info['account']),
                         client.quote(info['container'])),
                     'sync_to': sync_to,
                     'sync_key': sync_key})
            else:
                self.logger.exception(
                    _('ERROR Syncing %(db_file)s %(row)s'),
                    {'db_file': broker.db_file, 'row': row})
            self.container_failures += 1
            return False
        except Exception:
            self.logger.exception(
                _('ERROR Syncing %(db_file)s %(row)s'),
                {'db_file': broker.db_file, 'row': row})
            self.container_failures += 1
            return False
        return True
