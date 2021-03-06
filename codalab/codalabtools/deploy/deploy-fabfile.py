"""
Defines deployment commands.
"""

import logging
import logging.config
import os
from os.path import (abspath, dirname)
import sys

# Add codalabtools to the module search path
sys.path.append(dirname(dirname(dirname(abspath(__file__)))))

from StringIO import StringIO
from fabric.api import (cd,
                        env,
                        execute,
                        get,
                        prefix,
                        put,
                        require,
                        task,
                        roles,
                        require,
                        run,
                        settings,
                        shell_env,
                        sudo)
from fabric.contrib.files import exists
from fabric.network import ssh
from fabric.utils import fastprint
from codalabtools.deploy import DeploymentConfig, Deployment

logger = logging.getLogger('codalabtools')

############################################################
# Configuration (run every time)


@task
def using(path):
    """
    Specifies a location for the CodaLab configuration file (e.g., deployment.config)
    """
    env.cfg_path = path


@task
def config(label):
    """
    Reads deployment parameters for the given setup.
    label: Label identifying the desired setup (e.g., prod, test, etc.)
    """
    env.cfg_label = label
    print "Deployment label is:", env.cfg_label
    print "Loading configuration from:", env.cfg_path
    configuration = DeploymentConfig(label, env.cfg_path)
    print "Configuring logger..."
    logging.config.dictConfig(configuration.getLoggerDictConfig())
    logger.info("Loaded configuration from file: %s", configuration.getFilename())
    env.roledefs = {'web': configuration.getWebHostnames()}

    # Credentials
    env.user = configuration.getVirtualMachineLogonUsername()
    env.password = configuration.getVirtualMachineLogonPassword()
    env.key_filename = configuration.getServiceCertificateKeyFilename()

    # Repository
    env.git_codalab_tag = configuration.getGitTag()
    env.deploy_codalab_dir = 'codalab'  # Directory for codalab competitions

    env.django_settings_module = 'codalab.settings'
    env.django_configuration = configuration.getDjangoConfiguration()  # Prod or Dev
    env.config_http_port = '80'
    env.config_server_name = "{0}.cloudapp.net".format(configuration.getServiceName())

    env.configuration = True
    env.SHELL_ENV = {}

def setup_env():
    env.SHELL_ENV.update(dict(
        DJANGO_SETTINGS_MODULE=env.django_settings_module,
        DJANGO_CONFIGURATION=env.django_configuration,
        CONFIG_HTTP_PORT=env.config_http_port,
        CONFIG_SERVER_NAME=env.config_server_name,
    ))
    return prefix('source ~/%s/venv/bin/activate' % env.deploy_codalab_dir), shell_env(**env.SHELL_ENV)

############################################################
# Installation (one-time)

@roles('web')
@task
def install():
    '''
    Install everything from scratch (idempotent).
    '''
    # Install Linux packages
    sudo('apt-get install -y git xclip python-virtualenv virtualenvwrapper zip ruby')
    sudo('apt-get install -y python-dev libmysqlclient-dev libjpeg-dev')
    sudo('apt-get install -y nginx supervisor')

    # Setup repositories
    def ensure_repo_exists(repo, dest):
        run('[ -e %s ] || git clone %s %s' % (dest, repo, dest))
    ensure_repo_exists('https://github.com/codalab/codalab', env.deploy_codalab_dir)

    # Initial setup
    with cd(env.deploy_codalab_dir):
        run('git checkout %s' % env.git_codalab_tag)
        run('./dev_setup.sh')

    # Deploy!
    _deploy()

@roles('web')
@task
def install_mysql(choice='all'):
    """
    Installs a local instance of MySQL of the web instance. This will only work
    if the number of web instances is one.

    choice: Indicates which assets to create/install:
        'mysql'      -> just install MySQL; don't create the databases
        'website_db' -> just create the website database
        'bundles_db' -> just create the bundle service database
        'all' or ''  -> install everything
    """
    require('configuration')
    if len(env.roledefs['web']) != 1:
        raise Exception("Task install_mysql requires exactly one web instance.")

    if choice == 'mysql':
        choices = {'mysql'}
    elif choice == 'website_db':
        choices = {'website_db'}
    elif choice == 'bundles_db':
        choices = {'bundles_db'}
    elif choice == 'all':
        choices = {'mysql', 'website_db', 'bundles_db'}
    else:
        raise ValueError("Invalid choice: %s. Valid choices are: 'build', 'web' or 'all'." % (choice))

    configuration = DeploymentConfig(env.cfg_label, env.cfg_path)
    dba_password = configuration.getDatabaseAdminPassword()

    if 'mysql' in choices:
        sudo('DEBIAN_FRONTEND=noninteractive apt-get install -y mysql-server')
        sudo('mysqladmin -u root password {0}'.format(dba_password))

    if 'website_db' in choices:
        db_name = configuration.getDatabaseName()
        db_user = configuration.getDatabaseUser()
        db_password = configuration.getDatabasePassword()
        cmds = ["create database {0};".format(db_name),
                "create user '{0}'@'localhost' IDENTIFIED BY '{1}';".format(db_user, db_password),
                "GRANT ALL PRIVILEGES ON {0}.* TO '{1}'@'localhost' WITH GRANT OPTION;".format(db_name, db_user)]
        run('mysql --user=root --password={0} --execute="{1}"'.format(dba_password, " ".join(cmds)))

    if 'bundles_db' in choices:
        db_name = configuration.getBundleServiceDatabaseName()
        db_user = configuration.getBundleServiceDatabaseUser()
        db_password = configuration.getBundleServiceDatabasePassword()
        cmds = ["create database {0};".format(db_name),
                "create user '{0}'@'localhost' IDENTIFIED BY '{1}';".format(db_user, db_password),
                "GRANT ALL PRIVILEGES ON {0}.* TO '{1}'@'localhost' WITH GRANT OPTION;".format(db_name, db_user)]
        run('mysql --user=root --password={0} --execute="{1}"'.format(dba_password, " ".join(cmds)))


