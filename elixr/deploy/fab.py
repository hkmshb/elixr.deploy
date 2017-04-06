"""A colection of reusable functions for fabric bafiles.sed deployment.
"""
import os
import random
from enum import Enum
from fabric.contrib import files
from fabric import operations as ops
from fabric.utils import _AttributeDict as AttrDict
from fabric.api import cd, env, local, run, sudo



## funcs


def generate_random(length=50):
    chars = 'abcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*(-_=+)'
    text = ''.join(random.SystemRandom().choice(chars) for _ in range(length))
    return text


def to_bool(value):
    if type(value) not in (str,):
        value = str(value)

    value = value.lower()
    if value in ('true', 'yes', 't', 'y', '1'):
        return True
    elif value in ('false', 'no', 'f', 'n', '0'):
        return False
    raise ValueError("Value '%s' cannot be converted to bool" % value)


## enum types
class InitSystem(Enum):
    upstart = 1
    systemd = 2


class WebProxyServer(Enum):
    nginx   = 1
    apache2 = 2


class FabHelper(object):
    """Defines a collection of reusable functions for fabric based deployment.

    Utility functions are defined within a class rather than at module level so
    as to ease override of certain behaviours.
    """
    class Meta(object):
        required_attrs = ('project', 'repo_url', 'base_dir', 'site_subdirs')
        
        ## consts
        PY_VERSION = 'python3.5'

        ## vars
        project = None
        repo_url = None
        uses_celeryd = False
        wsgi_conf = 'uwsgi.ini'
        base_dir = '/opt/webapps'
        pip_rfile = 'requirements.txt'
        web_proxy = WebProxyServer.nginx
        init_system = InitSystem.systemd
        site_subdirs = ['source', 'venv', 'public']

        def build_context(self, **options):
            context = AttrDict({'user': env.user, 'host': env.host})
            self._collect(context, options, self.required_attrs, True)
            self._collect(context, options, self.get_optional_attrs(), False)
            context.update(options)
            return context
        
        def _collect(self, context, options, attributes, required):
            errmsg_fmt = "`%s` not defined. Provide as argument or Meta attribute."
            for attr in attributes:
                value = options.pop(attr, None) or getattr(self, attr)
                if not value and required:
                    raise ValueError(errmsg_fmt % attr)
                context[attr] = value
        
        def get_optional_attrs(self):
            attrs = [n for n in dir(self) if not n.startswith('_')]
            attrs.remove('required_attrs')
            return attrs
        
        
    def __init__(self, project, staging, repo_url, **extras):
        self.staging = to_bool(staging)
        self._meta = self.Meta()
        self.ctx = self._meta.build_context(**extras)
        
        # update context
        self.ctx.site = self.ctx.project
        if self.staging:
            self.ctx.site += '-staging'
        self.ctx.site_dir = '%(base_dir)s/%(site)s' % self.ctx

        # critical that subdirectories path are registered with context
        self._make_subdirectories()
    
    def deploy(self):
        self._pre_deploy()
        self._get_latest_source()
        self._update_virtualenv()
        self._update_configs()
        self._post_deploy()

    def _pre_deploy(self):
        pass
    
    def _post_deploy(self):
        pass
    
    def _expand_template(self, fp, use_sudo=False, flags='i', **mappings):
        fmappings = {
            '<usr>': self.ctx.user,
            '<host>': self.ctx.host,
            '<site>': self.ctx.site,
            '<project>': self.ctx.project
        }
        fmappings.update(mappings)
        for before, after in fmappings.items():
            files.sed(fp, before, after, use_sudo=use_sudo or False, 
                      flags=flags or 'i')

    def _get_latest_source(self):
        ctx = self.ctx
        if files.exists('%(source_dir)s/.git' % ctx):
            run('cd %(source_dir)s && git fetch' % ctx)
        else:
            run('git clone %(repo_url)s %(source_dir)s' % ctx)
        
        current_commit = local("git log -n 1 --format=%H", capture=True)
        lctx = {'dir': ctx.source_dir, 'commit_hash': current_commit}
        run("cd %(dir)s && git reset --hard '%(commit_hash)s'" % lctx)

    def _make_subdirectories(self):
        ctx = self.ctx
        for subdir in ctx.site_subdirs:
            ctx._subdir = subdir.replace(' ', '_')
            fp = '%(site_dir)s/%(_subdir)s' % ctx
            run('mkdir -p %s' % fp)
            ctx['%s_dir' % subdir] = fp
        del ctx['_subdir']  # cleanup
    
    def _update_configs(self):
        self._update_wsgi_server_conf()
        self._update_web_proxy_server_conf()
        self._update_project_init_sys_config()
        if self.ctx.uses_celeryd:
            self._update_celeryd_init_sys_config()
    
    def _update_virtualenv(self):
        ctx = self.ctx
        if not files.exists('%(venv_dir)s/bin/pip' % ctx):
            run('virtualenv --python=%(PY_VERSION)s %(venv_dir)s' % ctx)
        run('%(venv_dir)s/bin/pip install -r %(source_dir)s/%(pip_rfile)s' % ctx)
    
    def _replace_conf(self, source_conf, target_conf):
        if files.exists(target_conf):
            sudo('rm %s' % target_conf)
        
        sudo('cp %s %s' % (source_conf, target_conf))
        self._expand_template(target_conf, use_sudo=True)
    
    def _update_celeryd_options_conf(self):
        ctx = self.ctx
        ctx._target_dir = 'etc/conf.d'
        target_conf = '/%(_target_dir)s/celeryd-%(site)s' % ctx
        source_conf = '%(source_dir)s/scripts/%(_target_dir)s/celeryd' % ctx
        self._replace_conf(source_conf, target_conf)
        del ctx['_target_dir']  # cleanup
    
    def _update_celeryd_init_sys_config(self):
        self._update_celeryd_options_conf()
        self._update_init_sys_config(
            source_name='celeryd',
            target_name='celery-%s' % self.ctx.site
        )
    
    def _update_project_init_sys_config(self):
        self._update_init_sys_config(
            source_name=self.ctx.project,
            target_name=self.ctx.site
        )
    
    def _update_init_sys_config(self, source_name, target_name):
        ctx = self.ctx
        uses_systemd = ctx.init_system == InitSystem.systemd
        ctx._target_dir = 'etc/systemd/system' if uses_systemd else 'etc/init'
        file_ext = '.service' if uses_systemd else '.conf'
        
        target_conf = ('/%(_target_dir)s/' + target_name) % ctx
        if not target_conf.endswith(file_ext):
            target_conf += file_ext

        source_conf = '%(source_dir)s/scripts/%(_target_dir)s/'
        source_conf = (source_conf + source_name) % ctx
        if not source_conf.endswith(file_ext):
            source_conf += file_ext

        self._replace_conf(source_conf, target_conf)
        del ctx['_target_dir']      # cleanup

    def _update_web_proxy_server_conf(self):
        ctx = self.ctx
        uses_nginx = ctx.web_proxy == WebProxyServer.nginx
        ctx._websvr_dir = 'etc/nginx' if uses_nginx else 'etc/apache2'
        ctx._target_dir = '%(_websvr_dir)s/sites-available' % ctx
        target_conf = '/%(_target_dir)s/%(site)s' % ctx
        source_conf = '%(source_dir)s/scripts/%(_target_dir)s/%(project)s' % ctx
        
        self._replace_conf(source_conf, target_conf)
        # make symbolic link to sites-enabled
        ctx._target_dir = '/%(_websvr_dir)s/sites-enabled' % ctx
        linked_conf = '%(_target_dir)s/%(site)s' % ctx
        if not files.exists(linked_conf):
            sudo('cd %s && ln -s %s %s' % (
                ctx._target_dir, target_conf, ctx.site
            ))
        
        # context cleanup
        del ctx['_target_dir']
        del ctx['_websvr_dir']
    
    def _update_wsgi_server_conf(self):
        self._expand_template(
            '%(source_dir)s/scripts/%(wsgi_conf)s' % self.ctx,
            use_sudo=True
        )


