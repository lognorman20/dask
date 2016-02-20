import cloudpickle
from collections import defaultdict, deque
from copy import deepcopy
from operator import add
from time import time

import dask
from dask.core import get_deps
from toolz import merge, concat, valmap
from tornado.queues import Queue
from tornado.iostream import StreamClosedError
from tornado.gen import TimeoutError
from tornado import gen

import pytest

from distributed import Nanny, Worker
from distributed.core import connect, read, write, rpc, dumps
from distributed.client import WrappedKey
from distributed.scheduler import (validate_state, heal,
        decide_worker, heal_missing_data, Scheduler,
        _maybe_complex, dumps_function, dumps_task, apply, str_graph)
from distributed.utils_test import (inc, ignoring, dec, gen_cluster, gen_test,
        loop)
from distributed.utils import All


alice = 'alice:1234'
bob = 'bob:1234'


def test_heal():
    dsk = {'x': 1, 'y': (inc, 'x')}
    dependencies = {'x': set(), 'y': {'x'}}
    dependents = {'x': {'y'}, 'y': set()}

    who_has = dict()
    stacks = {alice: [], bob: []}
    processing = {alice: set(), bob: set()}

    waiting = {'y': {'x'}}
    ready = {'x'}
    waiting_data = {'x': {'y'}, 'y': set()}
    finished_results = set()

    local = {k: v for k, v in locals().items() if '@' not in k}

    output = heal(**local)

    assert output['dependencies'] == dependencies
    assert output['dependents'] == dependents
    assert output['who_has'] == who_has
    assert output['processing'] == processing
    assert output['stacks'] == stacks
    assert output['waiting'] == waiting
    assert output['waiting_data'] == waiting_data
    assert output['released'] == set()

    state = {'who_has': dict(),
             'stacks': {alice: ['x'], bob: []},
             'processing': {alice: set(), bob: set()},
             'waiting': {}, 'waiting_data': {}, 'ready': set()}

    heal(dependencies, dependents, **state)


def test_heal_2():
    dsk = {'x': 1, 'y': (inc, 'x'), 'z': (inc, 'y'),
           'a': 1, 'b': (inc, 'a'), 'c': (inc, 'b'),
           'result': (add, 'z', 'c')}
    dependencies = {'x': set(), 'y': {'x'}, 'z': {'y'},
                    'a': set(), 'b': {'a'}, 'c': {'b'},
                    'result': {'z', 'c'}}
    dependents = {'x': {'y'}, 'y': {'z'}, 'z': {'result'},
                  'a': {'b'}, 'b': {'c'}, 'c': {'result'},
                  'result': set()}

    state = {'who_has': {'y': {alice}, 'a': {alice}},  # missing 'b'
             'stacks': {alice: ['z'], bob: []},
             'processing': {alice: set(), bob: set(['c'])},
             'waiting': {}, 'waiting_data': {}, 'ready': set()}

    output = heal(dependencies, dependents, **state)
    assert output['waiting'] == {'c': {'b'}, 'result': {'c', 'z'}}
    assert output['ready'] == {'b'}
    assert output['waiting_data'] == {'a': {'b'}, 'b': {'c'}, 'c': {'result'},
                                      'y': {'z'}, 'z': {'result'},
                                      'result': set()}
    assert output['who_has'] == {'y': {alice}, 'a': {alice}}
    assert output['stacks'] == {alice: ['z'], bob: []}
    assert output['processing'] == {alice: set(), bob: set()}
    assert output['released'] == {'x'}


def test_heal_restarts_leaf_tasks():
    dsk = {'a': 1, 'b': (inc, 'a'), 'c': (inc, 'b'),
           'x': 1, 'y': (inc, 'x'), 'z': (inc, 'y')}
    dependents, dependencies = get_deps(dsk)

    state = {'who_has': dict(),  # missing 'b'
             'stacks': {alice: ['a'], bob: ['x']},
             'processing': {alice: set(), bob: set()},
             'waiting': {}, 'waiting_data': {}, 'ready': set()}

    del state['stacks'][bob]
    del state['processing'][bob]

    output = heal(dependencies, dependents, **state)
    assert 'x' in output['waiting']


