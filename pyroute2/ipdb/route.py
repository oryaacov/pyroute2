import types
import logging
import threading
from collections import namedtuple
from socket import AF_UNSPEC
from pyroute2.common import AF_MPLS
from pyroute2.common import basestring
from pyroute2.netlink import nlmsg
from pyroute2.netlink import nlmsg_base
from pyroute2.netlink.rtnl import rt_type
from pyroute2.netlink.rtnl import rt_proto
from pyroute2.netlink.rtnl import encap_type
from pyroute2.netlink.rtnl.rtmsg import rtmsg
from pyroute2.netlink.rtnl.req import IPRouteRequest
from pyroute2.ipdb.exceptions import CommitException
from pyroute2.ipdb.transactional import Transactional
from pyroute2.ipdb.transactional import with_transaction
from pyroute2.ipdb.linkedset import LinkedSet


class Metrics(Transactional):
    _fields = [rtmsg.metrics.nla2name(i[0]) for i in rtmsg.metrics.nla_map]


class Encap(Transactional):
    _fields = ['type', 'labels']


class Via(Transactional):
    _fields = ['family', 'addr']


class NextHopSet(LinkedSet):

    def __init__(self, prime=None):
        super(NextHopSet, self).__init__()
        prime = prime or []
        for v in prime:
            self.add(v)

    def __sub__(self, vs):
        ret = type(self)()
        sub = set(self.raw.keys()) - set(vs.raw.keys())
        for v in sub:
            ret.add(self[v], raw=self.raw[v])
        return ret

    def __make_nh(self, prime):
        if isinstance(prime, BaseRoute):
            return prime.make_key(prime)
        elif isinstance(prime, dict):
            if prime.get('family', None) == AF_MPLS:
                return MPLSRoute.make_key(prime)
            else:
                return Route.make_key(prime)
        elif isinstance(prime, tuple):
            return prime
        else:
            raise TypeError("unknown prime type %s" % type(prime))

    def __getitem__(self, key):
        return self.raw[key]

    def __iter__(self):
        def NHIterator():
            for x in tuple(self.raw.values()):
                yield x
        return NHIterator()

    def add(self, prime, raw=None, cascade=False):
        key = self.__make_nh(prime)
        r = key._required
        l = key._fields
        skey = key[:r] + (None, ) * (len(l) - r)
        if skey in self.raw:
            del self.raw[skey]
        return super(NextHopSet, self).add(key, raw=prime)

    def remove(self, prime, raw=None, cascade=False):
        key = self.__make_nh(prime)
        try:
            super(NextHopSet, self).remove(key)
        except KeyError as e:
            for key in tuple(self.raw.keys()):
                dct = dict(key._asdict())
                for ref in prime:
                    if prime[ref] and (dct[ref] != prime[ref]):
                        break
                else:
                    break
            else:
                raise e
            super(NextHopSet, self).remove(key)


class WatchdogMPLSKey(dict):

    def __init__(self, route):
        dict.__init__(self)
        self['oif'] = route['oif']
        self['dst'] = [{'ttl': 0, 'bos': 1, 'tc': 0, 'label': route['dst']}]


class WatchdogKey(dict):
    '''
    Construct from a route a dictionary that could be used as
    a match for IPDB watchdogs.
    '''
    def __init__(self, route):
        dict.__init__(self, [x for x in IPRouteRequest(route).items()
                             if x[0] in ('dst',
                                         'dst_len',
                                         'src',
                                         'src_len',
                                         'oif',
                                         'iif',
                                         'gateway',
                                         'table') and x[1]])

# Universal route key
RouteKey = namedtuple('RouteKey',
                      ('src',
                       'dst',
                       'gateway',
                       'encap',
                       'iif',
                       'oif'))
RouteKey._required = 4  # number of required fields (should go first)

# MPLS multipath NH key
MPLSNHKey = namedtuple('MPLSNHKey',
                       ('newdst',
                        'via',
                        'oif'))
MPLSNHKey._required = 2


