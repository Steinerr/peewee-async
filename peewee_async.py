"""
peewee-async
============

Asynchronous interface for `peewee`_ ORM powered by `asyncio`_:
https://github.com/05bit/peewee-async

.. _peewee: https://github.com/coleifer/peewee
.. _asyncio: https://docs.python.org/3/library/asyncio.html

Licensed under The MIT License (MIT)

Copyright (c) 2014, Alexey Kinev <rudy@05bit.com>

"""
import asyncio
import uuid

import aiopg
import peewee
import contextlib

__version__ = '0.4.0'

__all__ = [
    # Queries
    'execute',

    # Object actions
    'get_object',
    'create_object',
    'delete_object',
    'update_object',

    # Database backends
    'PostgresqlDatabase',
    'PooledPostgresqlDatabase',

    # Sync calls helpers
    'sync_unwanted',
    'UnwantedSyncQueryError',

    # Aggregation:
    'count',
    'scalar',
]


@asyncio.coroutine
def execute(query):
    """Execute *SELECT*, *INSERT*, *UPDATE* or *DELETE* query asyncronously.

    :param query: peewee query instance created with ``Model.select()``,
                  ``Model.update()`` etc.
    :return: result depends on query type, it's the same as for sync ``query.execute()``
    """
    if isinstance(query, peewee.UpdateQuery):
        coroutine = update
    elif isinstance(query, peewee.InsertQuery):
        coroutine = insert
    elif isinstance(query, peewee.DeleteQuery):
        coroutine = delete
    elif isinstance(query, peewee.RawQuery):
        coroutine = raw_query
    else:
        coroutine = select
    return (yield from coroutine(query))


@asyncio.coroutine
def create_object(model, **data):
    """Create object asynchronously.
    
    :param model: mode class
    :param data: data for initializing object
    :return: new object saved to database
    """
    obj = model(**data)

    # NOTE! Here are internals involved:
    #
    # - obj._data
    # - obj._dirty
    # - obj._get_pk_value()
    # - obj._set_pk_value()
    #
    field_dict = dict(obj._data)
    pk = obj._get_pk_value()
    pk_from_cursor = yield from insert(obj.insert(**field_dict))
    if pk_from_cursor is not None:
        pk = pk_from_cursor
    obj._set_pk_value(pk)  # Do not overwrite current ID with None.
    
    obj._dirty.clear()
    obj.prepared()

    return obj


@asyncio.coroutine
def get_object(source, *args):
    """Get object asynchronously.

    :param source: mode class or query to get object from
    :param args: lookup parameters
    :return: model instance or raises ``peewee.DoesNotExist`` if object not found
    """
    if isinstance(source, peewee.Query):
        base_query = source
        model = base_query.model_class
    else:
        base_query = source.select()
        model = source

    # Return first object from query
    for obj in (yield from select(base_query.where(*args).limit(1))):
        return obj

    # No objects found
    raise model.DoesNotExist


@asyncio.coroutine
def delete_object(obj, recursive=False, delete_nullable=False):
    """Delete object asynchronously.

    :param obj: object to delete
    :param recursive: if ``True`` also delete all other objects depends on object
    :param delete_nullable: if `True` and delete is recursive then delete even 'nullable' dependencies

    For details please check out `Model.delete_instance()`_ in peewee docs.

    .. _Model.delete_instance(): http://peewee.readthedocs.org/en/latest/peewee/api.html#Model.delete_instance
    """
    # Here are private calls involved:
    # - obj._pk_expr()
    if recursive:
        dependencies = obj.dependencies(delete_nullable)
        for query, fk in reversed(list(dependencies)):
            model = fk.model_class
            if fk.null and not delete_nullable:
                yield from update(model.update(**{fk.name: None}).where(query))
            else:
                yield from delete(model.delete().where(query))
    result = yield from delete(obj.delete().where(obj._pk_expr()))
    return result