def test_heal_culls():
    dsk = {'a': 1, 'b': (inc, 'a'), 'c': (inc, 'b'),
           'x': 1, 'y': (inc, 'x'), 'z': (inc, 'y')}
    dependencies, dependents = get_deps(dsk)

    state = {'who_has': {'c': {alice}, 'y': {alice}},
             'stacks': {alice: ['a'], bob: []},
             'processing': {alice: set(), bob: set('y')},
             'waiting': {}, 'waiting_data': {}, 'ready': set()}

    output = heal(dependencies, dependents, **state)
    assert 'a' not in output['stacks'][alice]
    assert output['released'] == {'a', 'b', 'x'}
    assert output['finished_results'] == {'c'}
    assert 'y' not in output['processing'][bob]

    assert output['ready'] == {'z'}


@gen_cluster()
def test_ready_add_worker(s, a, b):
    s.add_client(client='client')
    s.add_worker(address=alice)

    s.update_graph(tasks={'x-%d' % i: dumps_task((inc, i)) for i in range(20)},
                   keys=['x-%d' % i for i in range(20)],
                   client='client',
                   dependencies={'x-%d' % i: set() for i in range(20)})


def test_update_state(loop):
    s = Scheduler()
    s.start(0)
    s.add_worker(address=alice, ncores=1)
    s.update_graph(tasks={'x': 1, 'y': (inc, 'x')},
                   keys=['y'],
                   dependencies={'y': 'x', 'x': set()},
                   client='client')

    s.mark_task_finished('x', alice, nbytes=10, type=dumps(int))

    assert s.processing[alice] == {'y'}
    assert not s.ready
    assert s.who_wants == {'y': {'client'}}
    assert s.wants_what == {'client': {'y'}}

    s.update_graph(tasks={'a': 1, 'z': (add, 'y', 'a')},
                   keys=['z'],
                   dependencies={'z': {'y', 'a'}},
                   client='client')


    assert s.tasks == {'x': 1, 'y': (inc, 'x'), 'a': 1, 'z': (add, 'y', 'a')}
    assert s.dependencies == {'x': set(), 'a': set(), 'y': {'x'}, 'z': {'a', 'y'}}
    assert s.dependents == {'z': set(), 'y': {'z'}, 'a': {'z'}, 'x': {'y'}}

    assert s.waiting == {'z': {'a', 'y'}}
    assert s.waiting_data == {'x': {'y'}, 'y': {'z'}, 'a': {'z'}, 'z': set()}

    assert s.who_wants == {'z': {'client'}, 'y': {'client'}}
    assert s.wants_what == {'client': {'y', 'z'}}

    assert list(s.ready) == ['a']
    assert s.in_play == {'a', 'x', 'y', 'z'}

    s.stop()


def test_update_state_with_processing(loop):
    s = Scheduler()
    s.start(0)
    s.add_worker(address=alice, ncores=1)
    s.update_graph(tasks={'x': 1, 'y': (inc, 'x'), 'z': (inc, 'y')},
                   keys=['z'],
                   dependencies={'y': {'x'}, 'x': set(), 'z': {'y'}},
                   client='client')

    s.mark_task_finished('x', alice, nbytes=10, type=dumps(int))

    assert s.waiting == {'z': {'y'}}
    assert s.waiting_data == {'x': {'y'}, 'y': {'z'}, 'z': set()}
    assert list(s.ready) == []

    assert s.who_wants == {'z': {'client'}}
    assert s.wants_what == {'client': {'z'}}

    assert s.who_has == {'x': {alice}}
    assert s.in_play == {'z', 'x', 'y'}

    s.update_graph(tasks={'a': (inc, 'x'), 'b': (add,'a','y'), 'c': (inc, 'z')},
                   keys=['b', 'c'],
                   dependencies={'a': {'x'}, 'b': {'a', 'y'}, 'c': {'z'}},
                   client='client')

    assert s.waiting == {'z': {'y'}, 'b': {'a', 'y'}, 'c': {'z'}}
    assert s.stacks[alice] == ['a']
    assert not s.ready
    assert s.waiting_data == {'x': {'y', 'a'}, 'y': {'z', 'b'}, 'z': {'c'},
                              'a': {'b'}, 'b': set(), 'c': set()}

    assert s.who_wants == {'b': {'client'}, 'c': {'client'}, 'z': {'client'}}
    assert s.wants_what == {'client': {'b', 'c', 'z'}}
    assert s.in_play == {'x', 'y', 'z', 'a', 'b', 'c'}

    s.stop()