class BaseRoute(Transactional):
    '''
    Persistent transactional route object
    '''

    _fields = [rtmsg.nla2name(i[0]) for i in rtmsg.nla_map]
    for key, _ in rtmsg.fields:
        _fields.append(key)
    _fields.append('removal')
    _virtual_fields = ['ipdb_scope', 'ipdb_priority']
    _fields.extend(_virtual_fields)
    _linked_sets = ['multipath', ]
    _nested = []
    cleanup = ('attrs',
               'header',
               'event',
               'cacheinfo')

    def __init__(self, ipdb, mode=None, parent=None, uid=None):
        Transactional.__init__(self, ipdb, mode, parent, uid)
        with self._direct_state:
            self['ipdb_priority'] = 0

    @with_transaction
    def add_nh(self, prime):
        with self._write_lock:
            self['multipath'].add(prime)

    @with_transaction
    def del_nh(self, prime):
        with self._write_lock:
            self['multipath'].remove(prime)

    def load_netlink(self, msg):
        with self._direct_state:
            if self['ipdb_scope'] == 'locked':
                # do not touch locked interfaces
                return

            self['ipdb_scope'] = 'system'
            for (key, value) in msg.items():
                self[key] = value

            # cleanup multipath NH
            for nh in self['multipath']:
                self.del_nh(nh)

            # merge NLA
            for (name, value) in msg['attrs']:
                norm = rtmsg.nla2name(name)
                # normalize RTAX
                if norm == 'metrics':
                    with self['metrics']._direct_state:
                        for metric in tuple(self['metrics'].keys()):
                            del self['metrics'][metric]
                        for (rtax, rtax_value) in value['attrs']:
                            rtax_norm = rtmsg.metrics.nla2name(rtax)
                            self['metrics'][rtax_norm] = rtax_value
                elif norm == 'multipath':
                    for record in value:
                        nh = type(self)(ipdb=self.ipdb, parent=self)
                        nh.load_netlink(record)
                        with nh._direct_state:
                            del nh['dst']
                            del nh['ipdb_scope']
                            del nh['ipdb_priority']
                            del nh['multipath']
                            del nh['metrics']
                        self['multipath'].add(nh)
                elif norm == 'encap':
                    with self['encap']._direct_state:
                        ret = []
                        for l in value.get_attr('MPLS_IPTUNNEL_DST'):
                            ret.append(str(l['label']))
                        self['encap']['labels'] = '/'.join(ret)
                elif norm == 'via':
                    with self['via']._direct_state:
                        self['via'] = value
                elif norm == 'newdst':
                    self['newdst'] = [x['label'] for x in value]
                else:
                    self[norm] = value

            if msg.get('family', 0) == AF_MPLS:
                dst = msg.get_attr('RTA_DST')
                if dst:
                    dst = dst[0]['label']
            else:
                if msg.get_attr('RTA_DST'):
                    dst = '%s/%s' % (msg.get_attr('RTA_DST'),
                                     msg['dst_len'])
                else:
                    dst = 'default'
            self['dst'] = dst

            # fix RTA_ENCAP_TYPE if needed
            if msg.get_attr('RTA_ENCAP'):
                if self['encap_type'] is not None:
                    with self['encap']._direct_state:
                        self['encap']['type'] = self['encap_type']
                    self['encap_type'] = None
            # or drop encap, if there is no RTA_ENCAP in msg
            elif self['encap'] is not None:
                self['encap_type'] = None
                with self['encap']._direct_state:
                    self['encap'] = {}

            # drop metrics, if there is no RTA_METRICS in msg
            if not msg.get_attr('RTA_METRICS') and self['metrics'] is not None:
                with self['metrics']._direct_state:
                    self['metrics'] = {}

            # same for via
            if not msg.get_attr('RTA_VIA') and self['via'] is not None:
                with self['via']._direct_state:
                    self['via'] = {}

            # finally, cleanup all not needed
            for item in self.cleanup:
                if item in self:
                    del self[item]

    def commit(self, tid=None, transaction=None, rollback=False):
        error = None
        drop = True
        devop = 'set'
        cleanup = []

        if tid:
            transaction = self.global_tx[tid]
        else:
            if transaction:
                drop = False
            else:
                transaction = self.current_tx

        # create a new route
        if self['ipdb_scope'] != 'system':
            devop = 'add'

        # work on an existing route
        snapshot = self.pick()
        diff = transaction - snapshot
        # FIXME
        if 'ipdb_scope' in diff:
            del diff['ipdb_scope']

        try:
            # route set
            if self['family'] != AF_MPLS:
                cleanup = [any(snapshot['metrics'].values()) and
                           not any(diff.get('metrics', {}).values()),
                           any(snapshot['encap'].values()) and
                           not any(diff.get('encap', {}).values())]
            if any(diff.values()) or any(cleanup) or devop == 'add':
                # prepare the anchor key to catch *possible* route update
                old_key = self.make_key(self)
                new_key = self.make_key(transaction)
                if old_key != new_key:
                    # assume we can not move routes between tables (yet ;)
                    if self['family'] == AF_MPLS:
                        route_index = self.ipdb.routes.tables['mpls'].idx
                    else:
                        route_index = (self.ipdb
                                       .routes
                                       .tables[self['table'] or 254]
                                       .idx)
                    if new_key not in route_index:
                        route_index[new_key] = {'key': new_key,
                                                'route': self}
                    else:
                        raise CommitException('Route idx conflict')
                    self.nl.route(devop, **transaction)
                    del route_index[old_key]
                else:
                    self.nl.route(devop, **transaction)
                transaction.wait_all_targets()
            # route removal
            if (transaction['ipdb_scope'] in ('shadow', 'remove')) or\
                    ((transaction['ipdb_scope'] == 'create') and rollback):
                if transaction['ipdb_scope'] == 'shadow':
                    with self._direct_state:
                        self['ipdb_scope'] = 'locked'
                # create watchdog
                wd = self.ipdb.watchdog('RTM_DELROUTE',
                                        **self.wd_key(snapshot))
                for route in self.nl.route('delete', **snapshot):
                    self.ipdb._route_del(route)
                wd.wait()
                if transaction['ipdb_scope'] == 'shadow':
                    with self._direct_state:
                        self['ipdb_scope'] = 'shadow'

        except Exception as e:
            if devop == 'add':
                error = e
                self.nl = None
                with self._direct_state:
                    self['ipdb_scope'] = 'invalid'
                if self['family'] == AF_MPLS:
                    route_index = self.ipdb.routes.tables['mpls'].idx
                else:
                    route_index = (self.ipdb
                                   .routes
                                   .tables[self['table'] or 254]
                                   .idx)
                route_key = self.make_key(self)
                del route_index[route_key]
            elif not rollback:
                ret = self.commit(transaction=snapshot, rollback=True)
                if isinstance(ret, Exception):
                    error = ret
                else:
                    error = e
            else:
                if drop:
                    self.drop(transaction.uid)
                x = RuntimeError()
                x.cause = e
                raise x

        if drop and not rollback:
            self.drop(transaction.uid)

        if error is not None:
            error.transaction = transaction
            raise error

        return self

    def remove(self):
        self['ipdb_scope'] = 'remove'
        return self

    def shadow(self):
        self['ipdb_scope'] = 'shadow'
        return self


