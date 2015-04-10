"""
Useful utilities for testing
"""
from contextlib import contextmanager
import logging
import os
from os.path import join, abspath, dirname
import tempfile
import simplejson
import nose.tools as nt

from stolos.initializer import initialize as _initialize
from stolos import util

from stolos import api
from stolos import queue_backend as qb
from stolos import dag_tools as dt
from stolos import configuration_backend as cb

from stolos import zookeeper_tools as zkt


TASKS_JSON_READ_FP = join(dirname(abspath(__file__)), 'examples/tasks.json')


def _smart_run(func, args, kwargs):
    """Given a function definition, determine which of the args and
    kwargs are relevant and then execute the function"""
    fc = func.func_code
    kwargs2 = {
        k: kwargs[k]
        for k in kwargs if k in fc.co_varnames[:fc.co_nlocals]}
    args2 = args[:fc.co_nlocals - len(kwargs2)]
    func(*args2, **kwargs2)


def _with_setup(setup=None, teardown=None, params=False):
    """Decorator to add setup and/or teardown methods to a test function

    `setup` - setup function to run before calling a test function
    `teardown` - teardown function to run after a test function returns
    `params` - (boolean) whether to enable a special mode where:
      - setup will return args and kwargs
      - the test function and teardown will may define any of the values
        returned by setup in their function definitions.


      def setup():
          return dict(abc=123, def=456)

      @_with_setup(setup, params=True)
      def test_something(abc):
          assert abc == 123
    """
    def decorate(func, setup=setup, teardown=teardown):
        args = []
        kwargs = {}
        if params:
            @nt.make_decorator(func)
            def func_wrapped():
                _smart_run(func, args, kwargs)
        else:
            func_wrapped = func
        if setup:
            def setup_wrapped():
                if params:
                    rv = setup(func.func_name)
                else:
                    rv = setup()
                if rv is not None:
                    args.extend(rv[0])
                    kwargs.update(rv[1])

            if hasattr(func, 'setup'):
                _old_s = func.setup

                def _s():
                    setup_wrapped()
                    _old_s()
                func_wrapped.setup = _s
            else:
                if params:
                    func_wrapped.setup = setup_wrapped
                else:
                    func_wrapped.setup = setup

        if teardown:
            if hasattr(func, 'teardown'):
                _old_t = func.teardown

                def _t():
                    _old_t()
                    _smart_run(teardown, args, kwargs)
                func_wrapped.teardown = _t
            else:
                if params:
                    def _sr():
                        return _smart_run(teardown, args, kwargs)
                    func_wrapped.teardown = _sr
                else:
                    func_wrapped.teardown = teardown
        return func_wrapped
    return decorate


def _create_tasks_json(func_name, inject={}):
    """
    Create a new default tasks.json file
    This of this like a useful setup() operation before tests.

    `inject` - (dct) (re)define new tasks to add to the tasks graph
    mark this copy as the new default.
    `func_name` - the name of the function we're testing

    """
    tasks_config = simplejson.load(open(TASKS_JSON_READ_FP))

    # change the name of all tasks to include the func_name
    tc1 = simplejson.dumps(tasks_config)
    tc2 = simplejson.dumps(inject)
    renames = [
        (k, 'test_stolos/%s/%s' % (func_name, k))
        for k in (x for y in (tasks_config, inject) for x in y)
        if not k.startswith('test_stolos/%s/' % func_name)]
    for k, new_k in renames:
        tc1 = tc1.replace(simplejson.dumps(k), simplejson.dumps(new_k))
        tc2 = tc2.replace(simplejson.dumps(k), simplejson.dumps(new_k))

    tc3 = simplejson.loads(tc1)
    tc3.update(simplejson.loads(tc2))

    f = tempfile.mkstemp(prefix='tasks_json', suffix=func_name)[1]
    with open(f, 'w') as fout:
        fout.write(simplejson.dumps(tc3))
    return f, renames


def _setup_func(func_name):
    """Code that runs just before each test and configures a tasks.json file
    for each test.  The tasks.json tmp files are stored in a global var."""
    log = util.configure_logging(logging.getLogger(
        'stolos.tests.%s' % func_name))

    tasks_json_tmpfile, renames = _create_tasks_json(func_name)
    zk = api.get_zkclient('localhost:2181')
    zk.delete('test_stolos/%s/' % func_name, recursive=True)
    _initialize([cb, dt, qb], args=['--tasks_json', tasks_json_tmpfile])

    rv = dict(
        log=log,
        zk=zk,
        job_id1='20140606_1111_profile',
        job_id2='20140606_2222_profile',
        job_id3='20140604_1111_profile',
        func_name=func_name,
        tasks_json_tmpfile=tasks_json_tmpfile,
    )
    rv.update(renames)

    return ((), rv)


def _teardown_func(func_name, tasks_json_tmpfile):
    """Code that runs just after each test"""
    os.remove(tasks_json_tmpfile)


def with_setup(func, setup_func=_setup_func, teardown_func=_teardown_func):
    """Decorator that wraps a test function and provides setup() and teardown()
    functionality.  The decorated func may define as a parameter any kwargs
    returned by setup(func_name).  The setup() is func.name aware but not
    necessarily module aware, so be careful if using the same test function in
    different test modules
    """
    return _with_setup(setup_func, teardown_func, True)(func)