def test_update_state_respects_data_in_memory(loop):
    s = Scheduler()
    s.start(0)
    s.add_worker(address=alice, ncores=1)
    s.update_graph(tasks={'x': 1, 'y': (inc, 'x')},
                   keys=['y'],
                   dependencies={'y': {'x'}, 'x': set()},
                   client='client')

    s.mark_task_finished('x', alice, nbytes=10, type=dumps(int))
    s.mark_task_finished('y', alice, nbytes=10, type=dumps(int))

    assert s.who_has == {'y': {alice}}

    s.update_graph(tasks={'x': 1, 'y': (inc, 'x'), 'z': (add, 'y', 'x')},
                   keys=['z'],
                   dependencies={'y': {'x'}, 'z': {'y', 'x'}},
                   client='client')

    assert s.waiting == {'z': {'x'}}
    assert s.processing[alice] == {'x'}  # x was released, need to recompute
    assert s.waiting_data == {'x': {'z'}, 'y': {'z'}, 'z': set()}
    assert s.who_wants == {'y': {'client'}, 'z': {'client'}}
    assert s.wants_what == {'client': {'y', 'z'}}
    assert s.in_play == {'x', 'y', 'z'}

    s.stop()


def test_update_state_supports_recomputing_released_results(loop):
    s = Scheduler()
    s.start(0)
    s.add_worker(address=alice, ncores=1)
    s.update_graph(tasks={'x': 1, 'y': (inc, 'x'), 'z': (inc, 'x')},
                   keys=['z'],
                   dependencies={'y': {'x'}, 'x': set(), 'z': {'y'}},
                   client='client')

    s.mark_task_finished('x', alice, nbytes=10, type=dumps(int))
    s.mark_task_finished('y', alice, nbytes=10, type=dumps(int))
    s.mark_task_finished('z', alice, nbytes=10, type=dumps(int))

    assert not s.waiting
    assert not s.ready
    assert s.waiting_data == {'z': set()}

    assert s.who_has == {'z': {alice}}

    s.update_graph(tasks={'x': 1, 'y': (inc, 'x')},
                   keys=['y'],
                   dependencies={'y': {'x'}},
                   client='client')

    assert s.waiting == {'y': {'x'}}
    assert s.waiting_data == {'x': {'y'}, 'y': set(), 'z': set()}
    assert s.who_wants == {'z': {'client'}, 'y': {'client'}}
    assert s.wants_what == {'client': {'y', 'z'}}
    assert s.processing[alice] == {'x'}

    s.stop()


def test_decide_worker_with_many_independent_leaves():
    dsk = merge({('y', i): (inc, ('x', i)) for i in range(100)},
                {('x', i): i for i in range(100)})
    dependencies, dependents = get_deps(dsk)
    stacks = {alice: [], bob: []}
    who_has = merge({('x', i * 2): {alice} for i in range(50)},
                    {('x', i * 2 + 1): {bob} for i in range(50)})
    nbytes = {k: 0 for k in who_has}

    for key in dsk:
        worker = decide_worker(dependencies, stacks, who_has, {}, set(), nbytes, key)
        stacks[worker].append(key)

    nhits = (len([k for k in stacks[alice] if alice in who_has[('x', k[1])]])
             + len([k for k in stacks[bob] if bob in who_has[('x', k[1])]]))

    assert nhits > 90