class Route(BaseRoute):
    _nested = ['encap', 'metrics']
    wd_key = WatchdogKey

    @classmethod
    def make_encap(cls, encap):
        '''
        Normalize encap object
        '''
        labels = encap.get('labels', None)
        if isinstance(labels, (list, tuple, set)):
            labels = '/'.join(map(lambda x: str(x['label'])
                                  if isinstance(x, dict)
                                  else str(x), labels))
        if not isinstance(labels, basestring):
            raise TypeError('labels struct not supported')
        return {'type': encap.get('type', 'mpls'),
                'labels': labels}

    @classmethod
    def make_key(cls, msg):
        '''
        Construct from a netlink message a key that can be used
        to locate the route in the table
        '''
        values = []
        if isinstance(msg, nlmsg_base):
            for field in RouteKey._fields:
                v = msg.get_attr(msg.name2nla(field))
                if field in ('src', 'dst'):
                    if v is not None:
                        v = '%s/%s' % (v, msg['%s_len' % field])
                    elif field == 'dst':
                        v = 'default'
                elif field == 'encap':
                    # 1. encap type
                    if msg.get_attr('RTA_ENCAP_TYPE') != 1:  # FIXME
                        values.append(None)
                        continue
                    # 2. encap_type == 'mpls'
                    v = '/'.join([str(x['label']) for x
                                  in v.get_attr('MPLS_IPTUNNEL_DST')])
                elif v is None:
                    v = msg.get(field, None)
                values.append(v)
        elif isinstance(msg, dict):
            for field in RouteKey._fields:
                v = msg.get(field, None)
                if field == 'encap' and v and v['labels']:
                    v = v['labels']
                elif (field == 'encap') and \
                        (len(msg.get('multipath', []) or []) == 1):
                    v = (tuple(msg['multipath'].raw.values())[0]
                         .get('encap', {})
                         .get('labels', None))
                elif field == 'encap':
                    v = None
                elif (field == 'gateway') and \
                        (len(msg.get('multipath', []) or []) == 1) and \
                        not v:
                    v = (tuple(msg['multipath'].raw.values())[0]
                         .get('gateway', None))

                if field == 'encap' and isinstance(v, (list, tuple, set)):
                    v = '/'.join(map(lambda x: str(x['label'])
                                     if isinstance(x, dict)
                                     else str(x), v))
                values.append(v)
        else:
            raise TypeError('prime not supported: %s' % type(msg))
        return RouteKey(*values)

    def __setitem__(self, key, value):
        ret = value
        if (key in ('encap', 'metrics')) and isinstance(value, dict):
            # transactionals attach as is
            if type(value) in (Encap, Metrics):
                with self._direct_state:
                    return Transactional.__setitem__(self, key, value)

            # check, if it exists already
            ret = Transactional.__getitem__(self, key)
            # it doesn't
            # (plain dict can be safely discarded)
            if (type(ret) == dict) or not ret:
                # bake transactionals in place
                if key == 'encap':
                    ret = Encap(parent=self)
                elif key == 'metrics':
                    ret = Metrics(parent=self)
                # attach transactional to the route
                with self._direct_state:
                    Transactional.__setitem__(self, key, ret)
                # begin() works only if the transactional is attached
                if any(value.values()):
                    if self._mode in ('implicit', 'explicit'):
                        ret._begin(tid=self.current_tx.uid)
                    [ret.__setitem__(k, v) for k, v
                     in value.items() if v is not None]
            # corresponding transactional exists
            else:
                # set fields
                for k in ret:
                    ret[k] = value.get(k, None)
            return
        elif key == 'multipath':
            cur = Transactional.__getitem__(self, key)
            if isinstance(cur, NextHopSet):
                # load entries
                vs = NextHopSet(value)
                for key in vs - cur:
                    cur.add(key)
                for key in cur - vs:
                    cur.remove(key)
            else:
                # drop any result of `update()`
                Transactional.__setitem__(self, key, NextHopSet(value))
            return
        elif key == 'encap_type' and not isinstance(value, int):
            ret = encap_type.get(value, value)
        elif key == 'type' and not isinstance(value, int):
            ret = rt_type.get(value, value)
        elif key == 'proto' and not isinstance(value, int):
            ret = rt_proto.get(value, value)
        Transactional.__setitem__(self, key, ret)

    def __getitem__(self, key):
        ret = Transactional.__getitem__(self, key)
        if (key in ('encap', 'metrics', 'multipath')) and (ret is None):
            with self._direct_state:
                self[key] = [] if key == 'multipath' else {}
                ret = self[key]
        return ret