@asyncio.coroutine
def update_object(obj, only=None):
    """Update object asynchronously.

    :param obj: object to update
    :param only: list or tuple of fields to updata, is `None` then all fields updated

    This function does the same as `Model.save()`_ for already saved object, but it
    doesn't invoke ``save()`` method on model class. That is important to know if you
    overrided save method for your model.

    .. _Model.save(): http://peewee.readthedocs.org/en/latest/peewee/api.html#Model.save
    """
    # Here are private calls involved:
    #
    # - obj._data
    # - obj._meta
    # - obj._prune_fields()
    # - obj._pk_expr()
    # - obj._dirty.clear()
    #
    field_dict = dict(obj._data)
    pk_field = obj._meta.primary_key

    if only:
        field_dict = obj._prune_fields(field_dict, only)

    if not isinstance(pk_field, peewee.CompositeKey):
        field_dict.pop(pk_field.name, None)
    else:
        field_dict = obj._prune_fields(field_dict, obj.dirty_fields)
    rows = yield from update(obj.update(**field_dict).where(obj._pk_expr()))

    obj._dirty.clear()
    return rows


@asyncio.coroutine
def select(query):
    """Perform SELECT query asynchronously.

    NOTE! It relies on internal peewee logic for generating
    results from queries and well, a bit hacky.
    """
    assert isinstance(query, peewee.SelectQuery),\
        ("Error, trying to run select coroutine"
         "with wrong query class %s" % str(query))

    # Perform *real* async query
    query = query.clone()
    cursor = yield from _execute_query_async(query)

    # Perform *fake* query: we only need a result wrapper
    # here, not the query result itself:
    query._execute = lambda: None
    result_wrapper = query.execute()

    # Fetch result
    result = AsyncQueryResult(result_wrapper=result_wrapper, cursor=cursor)
    try:
        while True:
            yield from result.fetchone()
    except GeneratorExit:
        pass

    # Release cursor and return
    cursor.release()
    return result


@asyncio.coroutine
def insert(query):
    """Perform INSERT query asynchronously. Returns last insert ID.
    """
    assert isinstance(query, peewee.InsertQuery),\
        ("Error, trying to run insert coroutine"
         "with wrong query class %s" % str(query))

    cursor = yield from _execute_query_async(query)

    if query.is_insert_returning:
        result = (yield from cursor.fetchone())[0]
    else:
        result = yield from query.database.last_insert_id_async(
            cursor, query.model_class)

    cursor.release()
    return result


@asyncio.coroutine
def update(query):
    """Perform UPDATE query asynchronously. Returns number of rows updated.
    """
    assert isinstance(query, peewee.UpdateQuery),\
        ("Error, trying to run update coroutine"
         "with wrong query class %s" % str(query))

    cursor = yield from _execute_query_async(query)
    rowcount = cursor.rowcount

    cursor.release()
    return rowcount


@asyncio.coroutine
def delete(query):
    """Perform DELETE query asynchronously. Returns number of rows deleted.
    """
    assert isinstance(query, peewee.DeleteQuery),\
        ("Error, trying to run delete coroutine"
         "with wrong query class %s" % str(query))

    cursor = yield from _execute_query_async(query)
    rowcount = cursor.rowcount

    cursor.release()
    return rowcount


@asyncio.coroutine
def count(query, clear_limit=False):
    """Perform *COUNT* aggregated query asynchronously.

    :return: number of objects in ``select()`` query
    """
    if query._distinct or query._group_by or query._limit or query._offset:
        # wrapped_count()
        clone = query.order_by()
        if clear_limit:
            clone._limit = clone._offset = None

        sql, params = clone.sql()
        wrapped = 'SELECT COUNT(1) FROM (%s) AS wrapped_select' % sql
        raw_query = query.model_class.raw(wrapped, *params)
        return (yield from scalar(raw_query)) or 0
    else:
        # simple count()
        query = query.order_by()
        query._select = [peewee.fn.Count(peewee.SQL('*'))]
        return (yield from scalar(query)) or 0


@asyncio.coroutine
def scalar(query, as_tuple=False):
    """Get single value from ``select()`` query, i.e. for aggregation.

    :return: result is the same as after sync ``query.scalar()`` call
    """
    cursor = yield from _execute_query_async(query)
    row = yield from cursor.fetchone()

    cursor.release()

    if row and not as_tuple:
        return row[0]
    else:
        return row


@asyncio.coroutine
def raw_query(query):
    assert isinstance(query, peewee.RawQuery),\
        ("Error, trying to run delete coroutine"
         "with wrong query class %s" % str(query))

    cursor = yield from _execute_query_async(query)
    # Perform *fake* query: we only need a result wrapper
    # here, not the query result itself:
    query._execute = lambda: None
    result_wrapper = query.execute()

    # Fetch result
    result = AsyncQueryResult(result_wrapper=result_wrapper, cursor=cursor)
    try:
        while True:
            yield from result.fetchone()
    except GeneratorExit:
        pass

    # Release cursor and return
    cursor.release()
    return result