def test_decide_worker_with_restrictions():
    dependencies = {'x': set()}
    alice, bob, charlie = 'alice:8000', 'bob:8000', 'charlie:8000'
    stacks = {alice: [], bob: [], charlie: []}
    who_has = {}
    restrictions = {'x': {'alice', 'charlie'}}
    nbytes = {}
    result = decide_worker(dependencies, stacks, who_has, restrictions, set(), nbytes, 'x')
    assert result in {alice, charlie}


    stacks = {alice: [1, 2, 3], bob: [], charlie: [4, 5, 6]}
    result = decide_worker(dependencies, stacks, who_has, restrictions, set(), nbytes, 'x')
    assert result in {alice, charlie}

    dependencies = {'x': {'y'}}
    who_has = {'y': {bob}}
    nbytes = {'y': 0}
    result = decide_worker(dependencies, stacks, who_has, restrictions, set(), nbytes, 'x')
    assert result in {alice, charlie}


def test_decide_worker_with_loose_restrictions():
    dependencies = {'x': set()}
    alice, bob, charlie = 'alice:8000', 'bob:8000', 'charlie:8000'
    stacks = {alice: [1, 2, 3], bob: [], charlie: [1]}
    who_has = {}
    nbytes = {}
    restrictions = {'x': {'alice', 'charlie'}}

    result = decide_worker(dependencies, stacks, who_has, restrictions,
                           set(), nbytes, 'x')
    assert result == charlie

    result = decide_worker(dependencies, stacks, who_has, restrictions,
                           {'x'}, nbytes, 'x')
    assert result == charlie

    restrictions = {'x': {'david', 'ethel'}}
    with pytest.raises(ValueError):
        result = decide_worker(dependencies, stacks, who_has, restrictions,
                               set(), nbytes, 'x')

    restrictions = {'x': {'david', 'ethel'}}
    result = decide_worker(dependencies, stacks, who_has, restrictions,
                           {'x'}, nbytes, 'x')
    assert result == bob



def test_decide_worker_without_stacks():
    assert not decide_worker({'x': []}, [], {}, {}, set(), {}, 'x')


def test_validate_state():
    dsk = {'x': 1, 'y': (inc, 'x')}
    dependencies = {'x': set(), 'y': {'x'}}
    waiting = {'y': {'x'}}
    ready = deque(['x'])
    dependents = {'x': {'y'}, 'y': set()}
    waiting_data = {'x': {'y'}}
    who_has = dict()
    stacks = {alice: [], bob: []}
    processing = {alice: set(), bob: set()}
    finished_results = set()
    released = set()
    in_play = {'x', 'y'}
    who_wants = {'y': {'client'}}
    wants_what = {'client': {'y'}}

    validate_state(**locals())

    who_has['x'] = {alice}
    with pytest.raises(Exception):
        validate_state(**locals())

    ready.remove('x')
    with pytest.raises(Exception):
        validate_state(**locals())

    waiting['y'].remove('x')
    with pytest.raises(Exception):
        validate_state(**locals())

    del waiting['y']
    ready.appendleft('y')
    validate_state(**locals())

    stacks[alice].append('y')
    with pytest.raises(Exception):
        validate_state(**locals())

    ready.remove('y')
    validate_state(**locals())

    stacks[alice].pop()
    with pytest.raises(Exception):
        validate_state(**locals())

    processing[alice].add('y')
    validate_state(**locals())

    processing[alice].pop()
    with pytest.raises(Exception):
        validate_state(**locals())

    who_has['y'] = {alice}
    with pytest.raises(Exception):
        validate_state(**locals())

    finished_results.add('y')
    validate_state(**locals())


def test_fill_missing_data():
    dsk = {'x': 1, 'y': (inc, 'x'), 'z': (inc, 'y')}
    dependencies, dependents = get_deps(dsk)

    waiting = {}
    waiting_data = {'z': set()}

    who_wants = defaultdict(set, z={'client'})
    wants_what = defaultdict(set, client={'z'})
    who_has = {'z': {alice}}
    processing = set()
    released = set()
    in_play = {'z'}

    e_waiting = {'y': {'x'}, 'z': {'y'}}
    e_ready = {'x'}
    e_waiting_data = {'x': {'y'}, 'y': {'z'}, 'z': set()}
    e_in_play = {'x', 'y', 'z'}

    lost = {'z'}
    del who_has['z']
    in_play.remove('z')

    ready = heal_missing_data(dsk, dependencies, dependents, who_has, in_play, waiting,
            waiting_data, lost)

    assert waiting == e_waiting
    assert waiting_data == e_waiting_data
    assert ready == e_ready
    assert in_play == e_in_play


