import logging
from collections import namedtuple
from pipes import quote
from StringIO import StringIO

from bd2k.util.iterables import concat
from bd2k.util.strings import interpolate as fmt
from fabric.context_managers import settings
from fabric.operations import run

from cgcloud.core.box import fabric_task
from cgcloud.core.cluster import ClusterBox, ClusterLeader, ClusterWorker
from cgcloud.core.common_iam_policies import ec2_read_only_policy
from cgcloud.core.generic_boxes import GenericUbuntuDefaultBox
from cgcloud.core.mesos_box import MesosBox as CoreMesosBox
from cgcloud.core.ubuntu_box import Python27UpdateUbuntuBox
from cgcloud.fabric.operations import sudo, remote_open, pip, sudov, put
from cgcloud.lib.util import abreviated_snake_case_class_name, heredoc

log = logging.getLogger( __name__ )

user = 'mesosbox'

install_dir = '/opt/mesosbox'

log_dir = '/var/log/mesosbox'

ephemeral_dir = '/mnt/ephemeral'

persistent_dir = '/mnt/persistent'

work_dir = '/var/lib/mesos'

Service = namedtuple( 'Service', [
    'init_name',
    'user',
    'description',
    'command' ] )


def mesos_service( name, user, *flags ):
    command = concat( '/usr/sbin/mesos-{name}', '--log_dir={log_dir}/mesos', flags )
    return Service(
        init_name='mesosbox-' + name,
        user=user,
        description=fmt( 'Mesos {name} service' ),
        command=fmt( ' '.join( command ) ) )


mesos_services = dict(
    master=[ mesos_service( 'master', user,
                            '--registry=in_memory',
                            # would use "--ip mesos-master" here but that option only supports
                            # IP addresses, not DNS names or /etc/hosts entries
                            '--ip_discovery_command="hostname -i"',
                            '--credentials=/etc/mesos/credentials' ) ],
    slave=[ mesos_service( 'slave', 'root',
                           '--master=mesos-master:5050',
                           '--no-switch_user',
                           '--work_dir=' + work_dir,
                           '--executor_shutdown_grace_period=60secs',
                           # By default Mesos offers the total disk minus what it reserves for
                           # itself, which is half the total disk or 5GiB whichever is smaller.
                           # The code below mimicks that logic except that it uses available disk
                           # space as opposed to total disk. NB: the default unit is MiB in Mesos.
                           "--resources=disk:$(python -c %s)" % quote( heredoc( """
                               import os
                               df = os.statvfs( "{work_dir}" )
                               free = df.f_frsize * df.f_bavail >> 20
                               print max( 0, free - min( free / 2, 5120 ) )""" ).replace( '\n',
                                                                                          ';' ) ),
                           '$(cat /var/lib/mesos/slave_args)' ) ] )