############################################################
# Deployment

@roles('web')
@task
def supervisor(command):
    """
    Starts the supervisor on the web instances.
    """
    env_prefix, env_shell = setup_env()
    with env_prefix, env_shell, cd(env.deploy_codalab_dir):
        if command == 'start':
            run('mkdir -p ~/logs')
            run('supervisord -c codalab/config/generated/supervisor.conf')
        elif command == 'stop':
            run('supervisorctl -c codalab/config/generated/supervisor.conf stop all')
            run('supervisorctl -c codalab/config/generated/supervisor.conf shutdown')
            # HACK: since competition worker is multithreaded, we need to kill all running processes
            with settings(warn_only=True):
                run('pkill -9 -f worker.py')
        elif command == 'restart':
            run('supervisorctl -c codalab/config/generated/supervisor.conf restart all')
        else:
            raise 'Unknown command: %s' % command


@roles('web')
@task
def nginx_restart():
    """
    Restarts nginx on the web server.
    """
    sudo('/etc/init.d/nginx restart')


# Maintenance and diagnostics
@roles('web')
@task
def maintenance(mode):
    """
    Begin or end maintenance (mode is 'begin' or 'end')
    """
    modes = {'begin': '1', 'end': '0'}
    if mode not in modes:
        print "Invalid mode. Valid values are 'begin' or 'end'"
        sys.exit(1)

    require('configuration')
    env.SHELL_ENV['MAINTENANCE_MODE'] = modes[mode]

    # Update nginx.conf
    env_prefix, env_shell = setup_env()
    with env_prefix, env_shell, cd(env.deploy_codalab_dir), cd('codalab'):
        run('python manage.py config_gen')

    nginx_restart()


@roles('web')
@task
def deploy():
    """
    Put a maintenance message, deploy, and then restore website.
    """
    maintenance('begin')
    supervisor('stop')
    _deploy()
    supervisor('start')
    maintenance('end')


def _deploy():
    # Update competition website
    # Pull branch and run requirements file, for info about requirments look into dev_setp.sh
    env_prefix, env_shell = setup_env()
    with env_prefix, env_shell, cd(env.deploy_codalab_dir):
        run('git pull')
        run('git checkout %s' % env.git_codalab_tag)
        run('./dev_setup.sh')


    # Create local.py
    cfg = DeploymentConfig(env.cfg_label, env.cfg_path)
    dep = Deployment(cfg)
    buf = StringIO()
    buf.write(dep.getSettingsFileContent())
    # local.py is generated here. For more info about content look into deploy/__.init__.py
    settings_file = os.path.join(env.deploy_codalab_dir, 'codalab', 'codalab', 'settings', 'local.py')
    put(buf, settings_file)

    # Update the website configuration
    env_prefix, env_shell = setup_env()
    with env_prefix, env_shell, cd(env.deploy_codalab_dir), cd('codalab'):
        # Generate configuration files (bundle_server_config, nginx, etc.)
        run('python manage.py config_gen')
        # Migrate database
        run('python manage.py syncdb --migrate')
        # Create static pages
        run('python manage.py collectstatic --noinput')
        # For sending email, have the right domain name.
        run('python manage.py set_site %s' % cfg.getSslRewriteHosts()[0])
        # Create a superuser for OAuth
        run('python manage.py create_codalab_user %s' % cfg.getDatabaseAdminPassword())
        # Allow bundle service to connect to website for OAuth
        run('mkdir -p ~/.codalab && python manage.py set_oauth_key ./config/generated/bundle_server_config.json > ~/.codalab/config.json')
        # Put nginx and supervisor configuration files in place
        sudo('ln -sf `pwd`/config/generated/nginx.conf /etc/nginx/sites-enabled/codalab.conf')
        sudo('ln -sf `pwd`/config/generated/supervisor.conf /etc/supervisor/conf.d/codalab.conf')
        # Setup new relic
        run('newrelic-admin generate-config %s newrelic.ini' % cfg.getNewRelicKey())

    # Install SSL certficates (/etc/ssl/certs/)
    require('configuration')
    if (len(cfg.getSslCertificateInstalledPath()) > 0) and (len(cfg.getSslCertificateKeyInstalledPath()) > 0):
        put(cfg.getSslCertificatePath(), cfg.getSslCertificateInstalledPath(), use_sudo=True)
        put(cfg.getSslCertificateKeyPath(), cfg.getSslCertificateKeyInstalledPath(), use_sudo=True)
    else:
        logger.info("Skipping certificate installation because both files are not specified.")


@roles('web')
@task
def get_database_dump():
    '''Saves backups to $CODALAB_MYSQL_BACKUP_DIR/launchdump-year-month-day-hour-min-second.sql.gz'''
    require('configuration')
    configuration = DeploymentConfig(env.cfg_label, env.cfg_path)
    db_host = "localhost"
    db_name = configuration.getDatabaseName()
    db_user = configuration.getDatabaseUser()
    db_password = configuration.getDatabasePassword()

    dump_file_name = 'competitiondump.sql.gz'

    run('mysqldump --host=%s --user=%s --password=%s %s --port=3306 | gzip > /tmp/%s' % (
        db_host,
        db_user,
        db_password,
        db_name,
        dump_file_name)
    )
    backup_directory = os.path.dirname(os.path.realpath(__file__))

    get('/tmp/%s' % dump_file_name, backup_directory)