def div(x, y):
    return x / y

@gen_cluster()
def test_scheduler(s, a, b):
    stream = yield connect(s.ip, s.port)
    yield write(stream, {'op': 'register-client', 'client': b'ident'})
    msg = yield read(stream)
    assert msg['op'] == 'stream-start'

    # Test update graph
    yield write(stream, {'op': 'update-graph',
                         'tasks': valmap(dumps_task, {b'x': (inc, 1),
                                                      b'y': (inc, 'x'),
                                                      b'z': (inc, 'y')}),
                         'dependencies': {b'x': [],
                                          b'y': [b'x'],
                                          b'z': [b'y']},
                         'keys': [b'x', b'z'],
                         'client': b'ident'})
    while True:
        msg = yield read(stream)
        if msg['op'] == 'key-in-memory' and msg['key'] == b'z':
            break

    assert a.data.get(b'x') == 2 or b.data.get(b'x') == 2

    # Test erring tasks
    yield write(stream, {'op': 'update-graph',
                         'tasks': valmap(dumps_task, {b'a': (div, 1, 0),
                                                       b'b': (inc, b'a')}),
                         'dependencies': {b'a': [],
                                           b'b': ['a']},
                         'keys': [b'a', b'b'],
                         'client': b'ident'})

    while True:
        msg = yield read(stream)
        if msg['op'] == 'task-erred' and msg['key'] == b'b':
            break

    # Test missing data
    yield write(stream, {'op': 'missing-data', 'missing': [b'z']})

    while True:
        msg = yield read(stream)
        if msg['op'] == 'key-in-memory' and msg['key'] == b'z':
            break

    # Test missing data without being informed
    for w in [a, b]:
        if 'z' in w.data:
            del w.data['z']
    yield write(stream, {'op': 'update-graph',
                         'tasks': {b'zz': dumps_task((inc, b'z'))},
                         'dependencies': {b'zz': [b'z']},
                         'keys': [b'zz'],
                         'client': b'ident'})
    while True:
        msg = yield read(stream)
        if msg['op'] == 'key-in-memory' and msg['key'] == b'zz':
            break

    write(stream, {'op': 'close'})
    stream.close()


@gen_cluster()
def test_multi_queues(s, a, b):
    sched, report = Queue(), Queue()
    s.handle_queues(sched, report)

    msg = yield report.get()
    assert msg['op'] == 'stream-start'

    # Test update graph
    sched.put_nowait({'op': 'update-graph',
                      'tasks': valmap(dumps_task, {b'x': (inc, 1),
                                                   b'y': (inc, b'x'),
                                                   b'z': (inc, b'y')}),
                      'dependencies': {b'x': [],
                                       b'y': [b'x'],
                                       b'z': [b'y']},
                      'keys': [b'z']})

    while True:
        msg = yield report.get()
        if msg['op'] == 'key-in-memory' and msg['key'] == b'z':
            break

    slen, rlen = len(s.scheduler_queues), len(s.report_queues)
    sched2, report2 = Queue(), Queue()
    s.handle_queues(sched2, report2)
    assert slen + 1 == len(s.scheduler_queues)
    assert rlen + 1 == len(s.report_queues)

    sched2.put_nowait({'op': 'update-graph',
                       'tasks': {b'a': dumps_task((inc, 10))},
                       'dependencies': {b'a': []},
                       'keys': [b'a']})

    for q in [report, report2]:
        while True:
            msg = yield q.get()
            if msg['op'] == 'key-in-memory' and msg['key'] == b'a':
                break


@gen_cluster()
def test_server(s, a, b):
    stream = yield connect('127.0.0.1', s.port)
    yield write(stream, {'op': 'register-client', 'client': b'ident'})
    yield write(stream, {'op': 'update-graph',
                         'tasks': {b'x': dumps_task((inc, 1)),
                                   b'y': dumps_task((inc, b'x'))},
                         'dependencies': {b'x': [], b'y': [b'x']},
                         'keys': [b'y'],
                         'client': b'ident'})

    while True:
        msg = yield read(stream)
        if msg['op'] == 'key-in-memory' and msg['key'] == b'y':
            break

    yield write(stream, {'op': 'close-stream'})
    msg = yield read(stream)
    assert msg == {'op': 'stream-closed'}
    assert stream.closed()
    stream.close()


