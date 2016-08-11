#!/usr/bin/env python
# encoding: utf-8

'''
Class for creating CAS sessions

'''

from __future__ import print_function, division, absolute_import, unicode_literals

import contextlib
import copy
import json
import os
import re
import six
import weakref
from . import rest
from .. import clib
from .. import config as cf
from ..exceptions import SWATError, SWATCASActionError
from ..utils.config import subscribe, get_option
from ..clib import errorcheck
from ..utils.compat import (a2u, a2n, int32, int64, float64, text_types,
                            binary_types, items_types, int_types)
from ..utils import getsoptions
from ..utils.args import iteroptions
from ..formatter import SASFormatter
from .actions import CASAction, CASActionSet
from .table import CASTable
from .transformers import py2cas
from .request import CASRequest
from .response import CASResponse
from .results import CASResults
from .utils.params import ParamManager, ActionParamManager

# pylint: disable=W0212


def _option_handler(key, value):
    ''' Handle option changes '''
    sessions = list(CAS.sessions.values())

    key = key.lower()
    if key == 'cas.print_messages':
        key = 'print_messages'
    elif key == 'cas.trace_actions':
        key = 'trace_actions'
    elif key == 'cas.trace_ui_actions':
        key = 'trace_ui_actions'
    else:
        return

    for ses in sessions:
        ses._set_option(**{key: value})


subscribe(_option_handler)


def _lower_actionset_keys(asinfo):
    '''
    Lowercase action set information keys

    Parameters
    ----------
    asinfo : dict
       Action set reflection information

    Returns
    -------
    dict
       Same dictionary with lower-cased action / param keys

    '''
    for act in asinfo.get('actions', []):
        act['name'] = act['name'].lower()
        for param in act.get('params', []):
            param['name'] = param['name'].lower()
            if 'parmList' in param:
                param['parmList'] = _lower_parmlist_keys(param['parmList'])
            if 'exemplar' in param:
                param['exemplar'] = _lower_parmlist_keys(param['exemplar'])
    return asinfo


def _lower_parmlist_keys(parmlist):
    '''
    Lowercase parmList/exemplar keys

    Parameters
    ----------
    parmlist : list
       parmList or exemplar reflection information

    Returns
    -------
    list
       Same list with lower-cased name keys

    '''
    for parm in parmlist:
        parm['name'] = parm['name'].lower()
        if 'parmList' in parm:
            parm['parmList'] = _lower_parmlist_keys(parm['parmList'])
        if 'exemplar' in parm:
            parm['exemplar'] = _lower_parmlist_keys(parm['exemplar'])
    return parmlist


@six.python_2_unicode_compatible
class CAS(object):
    '''
    Create a connection to a CAS server.

    Parameters
    ----------
    hostname : string or list-of-strings, optional
        Host to connect to.  If not specified, the value will come
        from the `cas.hostname` option or ``CASHOST`` environment variable.
    port : int or long, optional
        Port number.  If not specified, the value will come from the
        `cas.port` option or ``CASPORT`` environment variable.
    username : string, optional
        Name of user on CAS host.
    password : string, optional
        Password of user on CAS host.
    session : string, optional
        ID of existing session to reconnect to.
    locale : string, optional
        Name of locale used for the session.
    name : string, optional
        User-definable name for the session.
    nworkers : int or long, optional
        Number of worker nodes to use.
    authinfo : string or list-of-strings, optional
        The filename or list of filenames of authinfo/netrc files used
        for authentication.
    protocol : string, optional
        The protocol to use for communicating with the server.
        This protocol must match the protocol spoken by the specified
        server port.  If not specified, the value will come from the
        `cas.protocol` option or ``CASPROTOCOL`` environment variable.
    **kwargs : any, optional
        Arbitrary keyword arguments used for internal purposes only.

    Raises
    ------
    IOError
        When a connection can not be established.

    Returns
    -------
    :class:`CAS` object

    Examples
    --------
    To create a connection to a CAS host, you simply supply a hostname
    (or list of hostnames), a port number, and user credentials.  Here is
    an example specifying a single hostname, and username and password as
    strings.

    >>> conn = swat.CAS('mycashost.com', 12345, 'username', 'password')

    If you use an authinfo file and it is in your home directory, you don't
    have to specify any username or password.  You can override the authinfo
    file location with the authinfo= parameter.  This form also works for 
    Kerberos authentication.

    >>> conn = swat.CAS('mycashost.com', 12345)

    If you specify multiple hostnames, it will connect to the first available
    server in the list.

    >>> conn = swat.CAS(['mycashost1.com', 'mycashost2.com', 'mycashost3.com'],
                        12345, 'username', 'password')

    To connect to an existing CAS session, you specify the session identifier.

    >>> conn = swat.CAS('mycashost.com', 12345,
                        session='ABCDEF12-ABCD-EFG1-2345-ABCDEF123456')

    If you wish to change the locale used on the server, you can use the
    locale= option.

    >>> conn = swat.CAS('mycashost.com', 12345, locale='es_US')

    To limit the number of worker nodes in a grid, you use the nworkers= 
    parameter.

    >>> conn = swat.CAS('mycashost.com', 12345, nworkers=4)

    '''
    trait_names = None  # Block IPython's look of this
    sessions = weakref.WeakValueDictionary()
    _sessioncount = 1

    def __init__(self, hostname=None, port=None, username=None, password=None,
                 session=None, locale=None, nworkers=None,
                 name=None, authinfo=None, protocol=None, **kwargs):

        if hostname is None:
            hostname = cf.get_option('cas.hostname')
        if port is None:
            port = cf.get_option('cas.port')

        # Detect protocol
        if hasattr(hostname, 'startswith') and (hostname.startswith('http:') or
                                                hostname.startswith('https:')):
            protocol = hostname.split(':', 1)[0]
        else:
            protocol = self._detect_protocol(hostname, port, protocol=protocol)

        # Use the prototype to make a copy
        prototype = kwargs.get('prototype')
        if prototype is not None:
            soptions = prototype._soptions
        else:
            soptions = getsoptions(session=session, locale=locale,
                                   nworkers=nworkers, protocol=protocol)

        try:
            if protocol in ['http', 'https']:
                self._sw_error = rest.REST_CASError(a2n(soptions))
            else:
                self._sw_error = clib.SW_CASError(a2n(soptions))
        except SystemError:
            raise SWATError('Could not create CAS object. Check your TK path setting.')

        # Make the connection
        try:
            if prototype is not None:
                self._sw_connection = errorcheck(prototype._sw_connection.copy(),
                                                 prototype._sw_connection)
            else:
                if isinstance(hostname, items_types):
                    hostname = ' '.join(a2n(x) for x in hostname if x)
                if authinfo is not None and password is None:
                    password = ''
                    if not isinstance(authinfo, items_types):
                        authinfo = [authinfo]
                    for item in authinfo:
                        password += '{%s}' % item
                    password = 'authinfo={%s}' % password
                if protocol in ['http', 'https']:
                    self._sw_connection = rest.REST_CASConnection(a2n(hostname),
                                                                  int(port),
                                                                  a2n(username),
                                                                  a2n(password),
                                                                  a2n(soptions),
                                                                  self._sw_error)
                else:
                    self._sw_connection = clib.SW_CASConnection(a2n(hostname),
                                                                int(port),
                                                                a2n(username),
                                                                a2n(password),
                                                                a2n(soptions),
                                                                self._sw_error)
                if self._sw_connection is None:
                    raise SystemError
        except SystemError:
            raise SWATError(self._sw_error.getLastErrorMessage())

        errorcheck(self._sw_connection.setZeroIndexedParameters(), self._sw_connection)

        # Get instance structure values from C layer
        self._hostname = errorcheck(
            a2u(self._sw_connection.getHostname(), 'utf-8'), self._sw_connection)
        self._port = errorcheck(self._sw_connection.getPort(), self._sw_connection)
        self._username = errorcheck(
            a2u(self._sw_connection.getUsername(), 'utf-8'), self._sw_connection)
        self._session = errorcheck(
            a2u(self._sw_connection.getSession(), 'utf-8'), self._sw_connection)
        self._soptions = errorcheck(
            a2u(self._sw_connection.getSOptions(), 'utf-8'), self._sw_connection)
        self._protocol = protocol
        if name:
            self._name = a2u(name)
        else:
            self._name = 'py-session-%d' % type(self)._sessioncount
            type(self)._sessioncount = type(self)._sessioncount + 1

        # Caches for action classes and reflection information
        self._action_classes = {}
        self._action_info = {}
        self._actionset_classes = {}
        self._actionset_info = {}

        # Dictionary of result hook functions
        self._results_hooks = {}

        # Preload __dir__ information.  It will be extended later with action names
        self._dir = set([x for x in self.__dict__.keys() if not x.startswith('_')])

        # Pre-populate action set attributes
        for asname, value in self.retrieve('builtins.help',
                                           showhidden=True,
                                           _messagelevel='error',
                                           _apptag='UI').items():
            self._actionset_classes[asname.lower()] = None
            for actname in value['name']:
                self._action_classes[asname.lower() + '.' + actname.lower()] = None
                self._action_classes[actname.lower()] = None

        # Populate CASTable documentation and method signatures
        CASTable._bootstrap(self)
        init = CASTable.__init__
        if hasattr(init, '__func__'):
            init = init.__func__
        self.CASTable.__func__.__doc__ = init.__doc__

        # Add loadactionset handler to populate actionset and action classes
        def handle_loadactionset(conn, results):
            ''' Force the creation of actionset and action classes '''
            if 'actionset' in results:
                conn.__getattr__(results['actionset'], atype='actionset')

        self.add_results_hook('builtins.loadactionset', handle_loadactionset)

        # Set the session name
        for resp in self._invoke_without_signature('session.sessionname',
                                                   name=self._name,
                                                   _messagelevel='error',
                                                   _apptag='UI'):
            pass

        # Set options
        self._set_option(print_messages=cf.get_option('cas.print_messages'))
        self._set_option(trace_actions=cf.get_option('cas.trace_actions'))
        self._set_option(trace_ui_actions=cf.get_option('cas.trace_ui_actions'))

        # Add the connection to a global dictionary for use by IPython notebook
        type(self).sessions[self._session] = self
        type(self).sessions[self._name] = self

        def _id_generator():
            ''' Generate unique IDs within a connection '''
            num = 0
            while True:
                yield num
                num = num + 1
        self._id_generator = _id_generator()

    def _gen_id(self):
        ''' Generate an ID unique to the session '''
        import numpy
        return numpy.base_repr(next(self._id_generator), 36)

    def _detect_protocol(self, hostname, port, protocol=None):
        '''
        Detect the protocol type for the given host and port

        Parameters
        ----------
        hostname : string
            The CAS host to connect to.
        port : int
            The CAS port to connect to.
        protocol : string, optional
            The protocol override value.

        Returns
        -------
        string
            'cas' or 'http'

        '''
        if protocol is None:
            protocol = cf.get_option('cas.protocol')

        # Try to detect the proper protocol
        if protocol == 'auto':

            from six.moves import urllib