class MPLSRoute(BaseRoute):
    wd_key = WatchdogMPLSKey
    _nested = ['via']

    @classmethod
    def make_key(cls, msg):
        '''
        Construct from a netlink message a key that can be used
        to locate the route in the table
        '''
        ret = None
        if isinstance(msg, nlmsg):
            ret = msg.get_attr('RTA_DST')
        elif isinstance(msg, dict):
            ret = msg.get('dst', None)
        else:
            raise TypeError('prime not supported')
        if isinstance(ret, list):
            ret = ret[0]['label']
        elif ret is None:
            # key for nexthops
            ret = MPLSNHKey(newdst=tuple(msg['newdst']),
                            via=msg.get('via', {}).get('addr', None),
                            oif=msg.get('oif', None))

        return ret

    def __setitem__(self, key, value):
        if key == 'via' and isinstance(value, dict):
            # replace with a new transactional
            if type(value) == Via:
                with self._direct_state:
                    return BaseRoute.__setitem__(self, key, value)
            # or load the dict
            ret = BaseRoute.__getitem__(self, key)
            if not isinstance(ret, Via):
                ret = Via(parent=self)
                # attach new transactional -- replace any
                # non-Via object (may be a result of update())
                with self._direct_state:
                    BaseRoute.__setitem__(self, key, ret)
                # load value into the new object
                if any(value.values()):
                    if self._mode in ('implicit', 'explicit'):
                        ret._begin(tid=self.current_tx.uid)
                    [ret.__setitem__(k, v) for k, v
                     in value.items() if v is not None]
            else:
                # load value into existing object
                for k in ret:
                    ret[k] = value.get(k, None)
            return
        elif key == 'multipath':
            cur = BaseRoute.__getitem__(self, key)
            if isinstance(cur, NextHopSet):
                # load entries
                vs = NextHopSet(value)
                for key in vs - cur:
                    cur.add(key)
                for key in cur - vs:
                    cur.remove(key)
            else:
                BaseRoute.__setitem__(self, key, NextHopSet(value))
        else:
            BaseRoute.__setitem__(self, key, value)

    def __getitem__(self, key):
        with self._direct_state:
            ret = BaseRoute.__getitem__(self, key)
            if key == 'multipath' and ret is None:
                self[key] = []
                ret = self[key]
            elif key == 'via' and ret is None:
                self[key] = {}
                ret = self[key]
            return ret