@gen_cluster()
def test_server_listens_to_other_ops(s, a, b):
    r = rpc(ip='127.0.0.1', port=s.port)
    ident = yield r.identity()
    assert ident['type'] == b'Scheduler'


@gen_cluster()
def test_remove_worker_from_scheduler(s, a, b):
    dsk = {('x', i): (inc, i) for i in range(10)}
    s.update_graph(tasks=valmap(dumps_task, dsk), keys=list(dsk),
                   dependencies={k: set() for k in dsk})
    assert s.ready
    assert not any(stack for stack in s.stacks.values())

    assert a.address_string in s.worker_queues
    s.remove_worker(address=a.address_string)
    assert a.address_string not in s.ncores
    assert len(s.ready) + len(s.processing[b.address_string]) == \
            len(dsk)  # b owns everything
    assert all(k in s.in_play for k in dsk)
    s.validate()


@gen_cluster()
def test_add_worker(s, a, b):
    w = Worker(s.ip, s.port, ncores=3, ip='127.0.0.1')
    w.data[b'x-5'] = 6
    w.data[b'y'] = 1
    yield w._start(0)

    dsk = {('x-%d' % i).encode(): (inc, i) for i in range(10)}
    s.update_graph(tasks=valmap(dumps_task, dsk), keys=list(dsk), client='client',
                   dependencies={k: set() for k in dsk})

    s.add_worker(address=w.address_string, keys=list(w.data),
                 ncores=w.ncores, services=s.services)

    for k in w.data:
        assert w.address_string in s.who_has[k]

    s.validate(allow_overlap=True)


@gen_cluster()
def test_feed(s, a, b):
    def func(scheduler):
        return dumps((scheduler.processing, scheduler.stacks))

    stream = yield connect(s.ip, s.port)
    yield write(stream, {'op': 'feed',
                         'function': dumps(func),
                         'interval': 0.01})

    for i in range(5):
        response = yield read(stream)
        expected = s.processing, s.stacks
        assert cloudpickle.loads(response) == expected

    stream.close()


@gen_cluster()
def test_feed_setup_teardown(s, a, b):
    def setup(scheduler):
        return 1

    def func(scheduler, state):
        assert state == 1
        return b'OK'

    def teardown(scheduler, state):
        scheduler.flag = 'done'

    stream = yield connect(s.ip, s.port)
    yield write(stream, {'op': 'feed',
                         'function': dumps(func),
                         'setup': dumps(setup),
                         'teardown': dumps(teardown),
                         'interval': 0.01})

    for i in range(5):
        response = yield read(stream)
        assert response == b'OK'

    stream.close()
    start = time()
    while not hasattr(s, 'flag'):
        yield gen.sleep(0.01)
        assert time() - start < 5


@gen_test()
def test_scheduler_as_center():
    s = Scheduler()
    done = s.start(0)
    a = Worker('127.0.0.1', s.port, ip='127.0.0.1', ncores=1)
    a.data.update({b'x': 1, b'y': 2})
    b = Worker('127.0.0.1', s.port, ip='127.0.0.1', ncores=2)
    b.data.update({b'y': 2, b'z': 3})
    c = Worker('127.0.0.1', s.port, ip='127.0.0.1', ncores=3)
    yield [w._start(0) for w in [a, b, c]]

    assert s.ncores == {w.address_string: w.ncores for w in [a, b, c]}
    assert s.who_has == {b'x': {a.address_string},
                         b'y': {a.address_string, b.address_string},
                         b'z': {b.address_string}}

    s.update_graph(tasks={b'a': dumps_task((inc, 1))},
                   keys=[b'a'],
                   dependencies={b'a': []})
    while not s.who_has[b'a']:
        yield gen.sleep(0.01)
    assert b'a' in a.data or b'a' in b.data or b'a' in c.data

    with ignoring(StreamClosedError):
        yield [w._close() for w in [a, b, c]]

    assert s.ncores == {}
    assert s.who_has == {}

    yield s.close()