#           for ptype in ['http', 'https']:
            for ptype in ['http']:
                try:
                    req = urllib.request.Request('%s://%s:%s/cas' %
                                                 (ptype, hostname, port))
                    with urllib.request.urlopen(req) as res:
                        pass
                except urllib.error.HTTPError as err:
                    protocol = ptype
                    break
                except Exception as err:
                    pass

            if protocol == 'auto':
                protocol = 'cas'

        return protocol

    def __enter__(self):
        ''' Enter a context '''
        return self

    def __exit__(self, type, value, traceback):
        ''' Exit the context '''
        self.retrieve('session.endsession', _apptag='UI', _messagelevel='error')
        self.close()

    @contextlib.contextmanager
    def session_context(self, *args, **kwargs):
        '''
        Create a context of session options

        This method is intended to be used in conjunction with Python's
        ``with`` statement.  It allows you to set CAS session options within
        that context, then revert them back to their previous state.

        For all possible session options, see the `sessionprop.getsessopt`
        CAS action documentation.

        Parameters
        ----------
        *args : string / any pairs
            Name / value pairs of options in consecutive arguments, name / value
            pairs in tuples, or dictionaries.
        **kwargs : string / any pairs
            Key / value pairs of session options

        Examples
        --------
        >>> conn = swat.CAS()
        >>> print(conn.getsessopt('locale').locale)
        en_US
        >>> with conn.session_context(locale='fr'):
        ...     print(conn.getsessopt('locale').locale)
        fr
        >>>  print(conn.getsessopt('locale').locale)
        en_US

        '''
        state = {}
        newkwargs = {}
        for key, value in iteroptions(*args, **kwargs):
            state[key.lower()] = list(self.retrieve('sessionprop.getsessopt',
                                                    _messagelevel='error',
                                                    _apptag='UI',
                                                    name=key.lower()).values())[0]
            newkwargs[key.lower()] = value
        self.retrieve('sessionprop.setsessopt', _messagelevel='error',
                      _apptag='UI', **newkwargs)
        yield
        self.retrieve('sessionprop.setsessopt', _messagelevel='error',
                      _apptag='UI', **state)

    def get_action_names(self):
        '''
        Return the list of action classes

        Returns
        -------
        list of strings

        '''
        return self._action_classes.keys()

    def get_actionset_names(self):
        '''
        Return the list of actionset classes

        Returns
        -------
        list of strings

        '''
        return self._actionset_classes.keys()

    def has_action(self, name):
        '''
        Does the given action name exist?

        Parameters
        ----------
        name : string
            The name of the CAS action to look for.

        Returns
        -------
        boolean

        '''
        return name in self._action_classes

    def has_actionset(self, name):
        ''' 
        Does the given actionset name exist?

        Parameters
        ----------
        name : string
            The name of the CAS action set to look for.
 
        Returns
        -------
        boolean

        '''
        return name in self._actionset_classes

    def get_action(self, name):
        '''
        Get the CAS action instance for the given action name

        Parameters
        ----------
        name : string
            The name of the CAS action to look for.

        Returns
        -------
        :class:`CASAction` object

        '''
        return self.__getattr__(name, atype='action')

    def get_action_class(self, name):
        '''
        Get the CAS action class for the given action name

        Parameters
        ----------
        name : string
            The name of the CAS action to look for.

        Returns
        -------
        :class:`CASAction`

        '''
        return self.__getattr__(name, atype='action_class')

    def get_actionset(self, name):
        '''
        Get the CAS action set instance for the given action set name

        Parameters
        ----------
        name : string
            The name of the CAS action set to look for.

        Returns
        -------
        :class:`CASActionSet` object

        '''
        return self.__getattr__(name, atype='actionset')

    def __dir__(self):
        return list(self._dir) + list(self.get_action_names())

    def __str__(self):
        args = []
        args.append(repr(self._hostname))
        args.append(repr(self._port))
        if self._username:
            args.append(repr(self._username))
        return 'CAS(%s, protocol=%s, name=%s, session=%s)' % (', '.join(args),
                                                              repr(self._protocol),
                                                              repr(self._name),
                                                              repr(self._session))

    def __repr__(self):
        return str(self)

    def CASTable(self, name, **kwargs):
        '''
        Create a CASTable instance

        The primary difference between constructing a :class:`CASTable`
        object through this method rather than directly, is that the
        current session will be automatically registered with the
        :class:`CASTable` object so that CAS actions can be called on
        it directly.

        Parameters
        ----------
        name : string
           Name of the table in CAS.
        **kwargs : any, optional
           Arbitrary keyword arguments.  These keyword arguments are
           passed to the :class:`CASTable` constructor.

        Returns
        -------
        :class:`CASTable` object

        '''
        table = CASTable(name, **kwargs)
        table.set_connection(self)
        return table

    def SASFormatter(self):
        '''
        Create a SASFormatter instance

        :class:`SASFormatters` can be used to format Python values using
        SAS data formats.

        Returns
        -------
        :class:`SASFormatter` object

        '''
        return SASFormatter(soptions=self._soptions)

    def add_results_hook(self, name, func):
        '''
        Add a post-processing function for results

        The function will be called with two arguments: the CAS connection
        object and the :class:`CASResult` object.

        Parameters
        ----------
        name : string
           Full name of action (actionset.actionname)
        func : function
           Function to call for result set

        See Also
        --------
        :meth:`del_results_hook`
        :meth:`del_results_hooks`

        Examples
        --------
        To add a post-processing function for a particular action, you
        specify the fully-qualified action name and a function.

        >>> def myfunc(connection, results):
               if results and results.get('num'):
                  results['num'] = math.abs(results['num'])
               return results
        >>>
        >>> s.add_results_hook('myactionset.myaction', myfunc)

        '''
        name = name.lower()
        if name not in self._results_hooks:
            self._results_hooks[name] = []
        self._results_hooks[name].append(func)

    def del_results_hook(self, name, func):
        '''
        Delete a post-processing function for an action

        Parameters
        ----------
        name : string
           Full name of action (actionset.actionname)
        func : function
           The function to remove

        See Also
        --------
        :meth:`add_results_hook`
        :meth:`del_results_hooks`

        Examples
        --------
        To remove a post-processing hook from an action, you must specify the
        action name as well as the function to remove.  This is due to the fact
        that multiple functions can be registered to a particular action.

        >>> s.del_results_hook('myactionset.myaction', myfunc)

        '''
        name = name.lower()
        if name in self._results_hooks:
            self._results_hooks[name] = [x for x in self._results_hooks[name]
                                         if x is not func]

    def del_results_hooks(self, name):
        '''
        Delete all post-processing functions for an action

        Parameters
        ---------
        name : string
           Full name of action (actionset.actionname)

        See Also
        --------
        :meth:`add_results_hook`
        :meth:`del_results_hook`

        Examples
        --------
        The following code removes all post-processing functions registered to
        the `myactionset.myaction` action.

        >>> s.del_results_hooks('myactionset.myaction')

        '''
        name = name.lower()
        if name in self._results_hooks:
            del self._results_hooks[name]

    def close(self):
        ''' Close the CAS connection '''
        errorcheck(self._sw_connection.close(), self._sw_connection)

    def _set_option(self, **kwargs):
        '''
        Set connection options

        Parameters
        ---------
        **kwargs : any
           Arbitrary keyword arguments.  Each key/value pair will be
           set as a connection option.

        Returns
        -------
        True
            If all options were set successfully

        '''
        for name, value in six.iteritems(kwargs):
            name = str(name)
            typ = errorcheck(self._sw_connection.getOptionType(name),
                             self._sw_connection)
            try:
                if typ == 'boolean':
                    if value is True or value == 1 or value is False or value == 0:
                        errorcheck(self._sw_connection.setBooleanOption(name,
                                                                        value and 1 or 0),
                                   self._sw_connection)
                    else:
                        raise SWATError('%s is not a valid boolean value' % value)
                elif typ == 'string':
                    if isinstance(value, binary_types) or isinstance(value, text_types):
                        errorcheck(self._sw_connection.setStringOption(name, a2n(value)),
                                   self._sw_connection)
                    else:
                        errorcheck(self._sw_connection.setStringOption(name, value),
                                   self._sw_connection)
                elif typ == 'int32':
                    errorcheck(self._sw_connection.setInt32Option(name, int32(value)),
                               self._sw_connection)
                elif typ == 'int64':
                    errorcheck(self._sw_connection.setInt64Option(name, int64(value)),
                               self._sw_connection)
                elif typ == 'double':
                    errorcheck(self._sw_connection.setDoubleOption(name, float64(value)),
                               self._sw_connection)
            except TypeError:
                raise SWATError('%s is not the correct type' % value)
        return True

    def copy(self):
        '''
        Create a copy of the connection

        The copy of the connection will use the same parameters as this
        object, but it will create a new session.

        Examples
        --------
        >>> conn = swat.CAS()
        >>> print(conn)
        CAS(..., session='76dd2bbe-de65-554f-a94f-a5e0e1abfdc8')

        >>> conn2 = conn.copy()
        >>> print(conn2)
        CAS(..., session='19cef586-6997-ae40-b62c-036f44cb60fc')

        See Also
        --------
        :meth:`fork`

        Returns
        -------
        :class:`CAS` object

        '''
        return type(self)(None, None, prototype=self)

    def fork(self, num=2):
        '''
        Create multiple copies of a connection

        The copies of the connection will use the same parameters as this
        object, but each will create a new session.

        .. note:: The first element in the returned list is the same object that
                  the method was called on.  You only get `num`-1 copies.

        Parameters
        ----------
        num : int, optional
           Number of returned connections.  The first element of the returned
           list is always the object that the fork method was called on.

        Examples
        --------
        The code below demonstrates how to get four unique connections.

        >>> conn = swat.CAS()
        >>> c1, c2, c3, c4 = conn.fork(4)
        >>> c1 is conn
        True
        >>> c2 is conn
        False

        See Also
        --------
        :meth:`copy`

        Returns
        -------
        list of :class:`CAS` objects

        '''
        output = [self]
        for i in range(1, num):
            output.append(self.copy())
        return output

    def _invoke_without_signature(self, _name_, **kwargs):
        '''
        Call an action on the server

        Parameters
        ----------
        _name_ : string
            Name of the action.

        **kwargs : any, optional
            Arbitrary keyword arguments.

        Returns
        -------
        :obj:`self`

        '''
        if isinstance(self._sw_connection, rest.REST_CASConnection):
            errorcheck(self._sw_connection.invoke(a2n(_name_), kwargs),
                       self._sw_connection)
        else:
            errorcheck(self._sw_connection.invoke(a2n(_name_),
                                                  py2cas(self._soptions,
                                                         self._sw_error, **kwargs)),
                       self._sw_connection)
        return self

    def _merge_param_args(self, parmlist, kwargs, action=None):
        '''
        Merge keyword arguments into a parmlist

        This method modifies the parmlist *in place*.

        Parameters
        ----------
        parmlist : list
            Parameter list.
        kwargs : dict
            Dictionary of keyword arguments.
        action : string
            Name of the action.

        '''
        if action is None:
            action = ''

        if isinstance(kwargs, ParamManager):
            kwargs = copy.deepcopy(kwargs.params)

        # Short circuit if we can
        if not isinstance(kwargs, dict):
            return

        # See if we have a caslib= parameter
        caslib = False
        for param in parmlist:
            if param['name'] == 'caslib':
                caslib = True
                break

        # kwargs preserving case
        casekeys = {k.lower(): k for k in kwargs.keys()}

        # Add support for CASTable objects
        inputs = None
        fetch = {}
        uses_inputs = False
        uses_fetchvars = False
        for param in parmlist:
            key = param['name']
            key = casekeys.get(key, key)
            key_lower = key.lower()

            # Check for inputs= / fetchvars= parameters
            if key_lower == 'inputs':
                uses_inputs = True
            elif key_lower == 'fetchvars':
                uses_fetchvars = True

            # Get table object if it exists
            tbl = kwargs.get('__table__', None)

            # Convert table objects to the proper form based on the argument type
            if key in kwargs and isinstance(kwargs[key], CASTable):
                if param.get('isTableDef'):
                    inputs = kwargs[key].get_inputs_param()
                    fetch = kwargs[key].get_fetch_params()
                    kwargs[key] = kwargs[key].to_table_params()
                elif param.get('isTableName'):
                    inputs = kwargs[key].get_inputs_param()
                    fetch = kwargs[key].get_fetch_params()
                    # Fill in caslib= first
                    if caslib and 'caslib' not in kwargs and \
                            kwargs[key].has_param('caslib'):
                        kwargs['caslib'] = kwargs[key].get_param('caslib')
                    kwargs[key] = kwargs[key].to_table_name()
                elif param.get('isOutTableDef'):
                    kwargs[key] = kwargs[key].to_outtable_params()
                elif param.get('isCasLib') and kwargs[key].has_param('caslib'):
                    kwargs[key] = kwargs[key].get_param('caslib')

            # If a string is given for a table object, convert it to a table object
            elif key in kwargs and isinstance(kwargs[key], text_types) and \
                    param.get('isTableDef'):
                kwargs[key] = {'name': kwargs[key]}

            elif tbl is not None and param.get('isTableDef') and key_lower == 'table':
                inputs = tbl.get_inputs_param()
                fetch = tbl.get_fetch_params()
                kwargs[key] = tbl.to_table_params()

            elif tbl is not None and param.get('isTableName') and key_lower == 'name':
                inputs = tbl.get_inputs_param()
                fetch = tbl.get_fetch_params()
                if caslib and 'caslib' not in kwargs and tbl.has_param('caslib'):
                    kwargs['caslib'] = tbl.get_param('caslib')
                kwargs[key] = tbl.to_table_name()

            # Workaround for columninfo which doesn't define table= as
            # a table definition.
            elif tbl is not None and key_lower == 'table' and \
                    action.lower() in ['columninfo', 'table.columninfo']:
                inputs = tbl.get_inputs_param()
                fetch = tbl.get_fetch_params()
                kwargs[key] = tbl.to_table_params()
                if inputs:
                    if 'vars' not in kwargs:
                         kwargs[key]['vars'] = inputs
                inputs = None 

        # Apply input variables
        if uses_inputs and inputs and 'inputs' not in kwargs:
            kwargs['inputs'] = inputs
        elif uses_fetchvars and inputs and 'fetchvars' not in kwargs:
            kwargs['fetchvars'] = inputs

        # Apply fetch parameters
        if fetch and action.lower() in ['fetch', 'table.fetch']:
            for key, value in fetch.items():
                if key in kwargs:
                    continue
                if key == 'sortby' and ('orderby' in kwargs or 'orderBy' in kwargs):
                    continue
                kwargs[key] = value

        kwargs.pop('__table__', None)

        # Workaround for tableinfo which aliases table= to name=, but
        # the alias is hidden.
        if action.lower() in ['tableinfo', 'table.tableinfo'] and 'table' in kwargs:
            if isinstance(kwargs['table'], CASTable):
                kwargs['table'] = kwargs['table'].to_table_params()
            if isinstance(kwargs['table'], dict):
                if caslib and 'caslib' not in kwargs and \
                       kwargs['table'].get('caslib'):
                    kwargs['caslib'] = kwargs['table']['caslib']
                kwargs['table'] = kwargs['table']['name']

        # Add current value fields in the signature
        for param in parmlist:
            if param['name'] in kwargs:
                if 'parmList' in param:
                    self._merge_param_args(param['parmList'], kwargs[param['name']],
                                           action=action)
                else:
                    if isinstance(kwargs[param['name']], text_types):
                        param['value'] = kwargs[param['name']].replace('"', '\\u0022')
                    # TODO: This should only happen for binary inputs (i.e., never)
                    elif isinstance(kwargs[param['name']], binary_types):
                        param['value'] = kwargs[param['name']].replace('"', '\\u0022')
                    else:
                        param['value'] = kwargs[param['name']]

    def _get_action_params(self, name, kwargs):
        '''
        Get additional parameters associated with the given action

        Parameters
        ----------
        name : string
            Name of the action being executed.
        kwargs : dict
            Action parameter dictionary.

        Returns
        -------
        dict
            The new set of action parameters.

        '''
        newkwargs = kwargs.copy()
        for key, value in six.iteritems(kwargs):
            if isinstance(value, ActionParamManager):
                newkwargs.update(value.get_action_params(name, {}))
        return newkwargs

    def _invoke_with_signature(self, _name_, **kwargs):
        '''
        Call an action on the server

        Parameters
        ----------
        _name_ : string
            Name of the action.
        **kwargs : any, optional
            Arbitrary keyword arguments.

        Returns
        -------
        dict
            Signature of the action

        '''
        # Get the signature of the action
        signature = self._get_action_info(_name_)[-1]

        # Check for additional action parameters
        kwargs = self._get_action_params(_name_, kwargs)

        if signature:
            signature = copy.deepcopy(signature)
            kwargs = copy.deepcopy(kwargs)
            self._merge_param_args(signature.get('params', {}), kwargs, action=_name_)

        self._invoke_without_signature(_name_, **kwargs)

        return signature

    def upload(self, data, importoptions=None, resident=None,
               promote=None, casout=None):
        '''
        Upload data from a local file into a CAS table

        This method is a thin wrapper around the `table.upload` CAS action.
        The primary difference between this data loader and the other data
        loaders on this class is that, in this case, the parsing of the data
        is done on the server.  This method simply uploads the file as 
        binary data which is then parsed by `table.loadtable` on the server.

        While the server parsers may not be quite a flexible as Python, they
        are generally much faster.  Files such as CSV can be parsed on the 
        server in multiple threads across many machines in the grid.

        Notes
        -----
        This method uses paths that are on the **client side**.  This means 
        you need to use paths to files that are **on the same machine that Python
        is running on**.  If you want to load files from the CAS server side, you
        would use the `table.loadtable` action.

        Also, when uploading a :class:`pandas.DataFrame`, the data is exported to
        a CSV file, then the CSV file is uploaded.  This can cause a loss of
        metadata about the columns since the server parser will guess at the
        data types of the columns.  You can use `importoptions=` to specify more
        information about the data.

        Parameters
        ----------
        data : string or :class:`pandas.DataFrame`
            If the value is a string, it can be either a filename
            or a URL.  DataFrames will be converted to CSV before
            uploading.
        importoptions : dict, optional
            Import options for the table.upload action.
        resident : boolean, optional
            Internal use only.
        promote : boolean, optional
            Should the resulting table be in the global namespace?
        casout : dict, optional
            Output table definition for the `table.upload` action.

        Examples
        --------
        >>> conn = swat.CAS()
        >>> out = conn.upload('data/iris.csv')
        >>> tbl = out.casTable
        >>> print(tbl.head())
           sepal_length  sepal_width  petal_length  petal_width species
        0           5.1          3.5           1.4          0.2  setosa
        1           4.9          3.0           1.4          0.2  setosa
        2           4.7          3.2           1.3          0.2  setosa
        3           4.6          3.1           1.5          0.2  setosa
        4           5.0          3.6           1.4          0.2  setosa

        Returns
        -------
        :class:`CASResults`

        '''
        delete = False
        name = None

        import pandas as pd
        if isinstance(data, pd.DataFrame):
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False, suffix='.csv') as tmp:
                delete = True
                filename = tmp.name
                name = os.path.splitext(os.path.basename(filename))[0]
                data.to_csv(filename, encoding='utf-8', index=False)

        elif data.startswith('http://') or \
                data.startswith('https://') or \
                data.startswith('ftp://'):
            import tempfile
            from six.moves.urllib.request import urlopen
            from six.moves.urllib.parse import urlparse
            parts = urlparse(data)
            ext = os.path.splitext(parts.path)[-1].lower()
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
                delete = True
                tmp.write(urlopen(data).read())
                filename = tmp.name
                if parts.path:
                    name = os.path.splitext(parts.path.split('/')[-1])[0]
                else:
                    name = os.path.splitext(os.path.basename(filename))[0]

        else:
            filename = data
            name = os.path.splitext(os.path.basename(filename))[0]

        # TODO: Populate docstring with table.upload action help
        filetype = {
            'sav': 'spss',
            'xlsx': 'excel',
            'sashdat': 'hdat',
            'sas7bdat': 'basesas',
        }

        kwargs = {}

        if importoptions is None:
            importoptions = {}
        if isinstance(importoptions, (dict, ParamManager)) and \
                'filetype' not in [x.lower() for x in importoptions.keys()]:
            ext = os.path.splitext(filename)[-1][1:].lower()
            if ext in filetype:
                importoptions['filetype'] = filetype[ext]
            elif len(ext) == 3 and ext.endswith('sv'):
                importoptions['filetype'] = 'csv'
        kwargs['importoptions'] = importoptions

        if casout is None:
            casout = {}
        if isinstance(casout, (dict, ParamManager)) and 'name' not in casout:
            casout['name'] = name
        kwargs['casout'] = casout

        if resident is not None:
            kwargs['resident'] = resident

        if promote is not None:
            kwargs['promote'] = promote

        if isinstance(self._sw_connection, rest.REST_CASConnection):
            resp = self._sw_connection.upload(a2n(filename), kwargs)
        else:
            resp = errorcheck(self._sw_connection.upload(a2n(filename),
                                                         py2cas(self._soptions,
                                                                self._sw_error, **kwargs)),
                              self._sw_connection)

        # Remove temporary file as needed
        if delete:
            try:
                os.remove(filename)
            except:
                pass

        return self._get_results([(CASResponse(resp, connection=self), self)])

    def upload_file(self, data, importoptions=None, promote=None, casout=None):
        '''
        Upload a client-side data file to CAS and parse it into a CAS table
        
        Parameters
        ----------
        data : string
            Either a filename or URL.
            or a URL.  DataFrames will be converted to CSV before
            uploading.
        importoptions : dict, optional
            Import options for the table.upload action.
        promote : boolean, optional
            Should the resulting table be in the global namespace?
        casout : dict, optional
            Output table definition for the `table.upload` action.

        Returns
        -------
        :class:`CASTable`

        '''
        out = self.upload(data, importoptions=importoptions,
                          promote=promote, casout=casout)
        if out.severity > 1:
            raise SWATError(out.status)
        return out['casTable']

    def upload_frame(self, data, importoptions=None, promote=None, casout=None):
        '''
        Upload a client-side data file to CAS and parse it into a CAS table
        
        Parameters
        ----------
        data : :class:`pandas.DataFrame`
            DataFrames will be converted to CSV before uploading.
        importoptions : dict, optional
            Import options for the table.upload action.
        promote : boolean, optional
            Should the resulting table be in the global namespace?
        casout : dict, optional
            Output table definition for the `table.upload` action.

        Returns
        -------
        :class:`CASTable`

        '''
        out = self.upload(data, importoptions=importoptions,
                          promote=promote, casout=casout)
        if out.severity > 1:
            raise SWATError(out.status)
        return out['casTable']

    def invoke(self, _name_, **kwargs):
        '''
        Call an action on the server

        The :meth:`invoke` method only calls the action on the server.  It
        does not retrieve the responses.  To get the responses, you iterate
        over the connection object.

        Parameters
        ----------
        _name_ : string
            Name of the action
        **kwargs : any, optional
            Arbitrary keyword arguments

        Returns
        -------
        `self`

        See Also
        --------
        :meth:`retrieve` : Calls action and retrieves results
        :meth:`__iter__` : Iterates over responses

        Examples
        --------
        The code below demonstrates how you invoke an action on the server and
        iterate through the results.

        >>> s.invoke('help')
        <swat.CAS object at 0x7fab0a9031d0>
        >>> for response in s:
        ...     for key, value in response:
        ...         print(key)
        ...         print(value)
        builtins
                      name                                        description
        0          addnode                           Add a node to the server
        1             help                        Lists the available actions
        .
        .
        .

        '''
        self._invoke_with_signature(a2n(_name_), **kwargs)
        return self

    def retrieve(self, _name_, **kwargs):
        '''
        Call the action and aggregate the results

        Parameters
        ----------
        _name_ : string
           Name of the action
        **kwargs : any, optional
           Arbitrary keyword arguments

        Returns
        -------
        :class:`CASResults` object

        See Also
        --------
        :meth:`invoke` : Calls action, but does not retrieve results

        Examples
        --------
        The code below demonstrates how you invoke an action on the server and
        retrieve the results.

        >>> out = s.retrieve('help')
        >>> print(out.keys())
        ['builtins', 'casidx', 'casmeta', 'espact', 'tkacon', 'table', 'tkcsessn',
         'tkcstate']
        >>> print(out['builtins'])
                      name                                        description
        0          addnode                           Add a node to the server
        1             help                        Lists the available actions
        2        listnodes                              List the server nodes
        .
        .
        .


        Status and performance information is also available on the returned object.
        Here is an example of an action call to an action that doesn't exist.

        >>> out = s.retrieve('foo')
        >>> print(out.status)
        'The specified action was not found.'
        >>> print(out.severity)
        2
        >>> print(out.messages)
        ["ERROR: Action 'foo' was not found.",
         'ERROR: The CAS server stopped processing this action because of errors.']

        Here is an example that demonstrates the performance metrics that are available.

        >>> out = s.retrieve('help')
        >>> print(out.performance)
        <swat.CASPerformance object at 0x33b1c50>

        Performance values are loaded lazily, but you can get a dictionary of
        all of them using the `to_dict` method.

        >>> print(out.performance.to_dict())
        {'system_cores': 1152L, 'memory_quota': 303759360L, 'cpu_user_time': 0.014995,
         'elapsed_time': 0.004200000000000001, 'system_nodes': 48L,
         'memory_system': 432093312L, 'cpu_system_time': 0.018999, 'memory': 150688L,
         'memory_os': 294322176L, 'system_total_memory': 4868538236928L}

        Rather than having the `retrieve` method compile all of the results into one
        object, you can control how the responses and results from the server are
        handled in your own functions using the `responsefunc` or `resultfunc` keyword
        arguments.

        The `responsefunc` argument allows you to specify a function that is called for
        each response from the server after the action is called.  The `resultfunc`
        is called for each result in a response.  These functions can not be used at the
        same time though.  In the case where both are specified, only the resultfunc
        will be used.  Below is an example of using a responsefunc function.
        This function closely mimics what the `retrieve` method does by default.

        >>> def myfunc(response, connection, userdata):
        ...     if userdata is None:
        ...         userdata = {}
        ...     for key, value in response:
        ...         userdata[key] = value
        ...     return userdata
        >>> out = s.retrieve('help', responsefunc=myfunc)
        >>> print(out['builtins'])
                      name                                        description
        0          addnode                           Add a node to the server
        1             help                        Lists the available actions
        2        listnodes                              List the server nodes
        .
        .
        .

        The same result can be gotten using the `resultfunc` option as well.

        >>> def myfunc(key, value, response, connection, userdata):
        ...    if userdata is None:
        ...       userdata = {}
        ...    userdata[key] = value
        ...    return userdata
        >>> out = s.retrieve('help', resultfunc=myfunc)
        >>> print(out['builtins'])
                      name                                        description
        0          addnode                           Add a node to the server
        1             help                        Lists the available actions
        2        listnodes                              List the server nodes
        .
        .
        .

        '''
        kwargs = dict(kwargs)

        # Decode from JSON as needed
        if '_json' in kwargs:
            newargs = json.loads(kwargs['_json'])
            newargs.update(kwargs)
            del newargs['_json']
            kwargs = newargs

        datamsghandler = None
        if 'datamsghandler' in kwargs:
            datamsghandler = kwargs['datamsghandler']
            kwargs.pop('datamsghandler')

        # Response callback function
        responsefunc = None
        if 'responsefunc' in kwargs:
            responsefunc = kwargs['responsefunc']
            kwargs.pop('responsefunc')

        # Result callback function
        resultfunc = None
        if 'resultfunc' in kwargs:
            resultfunc = kwargs['resultfunc']
            kwargs.pop('resultfunc')

        # Call the action and compile the results
        signature = self._invoke_with_signature(a2n(_name_), **kwargs)

        results = self._get_results(getnext(self, datamsghandler=datamsghandler),
                                    responsefunc=responsefunc, resultfunc=resultfunc)

        # Return raw data if a function was supplied
        if responsefunc is not None or resultfunc is not None:
            return results

        results.signature = signature

        # run post-processing hooks
        if signature and signature.get('name') in self._results_hooks:
            for func in self._results_hooks[signature['name']]:
                func(self, results)

        return results

    def _get_results(self, riter, responsefunc=None, resultfunc=None):
        '''
        Walk through responses in `riter` and compile results

        Parameters
        ----------
        riter : iterable
            Typically a CAS object, but any iterable that returns a
            response / connection pair for each iteration can be used.
        responsefunc : callable, optional
            Callback function that is called for each response
        resultfunc : callable, optional
            Callback function that is called for each result

        Returns
        -------
        CASResults
            If no callback functions were supplied.
        any
            If a callback function is supplied, the result of that function
            is returned.

        '''
        results = CASResults()
        results.messages = messages = []
        results.updateflags = updateflags = set()
        results.session = self._session
        results.sessionname = self._name
        events = results.events
        idx = 0
        resultdata = None
        responsedata = None

        try:
            for response, conn in riter:

                if responsefunc is not None:
                    responsedata = responsefunc(response, conn, responsedata)
                    continue

                # Action was restarted by the server
                if 'action-restart' in response.updateflags:
                    results = CASResults()
                    results.messages = messages = []
                    results.updateflags = updateflags = set()
                    results.session = self._session
                    results.sessionname = self._name
                    events = results.events
                    idx = 0
                    continue

                # CASTable parameters
                caslib = None
                tablename = None
                castable = None

                for key, value in response:

                    if resultfunc is not None:
                        resultdata = resultfunc(key, value, response,
                                                conn, resultdata)
                        continue

                    if key is None or isinstance(key, int_types):
                        results[idx] = value
                        idx += 1
                    else:
                        lowerkey = key.lower()
                        if lowerkey == 'tablename':
                            tablename = value
                        elif lowerkey == 'caslib':
                            caslib = value
                        elif lowerkey == 'castable':
                            castable = True
                        # Event results start with '$'
                        if key.startswith('$'):
                            events[key] = value
                        else:
                            results[key] = value

                # Create a CASTable instance if all of the pieces are there
                if caslib and tablename and not castable:
                    results['casTable'] = self.CASTable(tablename, caslib=caslib)

                results.performance = response.performance
                for key, value in six.iteritems(response.disposition.to_dict()):
                    setattr(results, key, value)
                messages.extend(response.messages)
                updateflags.update(response.updateflags)

        except SWATCASActionError as err:
            if responsefunc:
                err.results = responsedata
            elif resultfunc:
                err.results = resultdata
            else:
                err.results = results
                err.events = events
            raise err

        if responsefunc is not None:
            return responsedata

        if resultfunc is not None:
            return resultdata

        return results

    def __getattr__(self, name, atype=None):
        '''
        Convenience method for getting a CASActionSet/CASAction as an attribute

        When an attribute that looks like an action name is accessed, CAS
        is queried to see if it is an action set or action name.  If so,
        the reflection information for the entire actionset is used to
        generate classes for the actionset and all actions.

        Parameters
        ----------
        name : string
           Action name or action set name
        atype : string, optional
           Type of item to search for exclusively ('actionset', 'action',
           or 'action_class')

        Returns
        -------
        CASAction
           If `name` is an action name
        CASActionSet
           If `name` is an action set name

        Raises
        ------
        AttributeError
           if `name` is neither an action name or action set name

        '''
        class_requested = False
        origname = name

        # Normalize name
        if re.match(r'^[A-Z]', name):
            class_requested = True

        if atype is not None and atype == 'action_class':
            class_requested = True
            atype = 'action'

        name = name.lower()

        # Check cache for actionset and action classes
        if atype in [None, 'actionset']:
            if name in self._actionset_classes and \
                    self._actionset_classes[name] is not None:
                return self._actionset_classes[name]()

        if atype in [None, 'action']:
            if name in self._action_classes and \
                    self._action_classes[name] is not None:
                if class_requested:
                    return self._action_classes[name]
                return self._action_classes[name]()

        # See if the action/action set exists
        asname, actname, asinfo = self._get_actionset_info(name.lower(), atype=atype)

        # Generate a new actionset class
        ascls = CASActionSet.from_reflection(asinfo, self)

        # Add actionset and actions to the cache
        self._actionset_classes[asname.lower()] = ascls
        for key, value in six.iteritems(ascls.actions):
            self._action_classes[key] = value
            self._action_classes[asname.lower() + '.' + key] = value

        # Check cache for actionset and action classes
        if atype in [None, 'actionset']:
            if name in self._actionset_classes:
                return self._actionset_classes[name]()

        if atype in [None, 'action']:
            if name in self._action_classes:
                if class_requested:
                    return self._action_classes[name]
                return self._action_classes[name]()

        raise AttributeError(origname)

    def _get_action_info(self, name, showhidden=True):
        '''
        Get the reflection information for the given action name

        Parameters
        ----------
        name : string
           Name of the action
        showhidden : boolean
           Should hidden actions be shown?

        Returns
        -------
        ( string, string, dict )
           Tuple containing action-set-name, action-name, and action-info-dict

        '''
        name = name.lower()
        if name in self._action_info:
            return self._action_info[name]

        asname, actname, asinfo = self._get_reflection_info(name, showhidden=showhidden)

        # Populate action set info while we're here
        self._actionset_info[asname.lower()] = asname, None, asinfo

        # Populate action info
        actinfo = {}
        for item in asinfo.get('actions'):
            asname, aname = item['name'].split('.', 1)
            if aname == actname.lower():
                actinfo = item
            self._action_info[aname] = asname, aname, item
            self._action_info[item['name']] = asname, aname, item

        return asname, actname, actinfo

    def _get_actionset_info(self, name, atype=None, showhidden=True):
        '''
        Get the reflection information for the given action set / action name

        If the name is an action set, the returned action name will be None.

        Parameters
        ----------
        name : string
           Name of the action set or action
        atype : string, optional
           Specifies the type of the name ('action' or 'actionset')
        showhidden : boolean, optional
           Should hidden actions be shown?

        Returns
        -------
        ( string, string, dict )
           Tuple containing action-set-name, action-name, and action-set-info-dict

        '''
        name = name.lower()
        if atype in [None, 'actionset'] and name in self._actionset_info:
            return self._actionset_info[name]
        if atype in [None, 'action'] and name in self._action_info:
            asname, aname, actinfo = self._action_info[name]
            return asname, aname, self._actionset_info[asname.lower()][-1]

        asname, actname, asinfo = self._get_reflection_info(name, atype=atype,
                                                            showhidden=showhidden)

        # Populate action set info
        self._actionset_info[asname.lower()] = asname, None, asinfo

        # Populate action info while we're here
        for item in asinfo.get('actions'):
            asname, aname = item['name'].split('.', 1)
            self._action_info[aname] = asname, aname, item
            self._action_info[item['name']] = asname, aname, item

        return asname, actname, asinfo

    def _get_reflection_info(self, name, atype=None, showhidden=True):
        '''
        Get the full action name of the called action including the action set information

        Parameters
        ----------
        name : string
           Name of the argument
        atype : string, optional
           Specifies the type of the name ('action' or 'actionset')
        showhidden : boolean, optional
           Should hidden actions be shown?

        Returns
        -------
        tuple
           ( action set name, action name, action set reflection info )

        '''
        asname = None
        actname = None

        # See if the name is an action set name, action name, or nothing
        if atype in [None, 'actionset']:
            for response in self._invoke_without_signature('builtins.queryactionset',
                                                           actionset=name,
                                                           _messagelevel='error',
                                                           _apptag='UI'):
                for key, value in response:
                    if value:
                        asname = name.lower()
                    break

        if asname is None:
            idx = 0
            out = {}
            for response in self._invoke_without_signature('builtins.queryname',
                                                           name=name,
                                                           _messagelevel='error',
                                                           _apptag='UI'):
                for key, value in response:
                    if key is None or isinstance(key, int_types):
                        out[idx] = value
                        idx += 1
                    else:
                        out[key] = value

            asname = out.get('actionSet')
            actname = out.get('action')

            # We can't have both in the same namespace, action set name wins
            if asname == actname:
                actname = None

        # If we have an action set name, reflect it
        if asname:
            asname = asname.lower()
            query = {'showhidden': showhidden, 'actionset': asname}
            idx = 0
            out = {}
            for response in self._invoke_without_signature('builtins.reflect',
                                                           _messagelevel='error',
                                                           _apptag='UI', **query):
                for key, value in response:
                    if key is None or isinstance(key, int_types):
                        out[idx] = value
                        idx += 1
                    else:
                        out[key] = value

            # Normalize the output
            asinfo = _lower_actionset_keys(out[0])
            for act in asinfo.get('actions'):
                act['name'] = (asname + '.' + act['name']).lower()

            return asname, actname, asinfo

        raise AttributeError(name)

    def __iter__(self):
        '''
        Iterate over responses from CAS

        If you used the :meth:`invoke` method to call a CAS action, the
        responses from the server are not automatically retrieved.  You
        will need to pull them down manually.  Iterating over the CAS
        connection object after calling :meth:`invoke` will pull responses
        down until they have been exhausted.

        Examples
        --------
        >>> conn = swat.CAS()
        >>> conn.invoke('serverstatus')
        >>> for resp in conn:
        ...     for k, v in resp:
        ...        print(k, v)

        See Also
        --------
        :meth:`invoke` : Calls a CAS action without retrieving results
        :meth:`retrieve` : Calls a CAS action and retrieves results

        Yields
        ------
        :class:`CASResponse` object

        '''
        for response, conn in getnext(self, timeout=0):
            if conn is not None:
                yield response

    #
    # Top-level Pandas functions
    #

    def _get_table_args(self, *args, **kwargs):
        ''' Extract table paramaters from function arguments '''
        out = {}
        kwargs = kwargs.copy()
        casout = kwargs.pop('casout', {})
        if not isinstance(casout, dict):
            casout = dict(name=casout)
        out['table'] = casout.get('name', None)
        out['caslib'] = casout.get('caslib', None)
        out['replace'] = casout.get('replace', None)
        out['label'] = casout.get('label', None)
        out['promote'] = casout.get('promote', None)
        if not out['table']:
            out.pop('table')
        if not out['caslib']:
            out.pop('caslib')
        if out['replace'] is None:
            out.pop('replace')
        if out['label'] is None:
            out.pop('label')
        if out['promote'] is None:
            out.pop('promote')
        return out, kwargs

    def read_cas_path(self, path=None, readahead=None, importoptions=None,
                      resident=None, promote=None, ondemand=None, attrtable=None,
                      caslib=None, options=None, casout=None, singlepass=None,
                      where=None, varlist=None, groupby=None, groupbyfmts=None,
                      groupbymode=None, orderby=None, nosource=None, returnwhereinfo=None,
                      **kwargs):
        '''
        Read a path from a CASLib

        The parameters for this are the same as for the `builtins.loadtable`
        CAS action.  This method is simply a convenience method that loads a
        table and returns a :class:`CASTable` in one step.

        Notes
        -----
        The path specified must exist on the **server side**.  For loading 
        data from the client side, see the `read_*` and :meth:`upload` methods.

        Examples
        --------
        >>> conn = swat.CAS()
        >>> tbl = conn.read_cas_path('data/iris.csv')
        >>> print(tbl.head())

        See Also
        --------
        :meth:`read_csv`
        :meth:`upload`

        Returns
        -------
        :class:`CASTable`

        '''
        args = {k: v for k, v in dict(path=path, readahead=readahead,
                importoptions=importoptions, resident=resident, promote=promote,
                ondemand=ondemand, attrtable=attrtable, caslib=caslib,
                options=options, casout=casout, singlepass=singlepass,
                where=where, varlist=varlist, groupby=groupby,
                groupbyfmts=groupbyfmts, groupbymode=groupbymode,
                orderby=orderby, nosource=nosource,
                returnwhereinfo=returnwhereinfo).items() if v is not None}
        args.update(kwargs)
        out = self.retrieve('table.loadtable', _messagelevel='error', **args)
        try:
            return out['casTable']
        except KeyError:
            raise SWATError(out.status)

    def _read_any(self, _method_, *args, **kwargs):
        '''
        Generic data file reader

        Parameters
        ----------
        _method_ : string
            The name of the pandas data reader function.
        *args : one or more arguments
            Arguments to pass to the data reader.
        **kwargs : keyword arguments
            Keyword arguments to pass to the data reader function.
            The keyword parameters 'table', 'caslib', 'promote', and
            'replace' will be stripped to use for the output CAS
            table parameters.

        Returns
        -------
        :class:`CASTable`

        '''
        if self._protocol.startswith('http'):
            raise SWATError('The table.addtable action is not supported ' +
                            'in the REST interface')
        import pandas as pd
        from swat import datamsghandlers as dmh
        table, kwargs = self._get_table_args(*args, **kwargs)
        dframe = getattr(pd, _method_)(*args, **kwargs)
        table.update(dmh.PandasDataFrame(dframe).args.addtable)
        return self.retrieve('table.addtable', **table).casTable

    def read_pickle(self, path, **kwargs):
        '''
        Load pickled pandas object from the specified path

        This method calls :func:`pandas.read_pickle` with the
        given arguments, then uploads the resulting :class:`pandas.DataFrame`
        to a CAS table.

        Parameters
        ----------
        path : string
            Path to a local pickle file.
        casout : string or :class:`CASTable`, optional
            The output table specification.  This includes the following parameters.
                table : string, optional
                    Name of the output CAS table.
                caslib : string, optional
                    CASLib for the output CAS table.
                label : string, optional
                    The label to apply to the output CAS table. 
                promote : boolean, optional
                    If True, the output CAS table will be visible in all sessions.
                replace : boolean, optional
                    If True, the output CAS table will replace any existing CAS.
                    table with the same name.
        **kwargs : any, optional
            Keyword arguments to :func:`pandas.read_pickle`.

        Notes
        -----
        Paths to specified files point to files on the **client machine**.

        Examples
        --------
        >>> conn = swat.CAS()
        >>> tbl = conn.read_pickle('dataframe.pkl')
        >>> print(tbl.head())

        See Also
        --------
        :func:`pandas.read_pickle` 

        Returns
        -------
        :class:`CASTable`

        '''
        return self._read_any('read_pickle', path, **kwargs)

    def read_table(self, filepath_or_buffer, **kwargs):
        '''
        Read general delimited file into a CAS table

        This method calls :func:`pandas.read_table` with the
        given arguments, then uploads the resulting :class:`pandas.DataFrame`
        to a CAS table.

        Parameters
        ----------
        filepath_or_buffer : str or any object with a read() method
            Path, URL, or buffer to read.
        casout : string or :class:`CASTable`, optional
            The output table specification.  This includes the following parameters.
                table : string, optional
                    Name of the output CAS table.
                caslib : string, optional
                    CASLib for the output CAS table.
                label : string, optional
                    The label to apply to the output CAS table.
                promote : boolean, optional
                    If True, the output CAS table will be visible in all sessions.
                replace : boolean, optional
                    If True, the output CAS table will replace any existing CAS.
                    table with the same name.
        **kwargs : any, optional
            Keyword arguments to :func:`pandas.read_table`.
      
        Notes
        -----
        Paths to specified files point to files on the client machine.

        Examples
        --------
        >>> conn = swat.CAS()
        >>> tbl = conn.read_table('iris.tsv')
        >>> print(tbl.head())

        See Also
        --------
        :func:`pandas.read_table` 
        :meth:`upload`

        Returns
        -------
        :class:`CASTable`

        '''
        if self._protocol.startswith('http'):
            raise SWATError('The table.addtable action is not supported ' +
                            'in the REST interface')
        from swat import datamsghandlers as dmh
        table, kwargs = self._get_table_args(filepath_or_buffer, **kwargs)
        table.update(dmh.Text(filepath_or_buffer, **kwargs).args.addtable)
        return self.retrieve('table.addtable', **table).casTable

    def read_csv(self, filepath_or_buffer, **kwargs):
        '''
        Read CSV file into a CAS table

        This method calls :func:`pandas.read_csv` with the
        given arguments, then uploads the resulting :class:`pandas.DataFrame`
        to a CAS table.

        Parameters
        ----------
        filepath_or_buffer : str or any object with a read() method
            Path, URL, or buffer to read.
        casout : string or :class:`CASTable`, optional
            The output table specification.  This includes the following parameters.
                table : string, optional
                    Name of the output CAS table.
                caslib : string, optional
                    CASLib for the output CAS table.
                label : string, optional
                    The label to apply to the output CAS table.
                promote : boolean, optional
                    If True, the output CAS table will be visible in all sessions.
                replace : boolean, optional
                    If True, the output CAS table will replace any existing CAS.
                    table with the same name.
        **kwargs : any, optional
            Keyword arguments to :func:`pandas.read_csv`.

        Notes
        -----
        Paths to specified files point to files on the client machine.

        Examples
        --------
        >>> conn = swat.CAS()
        >>> tbl = conn.read_csv('iris.csv')
        >>> print(tbl.head())

        See Also
        --------
        :func:`pandas.read_table`
        :meth:`upload`

        Returns
        -------
        :class:`CASTable`

        '''
        if self._protocol.startswith('http'):
            raise SWATError('The table.addtable action is not supported ' +
                            'in the REST interface')
        from swat import datamsghandlers as dmh
        table, kwargs = self._get_table_args(filepath_or_buffer, **kwargs)
        table.update(dmh.CSV(filepath_or_buffer, **kwargs).args.addtable)
        return self.retrieve('table.addtable', **table).casTable

    def read_fwf(self, filepath_or_buffer, **kwargs):
        '''
        Read a table of fixed-width formatted lines into a CAS table

        This method calls :func:`pandas.read_fwf` with the
        given arguments, then uploads the resulting :class:`pandas.DataFrame`
        to a CAS table.

        Parameters
        ----------
        filepath_or_buffer : str or any object with a read() method
            Path, URL, or buffer to read.
        casout : string or :class:`CASTable`, optional
            The output table specification.  This includes the following parameters.
                table : string, optional
                    Name of the output CAS table.
                caslib : string, optional
                    CASLib for the output CAS table.
                label : string, optional
                    The label to apply to the output CAS table.
                promote : boolean, optional
                    If True, the output CAS table will be visible in all sessions.
                replace : boolean, optional
                    If True, the output CAS table will replace any existing CAS.
                    table with the same name.
        **kwargs : any, optional
            Keyword arguments to :func:`pandas.read_table`.

        Notes
        -----
        Paths to specified files point to files on the client machine.

        Examples
        --------
        >>> conn = swat.CAS()
        >>> tbl = conn.read_table('iris.dat')
        >>> print(tbl.head())

        See Also
        --------
        :func:`pandas.read_table`
        :meth:`upload`

        Returns
        -------
        :class:`CASTable`

        '''
        if self._protocol.startswith('http'):
            raise SWATError('The table.addtable action is not supported ' +
                            'in the REST interface')
        from swat import datamsghandlers as dmh
        table, kwargs = self._get_table_args(filepath_or_buffer, **kwargs)
        table.update(dmh.FWF(filepath_or_buffer, **kwargs).args.addtable)
        return self.retrieve('table.addtable', **table).casTable

    def read_clipboard(self, *args, **kwargs):
        '''
        Read text from clipboard and pass to :meth:`read_table`

        Parameters
        ----------
        casout : string or :class:`CASTable`, optional
            The output table specification.  This includes the following parameters.
                table : string, optional
                    Name of the output CAS table.
                caslib : string, optional
                    CASLib for the output CAS table.
                label : string, optional
                    The label to apply to the output CAS table.
                promote : boolean, optional
                    If True, the output CAS table will be visible in all sessions.
                replace : boolean, optional
                    If True, the output CAS table will replace any existing CAS.
                    table with the same name.
        **kwargs : any, optional
            Keyword arguments to :func:`pandas.read_table`.

        See Also
        --------
        :func:`pandas.read_clipboard`
        :func:`pandas.read_table`
        :meth:`read_table`

        Returns
        -------
        :class:`CASTable`

        '''
        return self._read_any('read_clipboard', *args, **kwargs)

    def read_excel(self, io, **kwargs):
        '''
        Read an Excel table into a CAS table

        This method calls :func:`pandas.read_excel` with the
        given arguments, then uploads the resulting :class:`pandas.DataFrame`
        to a CAS table.

        Parameters
        ----------
        io : string or path object
            File-like object, URL, or pandas ExcelFile.
        casout : string or :class:`CASTable`, optional
            The output table specification.  This includes the following parameters.
                table : string, optional
                    Name of the output CAS table.
                caslib : string, optional
                    CASLib for the output CAS table.
                label : string, optional
                    The label to apply to the output CAS table.
                promote : boolean, optional
                    If True, the output CAS table will be visible in all sessions.
                replace : boolean, optional
                    If True, the output CAS table will replace any existing CAS.
                    table with the same name.
        **kwargs : any, optional
            Keyword arguments to :func:`pandas.read_table`.

        Examples
        --------
        >>> conn = swat.CAS()
        >>> tbl = conn.read_excel('iris.xlsx')
        >>> print(tbl.head())

        See Also
        --------
        :func:`pandas.read_excel`
        :meth:`upload`

        Returns
        -------
        :class:`CASTable`

        '''
        return self._read_any('read_excel', io, **kwargs)

    def read_json(self, path_or_buf=None, **kwargs):
        '''
        Read a JSON string into a CAS table

        This method calls :func:`pandas.read_json` with the
        given arguments, then uploads the resulting :class:`pandas.DataFrame`
        to a CAS table.

        Parameters
        ----------
        path_or_buf : string or file-like object
            The path, URL, or file object that contains the JSON data.
        casout : string or :class:`CASTable`, optional
            The output table specification.  This includes the following parameters.
                table : string, optional
                    Name of the output CAS table.
                caslib : string, optional
                    CASLib for the output CAS table.
                label : string, optional
                    The label to apply to the output CAS table.
                promote : boolean, optional
                    If True, the output CAS table will be visible in all sessions.
                replace : boolean, optional
                    If True, the output CAS table will replace any existing CAS.
                    table with the same name.
        **kwargs : any, optional
            Keyword arguments to :func:`pandas.read_table`.

        Examples
        --------
        >>> conn = swat.CAS()
        >>> tbl = conn.read_json('iris.json')
        >>> print(tbl.head())

        See Also
        --------
        :func:`pandas.read_json`

        Returns
        -------
        :class:`CASTable`

        '''
        return self._read_any('read_json', path_or_buf, **kwargs)

    def json_normalize(self, data, **kwargs):
        '''
        "Normalize" semi-structured JSON data into a flat table and upload to a CAS table

        This method calls :func:`pandas.json_normalize` with the
        given arguments, then uploads the resulting :class:`pandas.DataFrame`
        to a CAS table.

        Parameters
        ----------
        data : dict or list of dicts
            Unserialized JSON objects
        casout : string or :class:`CASTable`, optional
            The output table specification.  This includes the following parameters.
                table : string, optional
                    Name of the output CAS table.
                caslib : string, optional
                    CASLib for the output CAS table.
                label : string, optional
                    The label to apply to the output CAS table.
                promote : boolean, optional
                    If True, the output CAS table will be visible in all sessions.
                replace : boolean, optional
                    If True, the output CAS table will replace any existing CAS.
                    table with the same name.
        **kwargs : any, optional
            Keyword arguments to :func:`pandas.json_normalize`.

        Examples
        --------
        >>> conn = swat.CAS()
        >>> tbl = conn.json_normalize('iris.json')
        >>> print(tbl.head())

        See Also
        --------
        :func:`pandas.json_normalize`

        Returns
        -------
        :class:`CASTable`

        '''
        return self._read_any('json_normalize', data, **kwargs)

    def read_html(self, io, **kwargs):
        '''
        Read HTML tables into a list of CASTable objects

        This method calls :func:`pandas.read_html` with the
        given arguments, then uploads the resulting :class:`pandas.DataFrame`
        to a CAS table.

        Parameters
        ----------
        io : string or file-like object
            The path, URL, or file object that contains the HTML data.
        casout : string or :class:`CASTable`, optional
            The output table specification.  This includes the following parameters.
                table : string, optional
                    Name of the output CAS table.
                caslib : string, optional
                    CASLib for the output CAS table.
                label : string, optional
                    The label to apply to the output CAS table.
                promote : boolean, optional
                    If True, the output CAS table will be visible in all sessions.
                replace : boolean, optional
                    If True, the output CAS table will replace any existing CAS.
                    table with the same name.
        **kwargs : any, optional
            Keyword arguments to :func:`pandas.read_html`.

        Examples
        --------
        >>> conn = swat.CAS()
        >>> tbl = conn.read_html('iris.html')
        >>> print(tbl.head())

        See Also
        --------
        :func:`pandas.read_html`

        Returns
        -------
        :class:`CASTable`

        '''
        if self._protocol.startswith('http'):
            raise SWATError('The table.addtable action is not supported ' +
                            'in the REST interface')
        import pandas as pd
        from swat import datamsghandlers as dmh
        kwargs = kwargs.copy()
        out = []
        table, kwargs = self._get_table_args(io, **kwargs)
        for i, dframe in enumerate(pd.read_html(io, **kwargs)):
            if i and table.get('table'):
                table['table'] += str(i)
            table.update(dmh.PandasDataFrame(dframe).args.addtable)
            out.append(self.retrieve('table.addtable', **table).casTable)
        return out

    def read_hdf(self, path_or_buf, **kwargs):
        '''
        Read from the HDF store and create a CAS table

        This method calls :func:`pandas.read_hdf` with the
        given arguments, then uploads the resulting :class:`pandas.DataFrame`
        to a CAS table.

        Parameters
        ----------
        path_or_buf : string or file-like object
            The path, URL, or file object that contains the HDF data.
        casout : string or :class:`CASTable`, optional
            The output table specification.  This includes the following parameters.
                table : string, optional
                    Name of the output CAS table.
                caslib : string, optional
                    CASLib for the output CAS table.
                label : string, optional
                    The label to apply to the output CAS table.
                promote : boolean, optional
                    If True, the output CAS table will be visible in all sessions.
                replace : boolean, optional
                    If True, the output CAS table will replace any existing CAS.
                    table with the same name.
        **kwargs : any, optional
            Keyword arguments to :func:`pandas.read_hdf`.

        Examples
        --------
        >>> conn = swat.CAS()
        >>> tbl = conn.read_hdf('iris.html')
        >>> print(tbl.head())

        See Also
        --------
        :func:`pandas.read_hdf`

        Returns
        -------
        :class:`CASTable`

        '''
        return self._read_any('read_hdf', path_or_buf, **kwargs)

    def read_sas(self, filepath_or_buffer, **kwargs):
        '''
        Read SAS files stored as XPORT or SAS7BDAT into a CAS table

        This method calls :func:`pandas.read_sas` with the
        given arguments, then uploads the resulting :class:`pandas.DataFrame`
        to a CAS table.

        Parameters
        ----------
        filepath_or_buffer : string or file-like object
            The path, URL, or file object that contains the HDF data.
        casout : string or :class:`CASTable`, optional
            The output table specification.  This includes the following parameters.
                table : string, optional
                    Name of the output CAS table.
                caslib : string, optional
                    CASLib for the output CAS table.
                label : string, optional
                    The label to apply to the output CAS table.
                promote : boolean, optional
                    If True, the output CAS table will be visible in all sessions.
                replace : boolean, optional
                    If True, the output CAS table will replace any existing CAS.
                    table with the same name.
        **kwargs : any, optional
            Keyword arguments to :func:`pandas.read_sas`.

        Examples
        --------
        >>> conn = swat.CAS()
        >>> tbl = conn.read_sas('iris.sas7bdat')
        >>> print(tbl.head())

        See Also
        --------
        :func:`pandas.read_sas`

        Returns
        -------
        :class:`CASTable`

        '''
        return self._read_any('read_sas', filepath_or_buffer, **kwargs)

    def read_sql_table(self, table_name, con, **kwargs):
        '''
        Read SQL database table into a CAS table

        This method calls :func:`pandas.read_sql_table` with the
        given arguments, then uploads the resulting :class:`pandas.DataFrame`
        to a CAS table.

        Parameters
        ----------
        table_name : string
            Name of SQL table in database.
        con : SQLAlchemy connectable (or database string URI)
            Database connection.
        casout : string or :class:`CASTable`, optional
            The output table specification.  This includes the following parameters.
                table : string, optional
                    Name of the output CAS table.
                caslib : string, optional
                    CASLib for the output CAS table.
                label : string, optional
                    The label to apply to the output CAS table.
                promote : boolean, optional
                    If True, the output CAS table will be visible in all sessions.
                replace : boolean, optional
                    If True, the output CAS table will replace any existing CAS.
                    table with the same name.
        **kwargs : any, optional
            Keyword arguments to :func:`pandas.read_sql_table`.

        Examples
        --------
        >>> conn = swat.CAS()
        >>> tbl = conn.read_sql_table('iris', dbcon)
        >>> print(tbl.head())

        Notes
        -----
        The data from the database will be pulled to the client machine
        in the form of a :class:`pandas.DataFrame` then uploaded to CAS.
        If you are moving large amounts of data, you may want to use 
        a direct database connecter from CAS.

        See Also
        --------
        :func:`pandas.read_sql_table`
        :meth:`read_sql_query`
        :meth:`read_sql`

        Returns
        -------
        :class:`CASTable`

        '''
        return self._read_any('read_sql_table', table_name, con, **kwargs)

    def read_sql_query(self, sql, con, **kwargs):
        '''
        Read SQL query table into a CAS table

        This method calls :func:`pandas.read_sql_query` with the
        given arguments, then uploads the resulting :class:`pandas.DataFrame`
        to a CAS table.

        Parameters
        ----------
        sql : string
            SQL to be executed.
        con : SQLAlchemy connectable (or database string URI)
            Database connection.
        casout : string or :class:`CASTable`, optional
            The output table specification.  This includes the following parameters.
                table : string, optional
                    Name of the output CAS table.
                caslib : string, optional
                    CASLib for the output CAS table.
                label : string, optional
                    The label to apply to the output CAS table.
                promote : boolean, optional
                    If True, the output CAS table will be visible in all sessions.
                replace : boolean, optional
                    If True, the output CAS table will replace any existing CAS.
                    table with the same name.
        **kwargs : any, optional
            Keyword arguments to :func:`pandas.read_sql_query`.

        Examples
        --------
        >>> conn = swat.CAS()
        >>> tbl = conn.read_sql_query('select * from iris', dbcon)
        >>> print(tbl.head())

        Notes
        -----
        The data from the database will be pulled to the client machine
        in the form of a :class:`pandas.DataFrame` then uploaded to CAS.
        If you are moving large amounts of data, you may want to use 
        a direct database connecter from CAS.

        See Also
        --------
        :func:`pandas.read_sql_query`
        :meth:`read_sql_table`
        :meth:`read_sql`

        Returns
        -------
        :class:`CASTable`

        '''
        return self._read_any('read_sql_query', sql, con, **kwargs)

    def read_sql(self, sql, con, **kwargs):
        '''
        Read SQL query or database table into a CAS table

        This method calls :func:`pandas.read_sql` with the
        given arguments, then uploads the resulting :class:`pandas.DataFrame`
        to a CAS table.

        Parameters
        ----------
        sql : string
            SQL to be executed or table name.
        con : SQLAlchemy connectable (or database string URI)
            Database connection.
        casout : string or :class:`CASTable`, optional
            The output table specification.  This includes the following parameters.
                table : string, optional
                    Name of the output CAS table.
                caslib : string, optional
                    CASLib for the output CAS table.
                label : string, optional
                    The label to apply to the output CAS table.
                promote : boolean, optional
                    If True, the output CAS table will be visible in all sessions.
                replace : boolean, optional
                    If True, the output CAS table will replace any existing CAS.
                    table with the same name.
        **kwargs : any, optional
            Keyword arguments to :func:`pandas.read_sql`.

        Examples
        --------
        >>> conn = swat.CAS()
        >>> tbl = conn.read_sql('select * from iris', dbcon)
        >>> print(tbl.head())

        Notes
        -----
        The data from the database will be pulled to the client machine
        in the form of a :class:`pandas.DataFrame` then uploaded to CAS.
        If you are moving large amounts of data, you may want to use
        a direct database connecter from CAS.

        See Also
        --------
        :func:`pandas.read_sql`
        :meth:`read_sql_table`
        :meth:`read_sql_query`

        Returns
        -------
        :class:`CASTable`

        '''
        return self._read_any('read_sql', sql, con, **kwargs)

    def read_gbq(self, query, **kwargs):
        '''
        Load data from a Google BigQuery into a CAS table

        This method calls :func:`pandas.read_gbq` with the
        given arguments, then uploads the resulting :class:`pandas.DataFrame`
        to a CAS table.

        Parameters
        ----------
        query : string
            SQL-like query to return data values.
        casout : string or :class:`CASTable`, optional
            The output table specification.  This includes the following parameters.
                table : string, optional
                    Name of the output CAS table.
                caslib : string, optional
                    CASLib for the output CAS table.
                label : string, optional
                    The label to apply to the output CAS table.
                promote : boolean, optional
                    If True, the output CAS table will be visible in all sessions.
                replace : boolean, optional
                    If True, the output CAS table will replace any existing CAS.
                    table with the same name.
        **kwargs : any, optional
            Keyword arguments to :func:`pandas.read_gbq`.

        See Also
        --------
        :func:`pandas.read_gbq`

        Returns
        -------
        :class:`CASTable`

        '''
        return self._read_any('read_gbq', query, **kwargs)

    def read_stata(self, filepath_or_buffer, **kwargs):
        '''
        Read Stata file into a CAS table

        This method calls :func:`pandas.read_stata` with the
        given arguments, then uploads the resulting :class:`pandas.DataFrame`
        to a CAS table.

        Parameters
        ----------
        filepath_or_buffer : string or file-like object
            Path to .dta file or file-like object containing data.
        casout : string or :class:`CASTable`, optional
            The output table specification.  This includes the following parameters.
                table : string, optional
                    Name of the output CAS table.
                caslib : string, optional
                    CASLib for the output CAS table.
                label : string, optional
                    The label to apply to the output CAS table.
                promote : boolean, optional
                    If True, the output CAS table will be visible in all sessions.
                replace : boolean, optional
                    If True, the output CAS table will replace any existing CAS.
                    table with the same name.
        **kwargs : any, optional
            Keyword arguments to :func:`pandas.read_stata`.

        See Also
        --------
        :func:`pandas.read_stata`

        Returns
        -------
        :class:`CASTable`

        '''
        return self._read_any('read_stata', filepath_or_buffer, **kwargs)