@contextmanager
def inject_into_dag(func_name, inject_dct):
    """Update (add or replace) tasks in dag with new task config.
    Assumes that the config we're using is the JSONMapping
    """
    if not all(k.startswith('test_stolos/%s/' % func_name)
               for k in inject_dct):
        raise UserWarning(
            "inject_into_dag can only inject app_names that"
            " have the correct prefix:  test_stolos/%s/{app_name}" % func_name)
    f = _create_tasks_json(func_name, inject=inject_dct)[0]
    _initialize([cb, dt, qb], args=['--tasks_json', f])

    # verify injection worked
    dg = cb.get_tasks_config().to_dict()
    for k, v in inject_dct.items():
        assert dg[k] == v, (
            "test code: inject_into_dag didn't insert the new tasks?")
    yield
    os.remove(f)


def enqueue(app_name, job_id, zk, validate_queued=True):
    # initialize job
    api.maybe_add_subtask(app_name, job_id, zk)
    api.maybe_add_subtask(app_name, job_id, zk)
    # verify initial conditions
    if validate_queued:
        validate_one_queued_task(zk, app_name, job_id)


def validate_zero_queued_task(zk, app_name):
    if zk.exists(join(app_name, 'entries')):
        nt.assert_equal(
            0, len(zk.get_children(join(app_name, 'entries'))))


def validate_zero_completed_task(zk, app_name):
    if zk.exists(join(app_name, 'all_subtasks')):
        nt.assert_equal(
            0, len(zk.get_children(join(app_name, 'all_subtasks'))))


def validate_one_failed_task(zk, app_name, job_id):
    status = get_zk_status(zk, app_name, job_id)
    nt.assert_equal(status['num_execute_locks'], 0)
    nt.assert_equal(status['num_add_locks'], 0)
    nt.assert_equal(status['in_queue'], False)
    # nt.assert_equal(status['app_qsize'], 1)
    nt.assert_equal(status['state'], 'failed')


def validate_one_queued_executing_task(zk, app_name, job_id):
    status = get_zk_status(zk, app_name, job_id)
    nt.assert_equal(status['num_execute_locks'], 1)
    nt.assert_equal(status['num_add_locks'], 0)
    nt.assert_equal(status['in_queue'], True)
    nt.assert_equal(status['app_qsize'], 1)
    nt.assert_equal(status['state'], 'pending')


def validate_one_queued_task(zk, app_name, job_id):
    return validate_n_queued_task(zk, app_name, job_id)


def validate_one_completed_task(zk, app_name, job_id):
    status = get_zk_status(zk, app_name, job_id)
    nt.assert_equal(status['num_execute_locks'], 0)
    nt.assert_equal(status['num_add_locks'], 0)
    nt.assert_equal(status['in_queue'], False)
    nt.assert_equal(status['app_qsize'], 0)
    nt.assert_equal(status['state'], 'completed')


def validate_one_skipped_task(zk, app_name, job_id):
    status = get_zk_status(zk, app_name, job_id)
    nt.assert_equal(status['num_execute_locks'], 0)
    nt.assert_equal(status['num_add_locks'], 0)
    nt.assert_equal(status['in_queue'], False)
    nt.assert_equal(status['app_qsize'], 0)
    nt.assert_equal(status['state'], 'skipped')


def validate_n_queued_task(zk, app_name, *job_ids):
    for job_id in job_ids:
        status = get_zk_status(zk, app_name, job_id)
        nt.assert_equal(status['num_execute_locks'], 0, job_id)
        nt.assert_equal(status['num_add_locks'], 0, job_id)
        nt.assert_equal(status['in_queue'], True, job_id)
        nt.assert_equal(status['app_qsize'], len(job_ids), job_id)
        nt.assert_equal(status['state'], 'pending', job_id)


def cycle_queue(zk, app_name):
    """Get item from queue, put at back of queue and return item"""
    q = zk.LockingQueue(app_name)
    item = q.get()
    q.put(item)
    q.consume()
    return item


def consume_queue(zk, app_name, timeout=1):
    q = zk.LockingQueue(app_name)
    item = q.get(timeout=timeout)
    q.consume()
    return item


def get_zk_status(zk, app_name, job_id):
    path = zkt._get_zookeeper_path(app_name, job_id)
    elockpath = join(path, 'execute_lock')
    alockpath = join(path, 'add_lock')
    entriespath = join(app_name, 'entries')
    return {
        'num_add_locks': len(
            zk.exists(alockpath) and zk.get_children(alockpath) or []),
        'num_execute_locks': len(
            zk.exists(elockpath) and zk.get_children(elockpath) or []),
        'in_queue': (
            any(zk.get(join(app_name, 'entries', x))[0] == job_id
                for x in zk.get_children(entriespath))
            if zk.exists(entriespath) else False),
        'app_qsize': (
            len(zk.get_children(entriespath))
            if zk.exists(entriespath) else 0),
        'state': zk.get(path)[0],
    }