@gen_cluster()
def test_delete_data(s, a, b):
    yield s.scatter(data=valmap(dumps, {b'x': 1, b'y': 2, b'z': 3}))
    assert set(a.data) | set(b.data) == {b'x', b'y', b'z'}
    assert merge(a.data, b.data) == {b'x': 1, b'y': 2, b'z': 3}

    s.delete_data(keys=[b'x', b'y'])
    yield s.clear_data_from_workers()
    assert set(a.data) | set(b.data) == {b'z'}


@gen_cluster()
def test_rpc(s, a, b):
    aa = s.rpc(ip=a.ip, port=a.port)
    aa2 = s.rpc(ip=a.ip, port=a.port)
    bb = s.rpc(ip=b.ip, port=b.port)
    assert aa is aa2
    assert aa is not bb


@gen_cluster()
def test_delete_callback(s, a, b):
    a.data[b'x'] = 1
    s.who_has[b'x'].add(a.address_string)
    s.has_what[a.address_string].add(b'x')

    s.delete_data(keys=[b'x'])
    assert not s.who_has[b'x']
    assert not s.has_what[b'y']
    assert a.data[b'x'] == 1  # still in memory
    assert s.deleted_keys == {a.address_string: {b'x'}}
    yield s.clear_data_from_workers()
    assert not s.deleted_keys
    assert not a.data

    assert s._delete_periodic_callback.is_running()


@gen_cluster()
def test_self_aliases(s, a, b):
    a.data[b'a'] = 1
    s.update_data(who_has={b'a': [a.address_string]},
                  nbytes={b'a': 10}, client=b'client')
    s.update_graph(tasks=valmap(dumps_task, {b'a': b'a', b'b': (inc, b'a')}),
                   keys=[b'b'], client=b'client',
                   dependencies={b'b': [b'a']})

    sched, report = Queue(), Queue()
    s.handle_queues(sched, report)
    msg = yield report.get()
    assert msg['op'] == 'stream-start'

    while True:
        msg = yield report.get()
        if msg['op'] == 'key-in-memory' and msg['key'] == b'b':
            break


@gen_cluster()
def test_filtered_communication(s, a, b):
    e = yield connect(ip=s.ip, port=s.port)
    f = yield connect(ip=s.ip, port=s.port)
    yield write(e, {'op': 'register-client', 'client': b'e'})
    yield write(f, {'op': 'register-client', 'client': b'f'})
    yield read(e)
    yield read(f)

    assert set(s.streams) == {b'e', b'f'}

    yield write(e, {'op': 'update-graph',
                    'tasks': {b'x': dumps_task((inc, 1)),
                              b'y': dumps_task((inc, b'x'))},
                    'dependencies': {b'x': [], b'y': [b'x']},
                    'client': b'e',
                    'keys': [b'y']})

    yield write(f, {'op': 'update-graph',
                    'tasks': {b'x': dumps_task((inc, 1)),
                              b'z': dumps_task((add, b'x', 10))},
                    'dependencies': {b'x': [], b'z': [b'x']},
                    'client': b'f',
                    'keys': [b'z']})

    msg = yield read(e)
    assert msg['op'] == 'key-in-memory'
    assert msg['key'] == b'y'
    msg = yield read(f)
    assert msg['op'] == 'key-in-memory'
    assert msg['key'] == b'z'


def test_maybe_complex():
    assert not _maybe_complex(1)
    assert not _maybe_complex('x')
    assert _maybe_complex((inc, 1))
    assert _maybe_complex([(inc, 1)])
    assert _maybe_complex([(inc, 1)])
    assert _maybe_complex({'x': (inc, 1)})


def test_dumps_function():
    a = dumps_function(inc)
    assert cloudpickle.loads(a)(10) == 11

    b = dumps_function(inc)
    assert a is b

    c = dumps_function(dec)
    assert a != c