def getone(connection, datamsghandler=None):
    '''
    Get a single response from a connection

    Parameters
    ----------
    connection : :class:`CAS` object
        The connection/CASAction to get the response from.
    datamsghandler : :class:`CASDataMsgHandler` object, optional
        The object to use for data messages from the server.

    Examples
    --------
    >>> conn = swat.CAS()
    >>> conn.invoke('serverstatus')
    >>> print(getone(conn))

    See Also
    --------
    :meth:`CAS.invoke`

    Returns
    -------
    :class:`CASResponse` object

    '''
    output = None, connection

    # enable data messages as needed
    if datamsghandler is not None:
        errorcheck(connection._sw_connection.enableDataMessages(),
                   connection._sw_connection)

    _sw_message = errorcheck(connection._sw_connection.receive(),
                             connection._sw_connection)
    if _sw_message:
        mtype = _sw_message.getType()
        if mtype == 'response':
            _sw_response = errorcheck(_sw_message.toResponse(
                connection._sw_connection), _sw_message)
            if _sw_response is not None:
                output = CASResponse(_sw_response, connection=connection), connection
        elif mtype == 'request' and datamsghandler is not None:
            _sw_request = errorcheck(_sw_message.toRequest(
                connection._sw_connection), _sw_message)
            if _sw_request is not None:
                req = CASRequest(_sw_request)
                output = datamsghandler(req, connection)
        elif mtype == 'request':
            _sw_request = errorcheck(_sw_message.toRequest(
                connection._sw_connection), _sw_message)
            if _sw_request is not None:
                req = CASRequest(_sw_request)
                output = req, connection

    if datamsghandler is not None:
        errorcheck(connection._sw_connection.disableDataMessages(),
                   connection._sw_connection)

    # Raise exception as needed
    if isinstance(output[0], CASResponse):
        exception_on_severity = get_option('cas.exception_on_severity')
        if exception_on_severity is not None and \
                output[0].disposition.severity >= exception_on_severity:
            raise SWATCASActionError(output[0].disposition.status, output[0], output[1])

    return output