class RoutingTable(object):

    route_class = Route

    def __init__(self, ipdb, prime=None):
        self.ipdb = ipdb
        self.lock = threading.Lock()
        self.idx = {}
        self.kdx = {}

    def __nogc__(self):
        return self.filter(lambda x: x['route']['ipdb_scope'] != 'gc')

    def __repr__(self):
        return repr([x['route'] for x in self.__nogc__()])

    def __len__(self):
        return len(self.keys())

    def __iter__(self):
        for record in self.__nogc__():
            yield record['route']

    def gc(self):
        for route in self.filter({'ipdb_scope': 'gc'}):
            try:
                self.ipdb.nl.route('get', **route['route'])
                with route['route']._direct_state:
                    route['route']['ipdb_scope'] = 'system'
            except:
                del self.idx[route['key']]

    def keys(self, key='dst'):
        with self.lock:
            return [x['route'][key] for x in self.__nogc__()]

    def filter(self, target, oneshot=False):
        #
        if isinstance(target, types.FunctionType):
            return filter(target, [x for x in self.idx.values()])

        if isinstance(target, basestring):
            target = {'dst': target}

        if not isinstance(target, dict):
            raise TypeError('target type not supported: %s' % type(target))

        ret = []
        for record in self.idx.values():
            for key, value in target.items():
                if (key not in record['route']) or \
                        (value != record['route'][key]):
                    break
            else:
                ret.append(record)
                if oneshot:
                    return ret

        return ret

    def describe(self, target, forward=False):
        # match the route by index -- a bit meaningless,
        # but for compatibility
        if isinstance(target, int):
            keys = tuple(self.idx.keys())
            return self.idx[keys[target]]

        # match the route by key
        if isinstance(target, (tuple, list)):
            try:
                # full match
                return self.idx[RouteKey(*target)]
            except KeyError:
                # w/o iif and oif
                # when a route is just created, there can be no oif and
                # iif specified, if they weren't provided explicitly,
                # and in that case there will be the key w/o oif and
                # iif
                r = RouteKey._required
                l = RouteKey._fields
                return self.idx[RouteKey(*(target[:r] +
                                           (None, ) * (len(l) - r)))]

        if isinstance(target, nlmsg):
            return self.idx[Route.make_key(target)]

        # match the route by filter
        ret = self.filter(target, oneshot=True)
        if ret:
            return ret[0]

        if not forward:
            raise KeyError('record not found')

        # match the route by dict spec
        if not isinstance(target, dict):
            raise TypeError('lookups can be done only with dict targets')

        # split masks
        if target.get('dst', '').find('/') >= 0:
            dst = target['dst'].split('/')
            target['dst'] = dst[0]
            target['dst_len'] = int(dst[1])

        if target.get('src', '').find('/') >= 0:
            src = target['src'].split('/')
            target['src'] = src[0]
            target['src_len'] = int(src[1])

        # load and return the route, if exists
        route = Route(self.ipdb)
        ret = self.ipdb.nl.get_routes(**target)
        if not ret:
            raise KeyError('record not found')
        route.load_netlink(ret[0])
        return {'route': route,
                'key': None}

    def __delitem__(self, key):
        with self.lock:
            item = self.describe(key, forward=False)
            del self.idx[self.route_class.make_key(item['route'])]

    def load(self, msg):
        key = self.route_class.make_key(msg)
        self[key] = msg
        return key

    def __setitem__(self, key, value):
        with self.lock:
            try:
                record = self.describe(key, forward=False)
            except KeyError:
                record = {'route': self.route_class(self.ipdb),
                          'key': None}

            if isinstance(value, nlmsg):
                record['route'].load_netlink(value)
            elif isinstance(value, self.route_class):
                record['route'] = value
            elif isinstance(value, dict):
                with record['route']._direct_state:
                    record['route'].update(value)

            key = self.route_class.make_key(record['route'])
            if record['key'] is None:
                self.idx[key] = {'route': record['route'],
                                 'key': key}
            else:
                self.idx[key] = record
                if record['key'] != key:
                    del self.idx[record['key']]
                    record['key'] = key

    def __getitem__(self, key):
        with self.lock:
            return self.describe(key, forward=False)['route']

    def __contains__(self, key):
        try:
            with self.lock:
                self.describe(key, forward=False)
            return True
        except KeyError:
            return False