class AsyncQueryResult:
    """Async query results wrapper for async `select()`. Internally uses
    results wrapper produced by sync peewee select query.

    Arguments:

        result_wrapper -- empty results wrapper produced by sync `execute()`
        call cursor -- async cursor just executed query

    To retrieve results after async fetching just iterate over this class
    instance, like you generally iterate over sync results wrapper.
    """
    def __init__(self, result_wrapper=None, cursor=None):
        self._result = []
        self._initialized = False
        self._result_wrapper = result_wrapper
        self._cursor = cursor

    def __iter__(self):
        return iter(self._result)

    def __getitem__(self, key):
        return self._result[key]

    def __len__(self):
        return len(self._result)

    @asyncio.coroutine
    def fetchone(self):
        row = yield from self._cursor.fetchone()

        if not row:
            self._cursor = None
            self._result_wrapper = None
            raise GeneratorExit
        elif not self._initialized:
            self._result_wrapper.initialize(self._cursor.description)
            self._initialized = True

        obj = self._result_wrapper.process_row(row)
        self._result.append(obj)


class AsyncConnection:
    """Asynchronous single database connection wrapper.
    """
    def __init__(self, loop, database, timeout, **kwargs):
        self._conn = None
        self._loop = loop if loop else asyncio.get_event_loop()
        self.database = database
        self.timeout = timeout
        self.dsn, self.connect_kwargs = _compose_dsn(self.database, **kwargs)

    @asyncio.coroutine
    def connect(self):
        """Connect asynchronously.
        """
        self._conn = yield from aiopg.connect(
            dsn=self.dsn, timeout=self.timeout, loop=self._loop,
            **self.connect_kwargs)

    @asyncio.coroutine
    def cursor(self, *args, **kwargs):
        """Get connection cursor asynchronously.
        """
        cursor = yield from self._conn.cursor(*args, **kwargs)
        cursor.release = lambda: None
        return cursor

    def close(self):
        """Close connection.
        """
        self._conn.close()


class PooledAsyncConnection:
    """
    Asynchronous database connection pool wrapper.
    """
    def __init__(self, loop, database, timeout, **kwargs):
        self._pool = None
        self._loop = loop if loop else asyncio.get_event_loop()
        self.database = database
        self.timeout = timeout
        self.dsn, self.connect_kwargs = _compose_dsn(self.database, **kwargs)

    @asyncio.coroutine
    def connect(self):
        """Create connection pool asynchronously.
        """
        self._pool = yield from aiopg.create_pool(
            dsn=self.dsn,
            loop=self._loop,
            timeout=self.timeout,
            **self.connect_kwargs)

    @asyncio.coroutine
    def cursor(self, *args, **kwargs):
        """Get cursor for connection from pool.
        """
        conn = yield from self._pool.acquire()
        cursor = yield from conn.cursor(*args, **kwargs)
        cursor.release = lambda: all((cursor.close(), self._pool.release(conn)))
        return cursor

    def close(self):
        """Terminate all pool connections.
        """
        self._pool.terminate()


class transaction(peewee._callable_context_manager):
    """Asynchronous context manager (`async with`), similar to
    `peewee.transaction()`.
    """
    def __init__(self, db):
        self.db = db

    @asyncio.coroutine
    def _begin(self):
        return (yield from _run_sql(self.db, "BEGIN"))

    @asyncio.coroutine
    def commit(self, begin=True):
        yield from _run_sql(self.db, "COMMIT")
        if begin:
            yield from self._begin()

    @asyncio.coroutine
    def rollback(self, begin=True):
        yield from _run_sql(self.db, "ROLLBACK")
        if begin:
            yield from self._begin()

    @asyncio.coroutine
    def __aenter__(self):
        self._orig = self.db.get_autocommit()
        self.db.set_autocommit(False)
        if self.db.transaction_depth() == 0:
            yield from self._begin()
        self.db.push_transaction(self)
        return self

    @asyncio.coroutine
    def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type:
                yield from self.rollback(False)
            elif self.db.transaction_depth() == 1:
                try:
                    yield from self.commit(False)
                except:
                    yield from self.rollback(False)
                    raise
        finally:
            self.db.set_autocommit(self._orig)
            self.db.pop_transaction()