def getnext(*objs, **kwargs):
    '''
    Return responses as they appear from multiple connections

    Parameters
    ----------
    *objs : :class:`CAS` objects and/or :class:`CASAction` objects
        Connection/CASAction objects to watch for responses.
    timeout : int, optional
        Timeout for waiting for a response on each connection.
    datamsghandler : :class:`CASDataMsgHandler` object, optional
        The object to use for data messages from the server.

    Examples
    --------
    >>> conn1 = swat.CAS()
    >>> conn2 = swat.CAS()
    >>> conn1.invoke('serverstatus')
    >>> conn2.invoke('userinfo')
    >>> for resp in getnext(conn1, conn2):
    ...     for k, v in resp:
    ...         print(k, v)

    See Also
    --------
    :meth:`CAS.invoke`

    Returns
    -------
    :class:`CASResponse` object

    '''
    timeout = kwargs.get('timeout', 0)
    datamsghandler = kwargs.get('datamsghandler')

    if len(objs) == 1 and isinstance(objs[0], (list, tuple, set)):
        connections = list(objs[0])
    else:
        connections = list(objs)

    # if the item in a CASAction, use the connection
    for i, conn in enumerate(connections):
        if isinstance(conn, CASAction):
            conn.invoke()
            connections[i] = conn.get_connection()

    # TODO: Set timeouts; check for mixed connection types
    if isinstance(connections[0]._sw_connection, rest.REST_CASConnection):
        for item in connections:
            yield getone(item)
        return

    _sw_watcher = errorcheck(clib.SW_CASConnectionEventWatcher(len(connections), timeout,
                                                               a2n(connections[
                                                                   0]._soptions),
                                                               connections[0]._sw_error),
                             connections[0]._sw_error)

    for item in connections:
        errorcheck(_sw_watcher.addConnection(item._sw_connection), _sw_watcher)

    try:

        while True:
            i = errorcheck(_sw_watcher.wait(), _sw_watcher)

            # finished
            if i == -2:
                break

            # timeout / retry
            if i == -1:
                yield [], None

            yield getone(connections[i], datamsghandler=datamsghandler)

    except (KeyboardInterrupt, SystemExit):
        for conn in connections:
            errorcheck(conn._sw_connection.stopAction(), conn._sw_connection)
        raise