class MPLSTable(RoutingTable):

    route_class = MPLSRoute

    def keys(self):
        return self.idx.keys()

    def describe(self, target, forward=False):
        # match by key
        if isinstance(target, int):
            return self.idx[target]

        # match by rtmsg
        if isinstance(target, rtmsg):
            return self.idx[self.route_class.make_key(target)]

        raise KeyError('record not found')


class RoutingTableSet(object):

    def __init__(self, ipdb, ignore_rtables=None):
        self.ipdb = ipdb
        self.ignore_rtables = ignore_rtables or []
        self.tables = {254: RoutingTable(self.ipdb)}

    def add(self, spec=None, **kwarg):
        '''
        Create a route from a dictionary
        '''
        spec = dict(spec or kwarg)
        if 'dst' not in spec:
            raise ValueError('dst not specified')
        multipath = spec.pop('multipath', [])
        if spec.get('family', 0) == AF_MPLS:
            table = 'mpls'
            if table not in self.tables:
                self.tables[table] = MPLSTable(self.ipdb)
            route = MPLSRoute(self.ipdb)
        else:
            table = spec.get('table', 254)
            if table not in self.tables:
                self.tables[table] = RoutingTable(self.ipdb)
            route = Route(self.ipdb)
        route.update(spec)
        with route._direct_state:
            route['ipdb_scope'] = 'create'
            for nh in multipath:
                if 'encap' in nh:
                    nh['encap'] = route.make_encap(nh['encap'])
                if table == 'mpls':
                    nh['family'] = AF_MPLS
                route.add_nh(nh)
        route.begin()
        for (key, value) in spec.items():
            if key == 'encap':
                route[key] = route.make_encap(value)
            else:
                route[key] = value
        self.tables[table][route.make_key(route)] = route
        return route

    def load_netlink(self, msg):
        '''
        Loads an existing route from a rtmsg
        '''
        if not isinstance(msg, rtmsg):
            return

        if msg.get('family', None) == AF_MPLS:
            table = 'mpls'
        else:
            table = msg.get('table', 254)
            if table == 252:
                table = msg.get_attr('RTA_TABLE')

        if table in self.ignore_rtables:
            return

        # RTM_DELROUTE
        if msg['event'] == 'RTM_DELROUTE':
            try:
                # locate the record
                record = self.tables[table][msg]
                # delete the record
                if record['ipdb_scope'] not in ('locked', 'shadow'):
                    del self.tables[table][msg]
                    with record._direct_state:
                        record['ipdb_scope'] = 'detached'
            except Exception as e:
                logging.debug(e)
                logging.debug(msg)
            return

        # RTM_NEWROUTE
        if table not in self.tables:
            if table == 'mpls':
                self.tables[table] = MPLSTable(self.ipdb)
            else:
                self.tables[table] = RoutingTable(self.ipdb)
        key = self.tables[table].load(msg)
        return self.tables[table][key]

    def gc(self):
        for table in self.tables.keys():
            self.tables[table].gc()

    def remove(self, route, table=None):
        if isinstance(route, Route):
            table = route.get('table', 254) or 254
            route = route.get('dst', 'default')
        else:
            table = table or 254
        self.tables[table][route].remove()

    def filter(self, target):
        ret = []
        for table in self.tables.values():
            if table is not None:
                ret.extend(table.filter(target))
        return ret

    def describe(self, spec, table=254):
        return self.tables[table].describe(spec)

    def get(self, dst, table=None):
        table = table or 254
        return self.tables[table][dst]

    def keys(self, table=254, family=AF_UNSPEC):
        return [x['dst'] for x in self.tables[table]
                if (x.get('family') == family) or
                (family == AF_UNSPEC)]

    def has_key(self, key, table=254):
        return key in self.tables[table]

    def __contains__(self, key):
        return key in self.tables[254]

    def __getitem__(self, key):
        return self.get(key)

    def __setitem__(self, key, value):
        if key != value['dst']:
            raise ValueError("dst doesn't match key")
        return self.add(value)

    def __delitem__(self, key):
        return self.remove(key)

    def __repr__(self):
        return repr(self.tables[254])