class savepoint(peewee._callable_context_manager):
    """Asynchronous context manager (`async with`), similar to
    `peewee.savepoint()`.
    """
    def __init__(self, db, sid=None):
        self.db = db
        _compiler = db.compiler()
        self.sid = sid or 's' + uuid.uuid4().hex
        self.quoted_sid = _compiler.quote(self.sid)

    @asyncio.coroutine
    def _execute(self, query):
        yield from _run_sql(query)

    @asyncio.coroutine
    def commit(self):
        yield from self._execute('RELEASE SAVEPOINT %s;' % self.quoted_sid)

    @asyncio.coroutine
    def rollback(self):
        yield from self._execute('ROLLBACK TO SAVEPOINT %s;' % self.quoted_sid)

    @asyncio.coroutine
    def __aenter__(self):
        self._orig_autocommit = self.db.get_autocommit()
        self.db.set_autocommit(False)
        yield from self._execute('SAVEPOINT %s;' % self.quoted_sid)
        return self

    @asyncio.coroutine
    def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type:
                yield from self.rollback()
            else:
                try:
                    yield from self.commit()
                except:
                    yield from self.rollback()
                    raise
        finally:
            self.db.set_autocommit(self._orig_autocommit)


class atomic(peewee._callable_context_manager):
    """Asynchronous context manager (`async with`), similar to
    `peewee.atomic()`.
    """
    def __init__(self, db):
        self.db = db

    @asyncio.coroutine
    def __aenter__(self):
        if self.db.transaction_depth() == 0:
            self._helper = self.db.transaction_async()
        else:
            self._helper = self.db.savepoint_async()
        yield from self._helper.__aenter__()

    @asyncio.coroutine
    def __aexit__(self, exc_type, exc_val, exc_tb):
        yield from self._helper.__aexit__(exc_type, exc_val, exc_tb)


class AsyncPostgresqlMixin:
    """Mixin for peewee database class providing extra methods
    for managing async connection.
    """
    def init_async(self, conn_cls=AsyncConnection, **kwargs):
        self.allow_sync = True

        self._loop = None
        self._async_conn = None
        self._async_conn_cls = conn_cls
        self._async_kwargs = {
            'enable_json': False,
            'enable_hstore': False,
        }
        self._async_kwargs.update(kwargs)

    @asyncio.coroutine
    def connect_async(self, loop=None, timeout=None):
        """Set up async connection on specified event loop or
        on default event loop.
        """
        if not self._async_conn:
            self._loop = loop if loop else asyncio.get_event_loop()
            self._async_conn = self._async_conn_cls(
                self._loop,
                self.database,
                timeout if timeout else aiopg.DEFAULT_TIMEOUT,
                **self._async_kwargs)
            yield from self._async_conn.connect()

    @asyncio.coroutine
    def last_insert_id_async(self, cursor, model):
        """Get ID of last inserted row.

        NOTE: it's a copy-paste, not sure how to make it better
        https://github.com/05bit/peewee/blob/2.3.2/peewee.py#L2907
        """
        meta = model._meta
        schema = ''
        if meta.schema:
            schema = '%s.' % meta.schema

        if meta.primary_key.sequence:
            seq = meta.primary_key.sequence
        elif meta.auto_increment:
            seq = '%s_%s_seq' % (meta.db_table, meta.primary_key.db_column)
        else:
            seq = None

        if seq:
            yield from cursor.execute("SELECT CURRVAL('%s\"%s\"')" % (schema, seq))
            result = (yield from cursor.fetchone())[0]
            return result

    def atomic_async(self):
        """Similar to peewee `Database.atomic()` method, but returns
        asynchronous context manager.
        """
        return atomic(self)

    def transaction_async(self):
        """Similar to peewee `Database.transaction()` method, but returns
        asynchronous context manager.
        """
        return transaction(self)

    def savepoint_async(self, sid=None):
        """Similar to peewee `Database.savepoint()` method, but returns
        asynchronous context manager.
        """
        return savepoint(self, sid=sid)

    def close(self):
        """Close both sync and async connections.
        """
        super().close()

        if self._async_conn:
            self._async_conn.close()
            self._async_conn = None
            self._loop = None

    def execute_sql(self, *args, **kwargs):
        """Sync execute SQL query. If this query is performing within
        `sync_unwanted()` context, then `UnwantedSyncQueryError` exception
        is raised.
        """
        if not self.allow_sync:
            raise UnwantedSyncQueryError("Error, unwanted sync query",
                                         args, kwargs)
        return super().execute_sql(*args, **kwargs)