def test_dumps_task():
    d = dumps_task((inc, 1))
    assert set(d) == {'function', 'args'}

    f = lambda x, y=2: x + y
    d = dumps_task((apply, f, (1,), {'y': 10}))
    assert cloudpickle.loads(d['function'])(1, 2) == 3
    assert cloudpickle.loads(d['args']) == (1,)
    assert cloudpickle.loads(d['kwargs']) == {'y': 10}

    d = dumps_task((apply, f, (1,)))
    assert cloudpickle.loads(d['function'])(1, 2) == 3
    assert cloudpickle.loads(d['args']) == (1,)
    assert set(d) == {'function', 'args'}


@gen_cluster()
def test_ready_remove_worker(s, a, b):
    s.add_client(client='client')
    s.update_graph(tasks={'x-%d' % i: dumps_task((inc, i)) for i in range(20)},
                   keys=['x-%d' % i for i in range(20)],
                   client='client',
                   dependencies={'x-%d' % i: [] for i in range(20)})

    assert all(len(s.processing[w]) == s.ncores[w]
                for w in s.ncores)
    assert not any(stack for stack in s.stacks.values())
    assert len(s.ready) + sum(map(len, s.processing.values())) == 20

    s.remove_worker(address=a.address_string)

    for collection in [s.ncores, s.stacks, s.processing]:
        assert set(collection) == {b.address_string}
    assert all(len(s.processing[w]) == s.ncores[w]
                for w in s.ncores)
    assert set(s.processing) == {b.address_string}
    assert not any(stack for stack in s.stacks.values())
    assert len(s.ready) + sum(map(len, s.processing.values())) == 20


@gen_cluster(Worker=Nanny)
def test_restart(s, a, b):
    s.add_client(client='client')
    s.update_graph(tasks={'x-%d' % i: dumps_task((inc, i)) for i in range(20)},
                   keys=['x-%d' % i for i in range(20)],
                   client='client',
                   dependencies={'x-%d' % i: [] for i in range(20)})

    assert len(s.ready) + sum(map(len, s.processing.values())) == 20
    assert s.ready

    yield s.restart()

    for c in [s.stacks, s.processing, s.ncores]:
        assert len(c) == 2

    for c in [s.stacks, s.processing]:
        assert not any(v for v in c.values())

    assert not s.ready
    assert not s.tasks
    assert not s.dependencies


@gen_cluster()
def test_ready_add_worker(s, a, b):
    s.add_client(client='client')
    s.update_graph(tasks={'x-%d' % i: dumps_task((inc, i)) for i in range(20)},
                   keys=['x-%d' % i for i in range(20)],
                   client='client',
                   dependencies={'x-%d' % i: [] for i in range(20)})

    assert all(len(s.processing[w]) == s.ncores[w]
                for w in s.ncores)
    assert len(s.ready) + sum(map(len, s.processing.values())) == 20

    w = Worker(s.ip, s.port, ncores=3, ip='127.0.0.1')
    w.listen(0)
    s.add_worker(address=w.address_string, ncores=w.ncores)

    assert w.address_string in s.ncores
    assert all(len(s.processing[w]) == s.ncores[w]
                for w in s.ncores)
    assert len(s.ready) + sum(map(len, s.processing.values())) == 20


def test_str_graph():
    dsk = {b'x': 1}
    assert str_graph(dsk) == dsk

    dsk = {('x', 1): (inc, 1)}
    assert str_graph(dsk) == {str(('x', 1)).encode(): (inc, 1)}

    dsk = {('x', 1): (inc, 1), ('x', 2): (inc, ('x', 1))}
    assert str_graph(dsk) == {str(('x', 1)).encode(): (inc, 1),
                              str(('x', 2)).encode(): (inc, str(('x', 1)).encode())}

    dsks = [{'x': 1},
            {('x', 1): (inc, 1), ('x', 2): (inc, ('x', 1))},
            {('x', 1): (sum, [1, 2, 3]),
             ('x', 2): (sum, [('x', 1), ('x', 1)])}]
    for dsk in dsks:
        sdsk = str_graph(dsk)
        keys = list(dsk)
        skeys = [str(k).encode() for k in keys]
        assert all(isinstance(k, bytes) for k in sdsk)
        assert dask.get(dsk, keys) == dask.get(sdsk, skeys)
