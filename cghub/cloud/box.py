from __future__ import print_function
from StringIO import StringIO
from contextlib import closing
from functools import partial
from operator import attrgetter
import socket
import subprocess
import time
import sys

from fabric.operations import sudo, run, get, put
from boto import ec2
from fabric.api import execute
from paramiko import SSHClient
from paramiko.client import MissingHostKeyPolicy

from cghub.cloud.environment import Environment
from util import unpack_singleton, prepend_shell_script, camel_to_snake


EC2_POLLING_INTERVAL = 5


def needs_instance(method):
    def wrapped_method(self, *args, **kwargs):
        if self.instance_id is None:
            raise RuntimeError( "Instance ID not set" )
        return method( self, *args, **kwargs )

    return wrapped_method

# FIXME: not thread-safe

wrapped = False


def fabric_task(function):
    def wrapper(box, *args, **kwargs):
        global wrapped
        if wrapped:
            return function( box, *args, **kwargs )
        else:
            wrapped = True
            try:
                return box._execute( partial( function, box, *args, **kwargs ) )
            finally:
                wrapped = False

    return wrapper


class Box( object ):
    """
    Manage EC2 instances. Each instance of this class represents a single virtual machine (aka
    instance) in EC2.
    """

    @classmethod
    def role(cls):
        """
        The name of the role performed by instances of this class, or rather by the EC2 instances
        they represent.
        """
        return camel_to_snake( cls.__name__, '-' )

    def username(self):
        """
        Returns the username for making SSH connections to the instance.
        """
        raise NotImplementedError( )

    def image_id(self):
        """
        Returns the ID of the AMI to boot instances of this box from
        """
        raise NotImplementedError( )

    def setup(self, update=False):
        """
        Create the EC2 instance represented by this box, install OS and additional packages on,
        optionally create an AMI image of it, and/or terminate it.

        :param update:
            Bring the package repository as well as any installed packages up to date, i.e. do
            what on Ubuntu is achieved by doing 'sudo apt-get update ; sudo apt-get upgrade'.
        """
        raise NotImplementedError( )

    def __init__(self, env):
        """
        Initialize an instance of this class. Before calling any of the methods on this object,
        you must ensure that a corresponding EC2 instance exists by calling either create() or
        adopt(). The former creates a new EC2 instance, the latter looks up an existing one.

        :type env: Environment
        """
        self.env = env
        self.instance_id = None
        self.host_name = None
        self.connection = ec2.connect_to_region( env.region )

    def user_data(self):
        """
        Return the EC2 user-data for instances represented by this box
        """
        return None

    def create(self, ssh_key_name, instance_type=None):
        """
        Launch (aka 'run' in EC2 lingo) the EC2 instance represented by this box

        :param instance_type: The type of instance to create, e.g. m1.small or t1.micro.

        :param ssh_key_name: The name of the SSH public key to inject into the instance
        """
        if self.instance_id is not None:
            raise RuntimeError( "Instance already adopted or created" )

        if instance_type is None:
            instance_type = self.recommended_instance_type( )

        self._log( 'Creating instance, ... ', newline=False )

        reservation = self.connection.run_instances( self.image_id( ),
                                                     instance_type=instance_type,
                                                     key_name=ssh_key_name,
                                                     placement=self.env.availability_zone,
                                                     user_data=self.user_data( ) )
        instance = unpack_singleton( reservation.instances )
        self.instance_id = instance.id

        self._post_instance_creation( instance )

        self.__wait_ready( instance, { 'pending' } )

    def _post_instance_creation(self, instance):
        self._log( 'tagging instance, ...', newline=False )
        instance.add_tag( 'Name', self.absolute_role( ) )

    def adopt(self, ordinal=None, wait_ready=True):
        """
        Verify that the EC instance represented by this box exists and, optionally,
        wait until it is ready, i.e. that it is is running, has a public host name and can be
        connected to via SSH. If the box doesn't exist and exception will be raised.

        :param wait_ready: if True, wait for the instance to be ready
        """
        if self.instance_id is None:
            self._log( 'Adopting instance, ... ', newline=False )
            instance = self.__get_instance_by_ordinal( ordinal )
            self.instance_id = instance.id
            if wait_ready:
                self.__wait_ready( instance, from_states={ 'pending' } )
            else:
                self._log( 'done.' )

    def list(self):
        role, instances = self.__list_instances( )
        return [ dict( role=role,
                       ordinal=ordinal,
                       id=instance.id,
                       ip=instance.ip_address,
                       created_at=instance.launch_time,
                       state=instance.state )
            for ordinal, instance in enumerate( instances ) ]

    def __list_instances(self):
        """
        Lookup and return a list of instance performing this box' role

        :return tuple of role name and list of instances
        :rtype: string, list of boto.ec2.instance.Instance
        """
        name = self.absolute_role( )
        reservations = self.connection.get_all_instances( filters={ 'tag:Name': name } )
        instances = [ i for r in reservations for i in r.instances if i.state != 'terminated' ]
        instances.sort( key=attrgetter( 'launch_time' ) )
        return name, instances

    def __get_instance_by_ordinal(self, ordinal):
        """
        Get the n-th instance that performs this box' role

        :param ordinal: the index of the instance based on the ordering by launch_time
        :return:
        """
        role, instances = self.__list_instances( )
        if not instances:
            raise RuntimeError( "No instance performing role '%s'" % role )
        if ordinal is None:
            if len( instances ) > 1:
                raise RuntimeError( "More than one instance performing role '%s'. "
                                    "Please specify an ordinal." % role )
            ordinal = 0
        return instances[ ordinal ]

    @needs_instance
    def create_image(self):
        """
        Create an image (AMI) of the EC2 instance represented by this box and return its ID.
        The EC2 instance needs to use an EBS-backed root volume. The box must be stopped or
        an exception will be raised.
        """
        self.__expect_state( 'stopped' )

        self._log( "Creating image, ... ", newline=False )
        image_name = "%s %s" % ( self.absolute_role( ), time.strftime( '%Y-%m-%d %H-%M-%S' ) )
        image_id = self.connection.create_image( self.instance_id, image_name )
        while True:
            try:
                image = self.connection.get_image( image_id )
                break
            except self.connection.ResponseError as e:
                if e.error_code != 'InvalidAMIID.NotFound':
                    raise
        self.__wait_transition( image, { 'pending' }, 'available' )
        self._log( "done." )
        return image_id

    @needs_instance
    def stop(self):
        """
        Stop the EC2 instance represented by this box. Stopped instances can be started later using
        :py:func:`Box.start`.
        """
        instance = self.__expect_state( 'running' )
        self._log( 'Stopping instance, ... ', newline=False )
        self.connection.stop_instances( [ instance.id ] )
        self.__wait_transition( instance,
                                from_states={ 'running', 'stopping' },
                                to_state='stopped' );
        self._log( 'done.' )

    @needs_instance
    def start(self):
        """
        Start the EC2 instance represented by this box
        """
        instance = self.__expect_state( 'stopped' )
        self._log( 'Starting instance, ... ', newline=False )
        self.connection.start_instances( [ self.instance_id ] )
        # Not 100% sure why from_states includes 'stopped' but I think I noticed that there is a
        # short interval after start_instances returns during which the instance is still in
        # stopped before it goes into pending
        self.__wait_ready( instance, from_states={ 'stopped', 'pending' } )

    @needs_instance
    def reboot(self):
        """
        Reboot the EC2 instance represented by this box. When this method returns,
        the EC2 instance represented by this object will likely have different public IP and
        hostname.
        """
        # There is reboot_instances in the API but reliably detecting the
        # state transitions is hard. So we stop and start instead.
        self.stop( )
        self.start( )

    def terminate(self, wait=True):
        """
        Terminate the EC2 instance represented by this box.
        """
        if self.instance_id is not None:
            instance = self.get_instance( )
            if instance._state != 'terminated':
                self._log( 'Terminating instance, ... ', newline=False )
                self.connection.terminate_instances( [ self.instance_id ] )
                if wait:
                    self.__wait_transition( instance,
                                            from_states={ 'running', 'shutting-down', 'stopped' },
                                            to_state='terminated' )
                self._log( 'done.' )

    def get_attachable_volume(self, name):
        """
        Ensure that an EBS volume of the given name is available in the current availability zone.
        If the EBS volume exists but has been placed into a different zone, or if it is not
        available, an exception will be thrown.

        :param name: the name of the volume
        """
        name = self.env.absolute_name( name )
        volumes = self.connection.get_all_volumes( filters={ 'tag:Name': name } )
        if len( volumes ) < 1: return None
        if len( volumes ) > 1: raise RuntimeError( "More than one EBS volume named %s" % name )
        volume = volumes[ 0 ]
        if volume.status != 'available':
            raise RuntimeError( "EBS volume %s is not available." % name )
        expected_zone = self.env.availability_zone
        if volume.zone != expected_zone:
            raise RuntimeError( "Availability zone of EBS volume %s is %s but should be %s."
                                % (name, volume.zone, expected_zone ) )
        return volume

    def get_or_create_volume(self, name, size, **kwargs):
        """
        Ensure that an EBS volume of the given name is available in the current availability zone.
        If the EBS volume exists but has been placed into a different zone, or if it is not
        available, an exception will be thrown. If the volume does not exist it will be created in
        the current zone with the specified size.

        :param name: the name of the volume
        :param size: the size to be used if it needs to be created
        :param kwargs: additional parameters for boto.connection.create_volume()
        :return: the volume
        """
        name = self.env.absolute_name( name )
        volume = self.get_attachable_volume( name )
        if volume is None:
            self._log( "Creating volume %s, ... " % name, newline=False )
            zone = self.env.availability_zone
            volume = self.connection.create_volume( size, zone, **kwargs )
            self.__wait_volume_transition( volume, { 'creating' }, 'available' )
            volume.add_tag( 'Name', name )
            self._log( 'done.' )
            volume = self.get_attachable_volume( name )
        return volume

    @needs_instance
    def attach_volume(self, volume, device):
        self.connection.attach_volume( volume_id=volume.id,
                                       instance_id=self.instance_id,
                                       device=device )
        self.__wait_volume_transition( volume, { 'available' }, 'in-use' )
        if volume.attach_data.instance_id != self.instance_id:
            raise RuntimeError( "Volume %s is not attached to this instance." )

    def _log(self, string, newline=True):
        if newline:
            print( string, file=sys.stderr )
        else:
            sys.stderr.write( string )
            sys.stderr.flush( )

    @needs_instance
    def _execute(self, task):
        """
        Execute the given Fabric task on the EC2 instance represented by this box
        """
        if not callable( task ): task = task( self )
        host = "%s@%s" % ( self.username( ), self.host_name )
        return execute( task, hosts=[ host ] )[ host ]

    def __expect_state(self, expected_state):
        """
        Raises an exception if the instance represented by this object is not in the given state.
        :param expected_state: the expected state
        :return: the instance
        :rtype: boto.ec2.instance.Instance
        """
        instance = self.get_instance( )
        actual_state = instance.state
        if actual_state != expected_state:
            raise RuntimeError( "Expected instance state %s but got %s"
                                % (expected_state, actual_state) )
        return instance

    @needs_instance
    def get_instance(self):
        """
        Return the EC2 instance API object represented by this box.

        :rtype: boto.ec2.instance.Instance
        """
        reservations = self.connection.get_all_instances( self.instance_id )
        return unpack_singleton( unpack_singleton( reservations ).instances )

    def __wait_ready(self, instance, from_states):
        """
        Wait until the given instance transistions from stopped or pending state to being fully
        running and accessible via SSH.
        """
        self._log( "waiting for instance, ... ", newline=False )
        self.__wait_transition( instance, from_states, 'running' )
        self._log( "running, ... ", newline=False )
        self.__wait_hostname_assigned( instance )
        self._log( "hostname assigned, ... ", newline=False )
        self.__wait_ssh_port_open( )
        self._log( "SSH port open, ... ", newline=False )
        self.__wait_ssh_working( )
        self._log( "SSH working, done." )

    def __wait_hostname_assigned(self, instance):
        """
        Wait until the instances has a public host name assigned to it. Returns a dictionary with
         one entry per instance, mapping its instance ID to its public hostname.
        """
        while True:
            host_name = instance.public_dns_name
            if host_name is not None and len( host_name ) > 0: break
            time.sleep( EC2_POLLING_INTERVAL )
            instance.update( )

        self.host_name = host_name

    def __wait_ssh_port_open(self):
        """
        Wait until the instance represented by this box is accessible via SSH.
        """
        while True:
            s = socket.socket( socket.AF_INET, socket.SOCK_STREAM )
            try:
                s.settimeout( EC2_POLLING_INTERVAL )
                s.connect( (self.host_name, 22) )
                return
            except socket.error:
                pass
            except socket.timeout:
                pass
            finally:
                s.close( )

    class IgnorePolicy( MissingHostKeyPolicy ):
        def missing_host_key(self, client, hostname, key):
            pass

    def __wait_ssh_working(self):
        while True:
            client = SSHClient( )
            try:
                client.load_system_host_keys( )
                client.set_missing_host_key_policy( self.IgnorePolicy( ) )
                client.connect( hostname=self.host_name,
                                username=self.username( ),
                                timeout=EC2_POLLING_INTERVAL )
                stdin, stdout, stderr = client.exec_command( 'echo hi' )
                try:
                    line = stdout.readline( )
                    if line == 'hi\n':
                        return
                    else:
                        raise RuntimeError( )
                finally:
                    stdin.close( )
                    stdout.close( )
                    stderr.close( )
            except RuntimeError:
                raise
            except KeyboardInterrupt:
                raise
            except Exception as e:
                self._log( str( e ) )
            finally:
                client.close( )
            time.sleep( EC2_POLLING_INTERVAL )

    def __wait_volume_transition(self, volume, from_states, to_state):
        """
        Same as :py:meth:`_wait_transition`, but for volumes which use 'status' instead of 'state'.
        """
        self.__wait_transition( volume, from_states, to_state, lambda volume: volume.status )

    def __wait_transition(self, resource, from_states, to_state,
                          state_getter=lambda resource: resource.state):
        """
        Wait until the specified EC2 resource (instance, image, volume, ...) transitions from any
        of the given 'from' states to the specified 'to' state. If the instance is found in a state
        other that the to state or any of the from states, an exception will be thrown.

        :param resource: the resource to monitor
        :param from_states:
            a set of states that the resource is expected to be in before the  transition occurs
        :param to_state: the state of the resource when this method returns
        """
        state = state_getter( resource )
        while state in from_states:
            time.sleep( EC2_POLLING_INTERVAL )
            resource.update( validate=True )
            state = state_getter( resource )
        if state != to_state:
            raise RuntimeError( "Expected state of %s to be '%s' but got '%s'"
                                % ( resource, to_state, state ) )

    def _config_file_path(self, file_name, mkdir=False, role=None):
        """
        Returns the path to a role-specific config file.

        :param file_name: the desired file name
        :param mkdir: ensure that the directies in the returned path exist
        :return: the absolute path of the config file
        """
        if role is None: role = self.role( )
        return self.env.config_file_path( [ role, file_name ], mkdir=mkdir )


    def _read_config_file(self, file_name, **kwargs):
        """
        Returns the contents of the given config file. Accepts the same parameters as
        self._config_file_path() with the exception of 'mkdir' which must be omitted.
        """
        path = self._config_file_path( file_name, mkdir='False', **kwargs )
        with open( path, 'r' ) as file:
            return file.read( )

    @needs_instance
    def ssh(self, user=None):
        subprocess.call( self._ssh_args( user ) )

    def _ssh_args(self, user):
        return [ 'ssh', '-l', user if user else self.username( ), self.host_name ]

    def get_keys(self):
        """
        Download the public keys that identify users on the instance.
        """
        pass

    def absolute_role(self):
        return self.env.absolute_name( self.role( ) )

    @fabric_task
    def _propagate_authorized_keys(self, user, group=None):
        """
        Ensure that the given user account accepts SSH connections for the same keys as the
        current user. The current user must have sudo.

        :param user:
            the name of the user to propagate the current user's authorized keys to

        :param group:
            the name of the group that should own the files and directories that are created by
            this method, defaults to the default group of the given user
        """

        if group is None:
            group = run( "getent group $(getent passwd %s | cut -d : -f 4) "
                         "| cut -d : -f 1" % user )
        args = dict( src_user=self.username( ),
                     dst_user=user,
                     dst_group=group )
        sudo( 'install -d ~{dst_user}/.ssh '
              '-m 755 -o {dst_user} -g {dst_group}'.format( **args ) )
        sudo( 'install -t ~{dst_user}/.ssh ~{src_user}/.ssh/authorized_keys '
              '-m 644 -o {dst_user} -g {dst_group}'.format( **args ) )

    def recommended_instance_type(self):
        return 't1.micro'

    def list_images(self):
        role = self.role( )
        image_name_pattern = '%s *' % self.absolute_role( )
        images = self.connection.get_all_images( filters={ 'name': image_name_pattern } )
        images.sort( key=attrgetter( 'name' ) ) # that sorts by date, effectively
        return [ dict( role=role,
                       ordinal=ordinal,
                       name=image.name,
                       id=image.id,
                       state=image.state )
            for ordinal, image in enumerate( images ) ]

    @fabric_task
    def _prepend_remote_shell_script(self, script, remote_path, **put_kwargs):
        """
        Insert the given script into the remote file at the given path before the first script
        line. See prepend_shell_script() for a definition of script line.

        :param script: the script to be inserted
        :param remote_path: the path to the file on the remote host
        :param put_kwargs: arguments passed to Fabric's put operation
        """
        with closing( StringIO( ) ) as out_file:
            with closing( StringIO( ) ) as in_file:
                get( remote_path=remote_path, local_path=in_file )
                in_file.seek( 0 )
                prepend_shell_script( '\n' + script, in_file, out_file )
            out_file.seek( 0 )
            put( remote_path=remote_path, local_path=out_file, **put_kwargs )