class PostgresqlDatabase(AsyncPostgresqlMixin, peewee.PostgresqlDatabase):
    """PosgreSQL database driver providing **single drop-in sync** connection
    and **single async connection** interface.

    See also:
    http://peewee.readthedocs.org/en/latest/peewee/api.html#PostgresqlDatabase
    """
    def __init__(self, database, threadlocals=True, autocommit=True,
                 fields=None, ops=None, autorollback=True, **kwargs):
        super().__init__(database, threadlocals=True, autocommit=autocommit,
                         fields=fields, ops=ops, autorollback=autorollback,
                         **kwargs)

        self.init_async(**self.connect_kwargs)


class PooledPostgresqlDatabase(AsyncPostgresqlMixin, peewee.PostgresqlDatabase):
    """PosgreSQL database driver providing **single drop-in sync**
    connection and **async connections pool** interface.

    :param max_connections: connections pool size

    See also:
    http://peewee.readthedocs.org/en/latest/peewee/api.html#PostgresqlDatabase
    """
    def __init__(self, database, threadlocals=True, autocommit=True,
                 fields=None, ops=None, autorollback=True, max_connections=20,
                 **kwargs):
        super().__init__(database, threadlocals=True, autocommit=autocommit,
                         fields=fields, ops=ops, autorollback=autorollback,
                         **kwargs)

        self.init_async(conn_cls=PooledAsyncConnection, minsize=1,
                        maxsize=max_connections, **self.connect_kwargs)


@contextlib.contextmanager
def sync_unwanted(database):
    """Context manager for preventing unwanted sync queries.
    `UnwantedSyncQueryError` exception will raise on such query.
    """
    old_allow_sync = database.allow_sync
    database.allow_sync = False
    yield
    database.allow_sync = old_allow_sync


class UnwantedSyncQueryError(Exception):
    """Exception which is raised when performing unwanted sync query.
    """
    pass


@asyncio.coroutine
def _run_sql(db, operation, *args, **kwargs):
    """Run SQL operation (query or command) against database.
    """    
    assert db._async_conn, "Error, no async database connection."
    cursor = yield from db._async_conn.cursor()
    try:
        yield from cursor.execute(operation, *args, **kwargs)
    except Exception as e:
        cursor.release()
        raise e
    return cursor


@asyncio.coroutine
def _execute_query_async(query):
    """Execute query and return cursor object.
    """
    db = query.database
    return (yield from _run_sql(db, *query.sql()))


def _compose_dsn(dbname, **kwargs):
    """Compose DSN string by set of connection parameters.
    Extract parameters: dbname, user, password, host, port.

    Return DSN string and remain parameters dict.
    """
    dsn = 'dbname=%s' % dbname
    for k in ('user', 'password', 'host', 'port'):
        v = kwargs.pop(k, None)
        if v:
            dsn += ' %s=%s' % (k, v)
    return dsn, kwargs


@asyncio.coroutine
def prefetch(sq, *subqueries):
    """Asynchronous version of the prefetch function from peewee.

    Returns Query that has already cached data.
    """

    # This code is copied from peewee.prefetch and adopted to use async execute

    if not subqueries:
        return sq
    fixed_queries = peewee.prefetch_add_subquery(sq, subqueries)

    deps = {}
    rel_map = {}
    for prefetch_result in reversed(fixed_queries):
        query_model = prefetch_result.model
        if prefetch_result.fields:
            for rel_model in prefetch_result.rel_models:
                rel_map.setdefault(rel_model, [])
                rel_map[rel_model].append(prefetch_result)

        deps[query_model] = {}
        id_map = deps[query_model]
        has_relations = bool(rel_map.get(query_model))

        # This is hack, because peewee async execute do a copy of query and do not change state of query
        # comparing to what real peewee is doing when execute method is called
        prefetch_result.query._qr = yield from execute(prefetch_result.query)
        prefetch_result.query._dirty = False

        for instance in prefetch_result.query._qr:
            if prefetch_result.fields:
                prefetch_result.store_instance(instance, id_map)
            if has_relations:
                for rel in rel_map[query_model]:
                    rel.populate_instance(instance, deps[rel.model])

    return prefetch_result.query