class MesosBoxSupport( GenericUbuntuDefaultBox, Python27UpdateUbuntuBox, CoreMesosBox ):
    """
    A node in a Mesos cluster. Both slaves and masters are based on this initial setup. Those
    specific roles are determined at boot time. Worker nodes need to be passed the master's IP
    and port before starting up.
    """

    @classmethod
    def get_role_options( cls ):
        return super( MesosBoxSupport, cls ).get_role_options( ) + [
            cls.RoleOption( name='etc_hosts_entries',
                            type=str,
                            repr=str,
                            inherited=True,
                            help="Additional entries for /etc/hosts in the form "
                                 "'foo:1.2.3.4,bar:2.3.4.5'" ) ]

    def other_accounts( self ):
        return super( MesosBoxSupport, self ).other_accounts( ) + [ user ]

    def default_account( self ):
        return user

    def __init__( self, ctx ):
        super( MesosBoxSupport, self ).__init__( ctx )
        self.lazy_dirs = set( )

    def _populate_security_group( self, group_id ):
        return super( MesosBoxSupport, self )._populate_security_group( group_id ) + [
            dict( ip_protocol='tcp', from_port=0, to_port=65535,
                  src_security_group_group_id=group_id ),
            dict( ip_protocol='udp', from_port=0, to_port=65535,
                  src_security_group_group_id=group_id ) ]

    def _get_iam_ec2_role( self ):
        iam_role_name, policies = super( MesosBoxSupport, self )._get_iam_ec2_role( )
        iam_role_name += '--' + abreviated_snake_case_class_name( MesosBoxSupport )
        policies.update( dict(
            ec2_read_only=ec2_read_only_policy,
            ec2_mesos_box=dict( Version="2012-10-17", Statement=[
                dict( Effect="Allow", Resource="*", Action="ec2:CreateTags" ),
                dict( Effect="Allow", Resource="*", Action="ec2:CreateVolume" ),
                dict( Effect="Allow", Resource="*", Action="ec2:AttachVolume" ) ] ) ) )
        return iam_role_name, policies

    def _pre_install_packages( self ):
        super( MesosBoxSupport, self )._pre_install_packages( )
        self.__setup_application_user( )

    @fabric_task
    def __setup_application_user( self ):
        sudo( fmt( 'useradd '
                   '--home /home/{user} '
                   '--create-home '
                   '--user-group '
                   '--shell /bin/bash {user}' ) )

        sudoer_file = heredoc( """
            # CGcloud - MesosBox

            # User rules for ubuntu
            mesosbox ALL=(ALL) NOPASSWD:ALL

            # User rules for ubuntu
            mesosbox ALL=(ALL) NOPASSWD:ALL
            """ )

        sudoer_file_path = '/etc/sudoers.d/89-mesosbox-user'
        put( local_path=StringIO( sudoer_file ), remote_path=sudoer_file_path, use_sudo=True, mode=0440 )
        sudo( "chown root:root '%s'" % sudoer_file_path )

    def _post_install_packages( self ):
        super( MesosBoxSupport, self )._post_install_packages( )
        self._propagate_authorized_keys( user, user )
        self.__setup_shared_dir( )
        self.__setup_ssh_config( )
        self.__create_mesos_keypair( )
        self.__setup_mesos( )
        self.__install_tools( )

    def _shared_dir( self ):
        return '/home/%s/shared' % self.default_account( )

    @fabric_task
    def __setup_shared_dir( self ):
        sudov( 'install', '-d', self._shared_dir( ), '-m', '700', '-o', self.default_account( ) )

    @fabric_task
    def __setup_ssh_config( self ):
        with remote_open( '/etc/ssh/ssh_config', use_sudo=True ) as f:
            f.write( heredoc( """
                Host spark-master
                    CheckHostIP no
                    HashKnownHosts no""" ) )

    @fabric_task( user=user )
    def __create_mesos_keypair( self ):
        self._provide_imported_keypair( ec2_keypair_name=self.__ec2_keypair_name( self.ctx ),
                                        private_key_path=fmt( "/home/{user}/.ssh/id_rsa" ),
                                        overwrite_ec2=True )
        # This trick allows us to roam freely within the cluster as the app user while still
        # being able to have keypairs in authorized_keys managed by cgcloudagent such that
        # external users can login as the app user, too. The trick depends on AuthorizedKeysFile
        # defaulting to or being set to .ssh/autorized_keys and .ssh/autorized_keys2 in sshd_config
        run( "cd .ssh && cat id_rsa.pub >> authorized_keys2" )

    def __ec2_keypair_name( self, ctx ):
        return user + '@' + ctx.to_aws_name( self.role( ) )

    @fabric_task
    def __setup_mesos( self ):
        self._lazy_mkdir( log_dir, 'mesos', persistent=False )
        self._lazy_mkdir( '/var/lib', 'mesos', persistent=True )
        self.__prepare_credentials( )
        self.__register_systemd_jobs( mesos_services )
        self._post_install_mesos( )

    def _post_install_mesos( self ):
        pass

    def __prepare_credentials( self ):
        # Create the credentials file and transfer ownership to mesosbox
        sudo( 'mkdir -p /etc/mesos' )
        sudo( 'echo toil liot > /etc/mesos/credentials' )
        sudo( 'chown mesosbox:mesosbox /etc/mesos/credentials' )

    @fabric_task
    def __install_tools( self ):
        """
        Installs the mesos-master-discovery init script and its companion mesos-tools. The latter
        is a Python package distribution that's included in cgcloud-mesos as a resource. This is
        in contrast to the cgcloud agent, which is a standalone distribution.
        """
        tools_dir = install_dir + '/tools'
        admin = self.admin_account( )
        sudo( fmt( 'mkdir -p {tools_dir}' ) )
        sudo( fmt( 'chown {admin}:{admin} {tools_dir}' ) )
        run( fmt( 'virtualenv --no-pip {tools_dir}' ) )
        run( fmt( '{tools_dir}/bin/easy_install pip==1.5.2' ) )

        with settings( forward_agent=True ):
            with self._project_artifacts( 'mesos-tools' ) as artifacts:
                pip( use_sudo=True,
                     path=tools_dir + '/bin/pip',
                     args=concat( 'install', artifacts ) )
        sudo( fmt( 'chown -R root:root {tools_dir}' ) )

        mesos_tools = "MesosTools(**%r)" % dict( user=user,
                                                 shared_dir=self._shared_dir( ),
                                                 ephemeral_dir=ephemeral_dir,
                                                 persistent_dir=persistent_dir,
                                                 lazy_dirs=self.lazy_dirs )

        self.lazy_dirs = None  # make sure it can't be used anymore once we are done with it

        mesosbox_start_path = '/usr/sbin/mesosbox-start.sh'
        mesosbox_stop_path = '/usr/sbin/mesosbox-stop.sh'
        systemd_heredoc = heredoc( """
            [Unit]
            Description=Mesos master discovery
            Requires=networking.service network-online.target
            After=networking.service network-online.target

            [Service]
            Type=simple
            ExecStart={mesosbox_start_path}
            RemainAfterExit=true
            ExecStop={mesosbox_stop_path}

            [Install]
            WantedBy=multi-user.target
            """ )

        mesosbox_setup_start_script = heredoc( """
                #!/bin/sh
                for i in 1 2 3; do if {tools_dir}/bin/python2.7 - <<END
                import logging
                logging.basicConfig( level=logging.INFO )
                from cgcloud.mesos_tools import MesosTools
                mesos_tools = {mesos_tools}
                mesos_tools.start()
                END
                then exit 0; fi; echo Retrying in 60s; sleep 60; done; exit 1""" )

        mesosbox_setup_stop_script = heredoc ("""
                #!/{tools_dir}/bin/python2.7
                import logging
                logging.basicConfig( level=logging.INFO )
                from cgcloud.mesos_tools import MesosTools
                mesos_tools = {mesos_tools}
                mesos_tools.stop()""" )

        put( local_path=StringIO( mesosbox_setup_start_script ), remote_path=mesosbox_start_path, use_sudo=True )
        sudo( "chown root:root '%s'" % mesosbox_start_path )
        sudo( "chmod +x '%s'" % mesosbox_start_path )

        put( local_path=StringIO( mesosbox_setup_stop_script ), remote_path=mesosbox_stop_path, use_sudo=True )
        sudo( "chown root:root '%s'" % mesosbox_stop_path )
        sudo( "chmod +x '%s'" % mesosbox_stop_path )

        self._register_init_script(
            "mesosbox",
            systemd_heredoc )

        # Enable mesosbox to start on boot
        sudo( "systemctl enable mesosbox" )

        # Explicitly start the mesosbox service to achieve creation of lazy directoriess right
        # now. This makes a generic mesosbox useful for adhoc tests that involve Mesos and Toil.
        self._run_init_script( 'mesosbox' )

    @fabric_task
    def _lazy_mkdir( self, parent, name, persistent=False ):
        """
        _lazy_mkdir( '/foo', 'dir', True ) creates /foo/dir now and ensures that
        /mnt/persistent/foo/dir is created and bind-mounted into /foo/dir when the box starts.
        Likewise, __lazy_mkdir( '/foo', 'dir', False) creates /foo/dir now and ensures that
        /mnt/ephemeral/foo/dir is created and bind-mounted into /foo/dir when the box starts.

        Note that at start-up time, /mnt/persistent may be reassigned  to /mnt/ephemeral if no
        EBS volume is mounted at /mnt/persistent.

        _lazy_mkdir( '/foo', 'dir', None ) will look up an instance tag named 'persist_foo_dir'
        when the box starts and then behave like _lazy_mkdir( '/foo', 'dir', True ) if that tag's
        value is 'True', or _lazy_mkdir( '/foo', 'dir', False ) if that tag's value is False.
        """
        assert self.lazy_dirs is not None
        assert '/' not in name
        assert parent.startswith( '/' )
        for location in (persistent_dir, ephemeral_dir):
            assert location.startswith( '/' )
            assert not location.startswith( parent ) and not parent.startswith( location )
        logical_path = parent + '/' + name
        sudo( 'mkdir -p "%s"' % logical_path )
        self.lazy_dirs.add( (parent, name, persistent) )
        return logical_path

    def __register_systemd_jobs( self, service_map ):
        for node_type, services in service_map.iteritems( ):
            for service in services:
                service_command_path = '/usr/sbin/%s-start.sh' % service.init_name

                put( local_path=StringIO( "#!/bin/sh\n" + service.command ), remote_path=service_command_path, use_sudo=True )
                sudo( "chown root:root '%s'" % service_command_path )
                sudo( "chmod +x '%s'" % service_command_path )

                self._register_init_script(
                    service.init_name,
                    heredoc( """
                        [Unit]
                        Description={service.description}
                        Before=docker.service
                        Wants=docker.service
                        Requires=mesosbox.service
                        After=mesosbox.service

                        [Service]
                        Type=simple
                        ExecStart={service_command_path}
                        User={service.user}
                        Group={service.user}
                        Environment="USER={user}"
                        LimitNOFILE=8000:8192
                        UMask=022

                        [Install]
                        WantedBy=multi-user.target
                        """ ) )

class MesosBox( MesosBoxSupport, ClusterBox ):
    """
    A node in a Mesos cluster; used only to create an image for master and worker boxes
    """
    pass


class MesosMaster( MesosBox, ClusterLeader ):
    """
    The master of a cluster of boxes created from a mesos-box image
    """
    pass


class MesosSlave( MesosBox, ClusterWorker ):
    """
    A slave in a cluster of boxes created from a mesos-box image
    """
    pass