class DjangoFabHelper(FabHelper):
    """A fabric automation helper class with django specific utility functions.
    """

    def __init__(self, project, staging, repo_url, **extras):
        super(DjangoFabHelper, self).__init__(project, staging, repo_url, **extras)
        # update context further
        self.ctx.project_dir = '%(source_dir)s/%(project)s' % self.ctx
        self.ctx.settings_dir = '%(project_dir)s/%(project)s' % self.ctx
        
    def _post_deploy(self):
        self._update_settings_base()
        self._create_settings_file()
        self._execute_management_commands()
        
    def _execute_management_commands(self):
        ctx = self.ctx
        with cd('%(venv_dir)s/bin/' % ctx):
            run('./python %(project_dir)s/manage.py collectstatic --noinput' % ctx)
            run('./python %(project_dir)s/manage.py migrate --noinput' % ctx)

    def _create_settings_file(self):
        ctx = self.ctx
        # create `settings.py` if it doesn't exist
        rfile = '%(settings_dir)s/settings.py' % ctx
        if files.exists(rfile):
            return

        lbase_dir = os.path.dirname(env.real_fabfile)
        lfile = os.path.join(lbase_dir, 'templates', 'settings.py.tpl')
        result = ops.put(lfile, rfile, mode=0o755)
        if not result.succeeded:
            raise Exception('Settings template upload failed.')
        
        secret_key = generate_random()
        if '&' in secret_key:
            hex_chars = '0123456789abcdef'
            secret_key = secret_key.replace('&', random.choice(hex_chars))
        files.sed(rfile, 'SECRET_KEY =.+$', 'SECRET_KEY = "%s"' % secret_key)
        files.sed(rfile, 'ALLOWED_HOSTS =.+$', 'ALLOWED_HOSTS = ["%s"]' % env.host)
        
        db_name, passwd = (ctx.db_name, ctx.db_pwd)
        if self.staging:
            db_name += '_st'

        # update database settings (if any)
        files.sed(rfile, '_DBNAME_.+$', '"NAME": "%s",' % db_name)
        files.sed(rfile, '_DBUSR_.+$', '"USER": "%s",' % ctx.user)
        files.sed(rfile, '_DBPWD_.+$', '"PASSWORD": "%s",' % passwd)

        mdb_name = getattr(ctx, 'mdb_name', db_name)
        mdb_passwd = getattr(ctx, 'mdb_pwd', passwd)
        if mdb_name and self.staging and not mdb_name.endswith('_st'):
            mdb_name += '_st'

        # update mongo settings (if any)
        files.sed(rfile, '^_MONGODB_NAME =.+$', '_MONGODB_NAME = "%s"' % mdb_name)
        files.sed(rfile, '^_MONGODB_USR =.+$', '_MONGODB_USR = "%s"' % ctx.user)
        files.sed(rfile, '^_MONGODB_PWD =.+$', '_MONGODB_PWD = "%s"' % mdb_passwd)

    def _update_settings_base(self):
        ctx = self.ctx
        rfile = '%(settings_dir)s/settings_base.py' % ctx
        files.sed(rfile, 'DEBUG =.+$', 'DEBUG = False')
