#!/usr/bin/env python3
# Copyright 2020 Erik Lönroth
# See LICENSE file for licensing details.

import logging
import subprocess
import sys
import os
from pathlib import Path
from jinja2 import Environment, FileSystemLoader

from ops.charm import CharmBase
from ops.main import main
from ops.framework import StoredState
from ops.lib import use
from io import BytesIO


from ops.model import (
    ActiveStatus,
    BlockedStatus,
    MaintenanceStatus
)

from utils import open_port

logger = logging.getLogger(__name__)

# POSTGRESQL interface documentation
# https://github.com/canonical/ops-lib-pgsql
pgsql = use("pgsql", 1, "postgresql-charmers@lists.launchpad.net")

NEXTCLOUD_CONFIG_PHP = '/var/www/nextcloud/config/config.php'

class NextcloudCharm(CharmBase):
    _stored = StoredState()
    
    def __init__(self, *args):
        super().__init__(*args)

        self._stored.set_default(version=0)
        
        self._stored.set_default(data_dir='/var/www/nextcloud/data/')

        self._stored.set_default(nextcloud_fetched=False)

        self._stored.set_default(database_available=False)
        
        self.framework.observe(self.on.install, self._on_install)
        
        self.framework.observe(self.on.config_changed, self._on_config_changed)

        self._stored.set_default(db_conn_str=None, db_uri=None, db_ro_uris=[])

        self.db = pgsql.PostgreSQLClient(self, 'db')  # 'db' relation in metadata.yaml

        self.framework.observe(self.db.on.database_relation_joined, self._on_database_relation_joined)
        
        self.framework.observe(self.db.on.master_changed, self._on_master_changed)

        self.framework.observe(self.db.on.standby_changed, self._on_standby_changed)
        
        ### ACTIONS ###
        self.framework.observe(self.on.add_trusted_domain_action,
                               self._on_add_trusted_domain_action)
        self.framework.observe(self.on.add_missing_indices_action,
                               self._on_add_missing_indices_action)
        self.framework.observe(self.on.convert_filecache_bigint_action,
                               self._on_convert_filecache_bigint_action)
        self.framework.observe(self.on.maintenance_action,
                               self._on_maintenance_action)


    def _on_install(self, event):
        # self._handle_storage()

        self._install_deps()

        if not self._stored.nextcloud_fetched:
            # Fetch nextcloud to /var/www/
            self._fetch_nextcloud()

        self._config_apache2()
        
        self._config_php()

        if not self._stored.database_available:
            self.unit.status = BlockedStatus("Missing postgresql relation data.")
            event.defer()
            return
        else:
            self.unit.status = MaintenanceStatus("Database available.")    
            
        if not self._stored.nextcloud_initialized:
            self._init_nextcloud()
        
        
    def _on_config_changed(self, _):
            logger.debug("version: %r", self._stored.version)

    
    def _on_database_relation_joined(self, event: pgsql.DatabaseRelationJoinedEvent):
        if self.model.unit.is_leader():
            # Provide requirements to the PostgreSQL server.
            event.database = 'nextcloud'  # Request database named mydbname
            event.extensions = ['citext']  # Request the citext extension installed
        elif event.database != 'nextcloud':
            # Leader has not yet set requirements. Defer, incase this unit
            # becomes leader and needs to perform that operation.
            event.defer()
            return
            
    def _on_master_changed(self, event: pgsql.MasterChangedEvent):
        if event.database != 'nextcloud':
            # Leader has not yet set requirements. Wait until next event,
            # or risk connecting to an incorrect database.
            return
        
        # The connection to the primary database has been created,
        # changed or removed. More specific events are available, but
        # most charms will find it easier to just handle the Changed
        # events. event.master is None if the master database is not
        # available, or a pgsql.ConnectionString instance.
        print("============master============")
        print(str(event.master))
        
        self._stored.db_conn_str = None if event.master is None else event.master.conn_str
        self._stored.db_uri = None if event.master is None else event.master.uri
        self._stored.dbname = None if event.master is None else event.master.dbname
        self._stored.dbuser = None if event.master is None else event.master.user
        self._stored.dbpass = None if event.master is None else event.master.password
        self._stored.dbhost = None if event.master is None else event.master.host
        self._stored.dbport = None if event.master is None else event.master.port
        self._stored.dbtype = None if event.master is None else 'pgsql'

        self._stored.database_available = True
        
        # You probably want to emit an event here or call a setup routine to
        # do something useful with the libpq connection string or URI now they
        # are available.

    def _on_standby_changed(self, event: pgsql.StandbyChangedEvent):
        if event.database != 'nextcloud':
            # Leader has not yet set requirements. Wait until next event,
            # or risk connecting to an incorrect database.
            return

        # Charms needing access to the hot standby databases can get
        # their connection details here. Applications can scale out
        # horizontally if they can make use of the read only hot
        # standby replica databases, rather than only use the single
        # master. event.stanbys will be an empty list if no hot standby
        # databases are available.
        self._stored.db_ro_uris = [c.uri for c in event.standbys]


    ## ACTIONS

    def _on_add_trusted_domain_action(self, event):
        pass

    def _on_add_missing_indices_action(self,event):
        pass

    def _on_add_missing_indices_action(self, event):
        pass

    def _on_convert_filecache_bigint_action(self, event):
        pass

    def _on_maintenance_action(self, event):
        """
        Action to take the site in or out of maintenance mode.
        :param event:
        :return:
        """
        try:
            hostname = subprocess.check_output(
                "hostname",
                shell=True
            )

            event.set_results({"maintenence": hostname})

        except subprocess.CalledProcessError as e:
            print(e)
            sys.exit(-1)


    def _install_deps(self):
        """
        Install dependencies for running nextcloud.
        """
        self.unit.status = MaintenanceStatus("Begin installing dependencies...")

        try:
            packages = ['apache2',
                        'libapache2-mod-php7.2',
                        'php7.2-gd',
                        'php7.2-json',
                        'php7.2-mysql',
                        'php7.2-pgsql',
                        'php7.2-curl',
                        'php7.2-mbstring',
                        'php7.2-intl',
                        'php-imagick',
                        'php7.2-zip',
                        'php7.2-xml',
                        'php-apcu',
                        'php-redis',
                        'php-smbclient']

            command = ["apt", "install", "-y"]

            command.extend(packages)

            subprocess.run(command, check=True)

            self.unit.status = MaintenanceStatus("Dependencies installed")
            
        except subprocess.CalledProcessError as e:
            print(e)
            sys.exit(-1)


    def _fetch_nextcloud(self):
        """
        Fetch and Install nextcloud from internet
        Sources are about 100M.
        """
        self.unit.status = MaintenanceStatus("Begin fetching nextcloud sources.")

        import requests
        import tarfile
        
        source = 'https://download.nextcloud.com/server/releases/nextcloud-18.0.3.tar.bz2'

        checksum = '7b67e709006230f90f95727f9fa92e8c73a9e93458b22103293120f9cb50fd72'

        try:
            response = requests.get(source, allow_redirects=True, stream=True)
            
            dst=Path('/var/www/')
            
            with tarfile.open( fileobj=BytesIO( response.content ), mode='r:bz2' ) as tfile:            
                tfile.extractall( path=dst )

            self.unit.status = MaintenanceStatus("Nexcloud sources installed")

            self._stored.nextcloud_fetched = True
            
        except subprocess.CalledProcessError as e:
            print(e)
            sys.exit(-1)


    def _handle_storage(self):
        """ 
       
        Handles juju storage, using 'location' in metadata.yaml if provided on deploy.
       
        """
        pass #not implemented
        self.unit.status = MaintenanceStatus("Begin handle storage...")

    
        data_dir = unitdata.kv().get("nextcloud.storage.data.mount")

        if os.path.exists(str(data_dir)):
            # Use non default for nextcloud

            logger.debug("nextcloud storage location for data set as: {}".format(data_dir))

            host.chownr(data_dir, "www-data", "www-data", follow_links=False, chowntopdir=True)

            os.chmod(data_dir, 0o700)

        else:
            # If no custom data_dir get to us via storage, we use the default
            data_dir = '/var/www/nextcloud/data'



    def _config_php(self):
        """
        Renders the phpmodule for nextcloud (nextcloud.ini)
        This is instead of manipulating the system wide php.ini
        which might be overwitten or changed from elsewhere.
        """
        self.unit.status = MaintenanceStatus("Begin config php.")

        phpmod_context = {
            'max_file_uploads': self.config.get('php_max_file_uploads'),
            'upload_max_filesize': self.config.get('php_upload_max_filesize'),
            'post_max_size': self.config.get('php_post_max_size'),
            'memory_limit': self.config.get('php_memory_limit')
        }

        template = Environment(
            loader=FileSystemLoader(Path( self.charm_dir / 'templates' ))).get_template('nextcloud.ini.j2')

        target = Path('/etc/php/7.2/mods-available/nextcloud.ini')

        target.write_text( template.render( phpmod_context ) )
        
        subprocess.check_call(['phpenmod', 'nextcloud'])

        # Restart required after phpenmod config changes.

    def _init_nextcloud(self):
            
        self.unit.status = MaintenanceStatus("Begin initializing nextcloud...")

        ctx = {'dbtype': self._stored.dbtype,
               'dbname': self._stored.dbname,
               'dbhost': self._stored.dbhost,
               'dbpass': self._stored.dbpass,
               'dbuser': self._stored.dbuser,
               'adminpassword': self.config.get('admin-password'),
               'adminuser': self.config.get('admin-user'),
               'datadir': '/var/www/nextcloud/data' 
               }
        
        nextcloud_init = ("sudo -u www-data /usr/bin/php occ maintenance:install "
                      "--database {dbtype} --database-name {dbname} "
                      "--database-host {dbhost} --database-pass {dbpass} "
                      "--database-user {dbuser} --admin-user {adminuser} "
                      "--admin-pass {adminpassword} "
                      "--data-dir {datadir} ").format(**ctx)

        with os.chdir('/var/www/nextcloud'):

            subprocess.call(("sudo chown -R www-data:www-data .").split())

            subprocess.call(nextcloud_init.split())

            #TODO: This is wrong and will also replace other values in config.php
            #BUG - perhaps add a config here with trusted_domains.
            Path('/var/www/nextcloud/config/config.php').write_text(
                Path('/var/www/nextcloud/config/config.php').open().read().replace(
                    "localhost", self.config.get('fqdn') or unit_public_ip()))


        open_port(port='80')

        self.unit.status = MaintenanceStatus("Nextcloud init complete.")


    def _config_apache2(self):
        """
        Configures apache2
        """
        self.unit.status = MaintenanceStatus("Begin config apache2.")

        ctx = {}

        template = Environment(
            loader=FileSystemLoader(Path( self.charm_dir / 'templates' ))).get_template('nextcloud.conf.j2')

        target = Path('/etc/apache2/sites-available/nextcloud.conf')

        target.write_text( template.render( ctx ) )
        # Enable required modules.        
        for module in ['rewrite', 'headers', 'env', 'dir', 'mime']:
            subprocess.call(['a2enmod', module])                

        subprocess.check_call(['a2ensite', 'nextcloud'])

        self.unit.status = MaintenanceStatus("apache2 config complete.")



        
if __name__ == "__main__":
    main(NextcloudCharm)
