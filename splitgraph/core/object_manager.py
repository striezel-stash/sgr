"""Functions related to creating, deleting and keeping track of physical Splitgraph objects."""
import itertools
import logging
import math
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime as dt, timedelta, date
from decimal import Decimal
from random import getrandbits

from psycopg2 import IntegrityError
from psycopg2.extras import Json
from psycopg2.sql import SQL, Identifier
from splitgraph.config import SPLITGRAPH_META_SCHEMA, CONFIG
from splitgraph.engine import ResultShape, switch_engine
from splitgraph.engine.postgres.engine import SG_UD_FLAG
from splitgraph.exceptions import SplitGraphException
from splitgraph.hooks.external_objects import get_external_object_handler

from ._common import META_TABLES, select, insert, pretty_size

# PG types we can run max/min on
_PG_INDEXABLE_TYPES = [
    "bigint",
    "bigserial",
    "bit",
    "character",
    "character varying",
    "cidr",
    "date",
    "double precision",
    "inet",
    "integer",
    "money",
    "numeric",
    "real",
    "smallint",
    "smallserial",
    "serial",
    "text",
    "time",
    "time without time zone",
    "time with time zone",
    "timestamp",
    "timestamp without time zone",
    "timestamp with time zone"]


class Tracer:
    """
    Accumulates events and returns the times between them.
    """

    def __init__(self):
        self.start_time = dt.now()
        self.events = []

    def log(self, event):
        self.events.append((dt.now(), event))

    def __str__(self):
        result = ""
        prev = self.start_time
        for event_time, event in self.events:
            result += "\n%s: %.3f" % (event, (event_time - prev).total_seconds())
            prev = event_time
        result += "\nTotal: %.3f" % (self.events[-1][0] - self.start_time).total_seconds()
        return result[1:]


class ObjectManager:
    """A Splitgraph metadata-aware class that keeps track of objects on a given engine.
    Backed by ObjectEngine to move physical objects around and run metadata queries."""

    def __init__(self, object_engine):
        """
        :param object_engine: An ObjectEngine that will be used as a backing store for the
            objects.
        """
        self.object_engine = object_engine

        # Cache size in bytes
        self.cache_size = float(CONFIG['SG_OBJECT_CACHE_SIZE']) * 1024 * 1024

        # 0 to infinity; higher means objects with smaller sizes are more likely to
        # get evicted than objects that haven't been used for a while.
        # Currently calculated so that an object that hasn't been accessed for 5 minutes has the same
        # removal priority as an object twice its size that's just been accessed.
        self.eviction_decay_constant = float(CONFIG['SG_EVICTION_DECAY'])

        # Objects smaller than this size are assumed to have this size (to simulate the latency of
        # downloading them).
        self.eviction_floor = float(CONFIG['SG_EVICTION_FLOOR']) * 1024 * 1024

        # TODO TF work
        # * we might keep this (but create temporary SNAPs based on quals in a single table --
        #   we can have the last chunk of a table that's queried often stored as a SNAP and other ones
        #   as SNAP + DIFFs)
        # * or remove this

        # If we had to return a given DIFF more than or equal to `cache_misses_for_snap` times in the last
        # `cache_misses_lookback` seconds, the resolver creates a SNAP from the diff chain and returns
        # that instead. This is to replace layered querying with direct SNAP queries for popular tables.
        # The temporary SNAP is evicted just like other objects.
        self.cache_misses_for_snap = int(CONFIG['SG_SNAP_CACHE_MISSES'])
        self.cache_misses_lookback = int(CONFIG['SG_SNAP_CACHE_LOOKBACK'])

    # TODO TF work this is only used in eviction and might need to be changed a lot.
    def get_full_object_tree(self):
        """Returns a dictionary (object_id -> [list of parents], SNAP/DIFF, size) with the full object tree
        in the engine"""
        query_result = self.object_engine.run_sql(select("objects", "object_id,parent_id,format,size"))

        result = {}
        for object_id, parent_id, object_format, size in query_result:
            if object_id not in result:
                result[object_id] = ([parent_id], object_format, size) if parent_id else ([], object_format, size)
            else:
                if parent_id:
                    result[object_id][0].append(parent_id)

        return result

    @staticmethod
    def _coerce_decimal(val):
        # Some values can't be stored in json so we turn them into strings
        if isinstance(val, Decimal):
            return str(val)
        if isinstance(val, date):
            return val.isoformat()
        return val

    def _generate_object_index(self, object_id):
        """
        Queries the max/min values of a given fragment for each column, used to speed up querying.

        :param object_id: ID of an object
        :return: Dict of {column_name: (min_val, max_val)}
        """
        # Maybe we should pass the column names in instead?
        column_names = [c[1] for c in self.object_engine.get_full_table_schema(SPLITGRAPH_META_SCHEMA, object_id)
                        if c[1] != SG_UD_FLAG and c[2] in _PG_INDEXABLE_TYPES]

        query = SQL("SELECT ") + SQL(",").join(SQL("MIN({0}), MAX({0})").format(Identifier(c)) for c in column_names)
        query += SQL(" FROM {}.{}").format(Identifier(SPLITGRAPH_META_SCHEMA), Identifier(object_id))
        result = self.object_engine.run_sql(query, return_shape=ResultShape.ONE_MANY)

        return {col: (self._coerce_decimal(cmin), self._coerce_decimal(cmax)) for col, cmin, cmax in
                zip(column_names, result[0::2], result[1::2])}

    def register_object(self, object_id, object_format, namespace, parent_object=None):
        """
        Registers a Splitgraph object in the object tree and indexes it

        :param object_id: Object ID
        :param object_format: Format (SNAP or DIFF)
        :param namespace: Namespace that owns the object. In registry mode, only namespace owners can alter or delete
            objects.
        :param parent_object: Parent that the object depends on, if it's a DIFF object.
        """
        if not parent_object and object_format != 'SNAP':
            raise ValueError("Non-SNAP objects can't have no parent!")
        if parent_object and object_format == 'SNAP':
            raise ValueError("SNAP objects can't have a parent!")

        object_size = self.object_engine.get_table_size(SPLITGRAPH_META_SCHEMA, object_id)
        object_index = self._generate_object_index(object_id)
        self.object_engine.run_sql(
            insert("objects", ("object_id", "format", "parent_id", "namespace", "size", "index")),
            (object_id, object_format, parent_object, namespace, object_size, object_index))

    def register_objects(self, object_meta, namespace=None):
        """
        Registers multiple Splitgraph objects in the tree. See `register_object` for more information.

        :param object_meta: List of (object_id, format, parent_id, namespace, size, index).
        :param namespace: If specified, overrides the original object namespace, required
            in the case where the remote repository has a different namespace than the local one.
        """
        if namespace:
            object_meta = [(o, f, p, namespace, s, i) for o, f, p, _, s, i in object_meta]
        self.object_engine.run_sql_batch(
            insert("objects", ("object_id", "format", "parent_id", "namespace", "size", "index")),
                                         object_meta)

    def register_tables(self, repository, table_meta):
        """
        Links tables in an image to physical objects that they are stored as.
        Objects must already be registered in the object tree.

        :param repository: Repository that the tables belong to.
        :param table_meta: A list of (image_hash, table_name, table_schema, object_id).
        """
        table_meta = [(repository.namespace, repository.repository,
                       o[0], o[1], Json(o[2]), o[3]) for o in table_meta]
        self.object_engine.run_sql_batch(
            insert("tables", ("namespace", "repository", "image_hash", "table_name", "table_schema", "object_id")),
            table_meta)

    def register_object_locations(self, object_locations):
        """
        Registers external locations (e.g. HTTP or S3) for Splitgraph objects.
        Objects must already be registered in the object tree.

        :param object_locations: List of (object_id, location, protocol).
        """
        # Don't insert redundant objects here either.
        existing_locations = self.object_engine.run_sql(select("object_locations", "object_id"),
                                                        return_shape=ResultShape.MANY_ONE)
        object_locations = [o for o in object_locations if o[0] not in existing_locations]
        self.object_engine.run_sql_batch(insert("object_locations", ("object_id", "location", "protocol")),
                                         object_locations)

    def get_existing_objects(self):
        """
        Gets all objects currently in the Splitgraph tree.

        :return: Set of object IDs.
        """
        return set(self.object_engine.run_sql(select("objects", "object_id"), return_shape=ResultShape.MANY_ONE))

    def get_downloaded_objects(self, limit_to=None):
        """
        Gets a list of objects currently in the Splitgraph cache (i.e. not only existing externally.)

        :param limit_to: If specified, only the objects in this list will be returned.
        :return: Set of object IDs.
        """
        query = "SELECT pg_tables.tablename FROM pg_tables WHERE pg_tables.schemaname = %s"
        query_args = [SPLITGRAPH_META_SCHEMA]
        if limit_to:
            query += " AND pg_tables.tablename IN (" + ",".join(itertools.repeat('%s', len(limit_to))) + ")"
            query_args += list(limit_to)
        objects = set(self.object_engine.run_sql(
            SQL(query).format(Identifier(SPLITGRAPH_META_SCHEMA)), query_args, return_shape=ResultShape.MANY_ONE))
        return objects.difference(META_TABLES)

    def get_external_object_locations(self, objects):
        """
        Gets external locations for objects.

        :param objects: List of objects stored externally.
        :return: List of (object_id, location, protocol).
        """
        return self.object_engine.run_sql(select("object_locations", "object_id, location, protocol",
                                                 "object_id IN (" + ','.join('%s' for _ in objects) + ")"),
                                          objects)

    def get_object_meta(self, objects):
        """
        Get metadata for multiple Splitgraph objects from the tree

        :param objects: List of objects to get metadata for.
        :return: List of (object_id, format, parent_id, namespace, size, index).
        """
        return self.object_engine.run_sql(select("objects", "object_id, format, parent_id, namespace, size, index",
                                                 "object_id IN (" + ','.join('%s' for _ in objects) + ")"), objects)

    def register_table(self, repository, table, image, schema, object_id):
        """
        Registers the object that represents a Splitgraph table inside of an image.

        :param repository: Repository
        :param table: Table name
        :param image: Image hash
        :param schema: Table schema
        :param object_id: Object ID to register the table to.
        """
        self.object_engine.run_sql(
            insert("tables", ("namespace", "repository", "image_hash", "table_name", "table_schema", "object_id")),
            (repository.namespace, repository.repository, image, table, Json(schema), object_id))

    # TODO TF work
    # * Add looking for the chunk boundaries and splitting the diff up
    # * Add testing whether too many rows have been overwritten (unlinking the DIFF chain and creating a new SNAP)
    # * Add conflating lots of small fragments together?
    def record_table_as_diff(self, old_table, image_hash):
        """
        Flushes the pending changes from the audit table for a given table and records them,
        registering the new objects.

        :param old_table: Table object pointing to the current HEAD table
        :param image_hash: Image hash to store the table under
        """
        object_id = get_random_object_id()
        engine = self.object_engine
        # Accumulate the diff in-memory. This might become a bottleneck in the future.

        # Only care about PKs that have been upserted / deleted
        # so we can get them and then run an insert select from
        # but there's still the conflation problem (delete + update on same PK is still an upsert;
        # make sure there aren't PK-changing updates (otherwise those are supposed to be a separate
        # delete + update))

        changeset = {}
        for row_data, action, changed_fields in engine.get_pending_changes(old_table.repository.to_schema(),
                                                                           old_table.table_name):
            _conflate_changes(changeset, [(row_data, action, changed_fields)])
        engine.discard_pending_changes(old_table.repository.to_schema(), old_table.table_name)

        # This can be simplified: we don't need to load the whole changeset and conflate it just
        # to find out which rows need to be copied over into the new object.
        if changeset:
            upserted = [pk for pk, change in changeset.items() if change[0] in (0, 2)]
            deleted = [pk for pk, change in changeset.items() if change[0] == 1]

            # Also we need to know the old values of the deleted rows since we're using them
            # for the index

            engine.store_fragment(upserted, deleted, SPLITGRAPH_META_SCHEMA, object_id,
                                  old_table.repository.to_schema(),
                                  old_table.table_name)
            self.register_object(
                object_id, object_format='DIFF', namespace=old_table.repository.namespace,
                parent_object=old_table.objects[0][0])

            self.register_table(old_table.repository, old_table.table_name, image_hash,
                                old_table.table_schema, object_id)
        else:
            # Changes in the audit log cancelled each other out. Delete the diff table and just point
            # the commit to the old table.
            self.register_table(old_table.repository, old_table.table_name, image_hash,
                                old_table.table_schema, old_table.objects[0][0])

    # TODO TF work: chunk the table up
    def record_table_as_snap(self, repository, table_name, image_hash):
        """
        Copies the full table verbatim into a new Splitgraph SNAP object, registering the new object.

        :param repository: Repository
        :param table_name: Table name
        :param image_hash: Hash of the new image
        """
        # Make sure the SNAP for this table doesn't already exist
        table = repository.images.by_hash(image_hash).get_table(table_name)
        if table and table.get_object('SNAP'):
            return

        object_id = get_random_object_id()
        self.object_engine.copy_table(repository.to_schema(), table_name, SPLITGRAPH_META_SCHEMA, object_id,
                                      with_pk_constraints=True)
        self.register_object(object_id, object_format='SNAP', namespace=repository.namespace,
                             parent_object=None)
        table_schema = self.object_engine.get_full_table_schema(repository.to_schema(), table_name)
        self.register_table(repository, table_name, image_hash, table_schema, object_id)

    # TODO TF work this probably stays
    def extract_recursive_object_meta(self, remote, table_meta):
        """Recursively crawl the a remote object manager in order to fetch all objects
        required to materialize tables specified in `table_meta` that don't yet exist on the local engine."""
        existing_objects = self.get_existing_objects()
        distinct_objects = set(o[3] for o in table_meta if o[3] not in existing_objects)
        known_objects = set()
        object_meta = []

        while True:
            new_parents = [o for o in distinct_objects if o not in known_objects]
            if not new_parents:
                break
            else:
                parents_meta = remote.get_object_meta(new_parents)
                distinct_objects.update(
                    set(o[3] for o in parents_meta if o[3] is not None and o[3] not in existing_objects))
                object_meta.extend(parents_meta)
                known_objects.update(new_parents)
        return distinct_objects, object_meta

    def get_all_required_objects(self, object_id):
        parents = self.object_engine.run_sql(SQL(
            """WITH RECURSIVE parents AS (
                SELECT object_id, parent_id FROM {0}.objects WHERE object_id = %s
                UNION ALL
                    SELECT o.object_id, o.parent_id
                        FROM parents p JOIN {0}.objects o ON p.parent_id = o.object_id)
            SELECT object_id FROM parents""").format(Identifier(SPLITGRAPH_META_SCHEMA)), (object_id,),
                                             return_shape=ResultShape.MANY_ONE)
        return list(parents)

    def _get_image_object_path(self, table):
        """
        Calculates a list of objects SNAP, DIFF, ... , DIFF that are used to reconstruct a table.

        :param table: Table object
        :return: A tuple of (SNAP object, list of DIFF objects in reverse order (latest object first))
        """

        # * A SNAP for a given table is always the same size or smaller than the equivalent SNAP + DIFF chain
        #   used to materialize that table -- but the DIFF chain allows to materialize more versions of a table.
        # * Currently an object can't have more than 1 parent.
        #   Therefore, we don't actually yet have multiple resolution paths: all we can do is follow the DIFF chain
        #   (there's only one parent DIFF we can jump to) until we reach an object where we have a SNAP --
        #   cached locally, derived locally or stored remotely). Then we should always use that SNAP in
        #   materialization/LQ (might be some argument whether we should still choose a remote SNAP over
        #   a completely local DIFF chain).

        object_id = table.get_object('SNAP')
        if object_id is not None:
            return object_id, []

        object_id = table.get_object('DIFF')
        snap_id = self._get_snap_cache_for(object_id)
        if snap_id is not None:
            return snap_id, []

        # Use a recursive subquery to fetch a path through the object tree.
        path = self.get_all_required_objects(object_id)
        # Traverse the path to see if we can short circuit a DIFF chain with a SNAP in the SNAP cache.
        final_path = []
        for object_id in path:
            cached_snap = self._get_snap_cache_for(object_id)
            if cached_snap is not None:
                return cached_snap, final_path
            final_path.append(object_id)
        return final_path[-1], final_path[:-1]


    def _store_snap_cache_miss(self, diff_id, timestamp):
        self.object_engine.run_sql(insert("snap_cache_misses", ("diff_id", "used_time")), (diff_id, timestamp))

    def _recent_snap_cache_misses(self, diff_id, start):
        return self.object_engine.run_sql(select("snap_cache_misses", "COUNT(diff_id)",
                                                 "diff_id = %s AND used_time > %s"), (diff_id, start),
                                          return_shape=ResultShape.ONE_ONE)

    def _get_snap_cache_for(self, diff_id):
        return self.object_engine.run_sql(select("snap_cache", "snap_id", "diff_id = %s"), (diff_id,),
                                          return_shape=ResultShape.ONE_ONE)

    def _get_snap_cache(self):
        return {snap_id: (diff_id, size) for snap_id, diff_id, size
                in self.object_engine.run_sql(select("snap_cache", "snap_id,diff_id,size"))}

    @staticmethod
    def _object_size(object_id, object_tree, snap_cache):
        # An object can be either a real object or an ephemeral SNAP.
        if object_id in object_tree:
            return object_tree[object_id][2]
        return snap_cache[object_id][1]

    def get_cache_occupancy(self):
        """
        :return: Space occupied by objects cached from external locations, in bytes.
        """
        return int(self.object_engine.run_sql(SQL("""
        WITH o AS (SELECT DISTINCT object_id, size FROM {0}.objects)
        SELECT COALESCE(SUM(size), 0) FROM (
            SELECT SUM(o.size) AS size FROM {0}.object_cache_status oc JOIN o ON o.object_id = oc.object_id
            WHERE oc.ready = 't'
            UNION ALL
            SELECT SUM(sc.size) AS size FROM {0}.snap_cache sc)_""").format(Identifier(SPLITGRAPH_META_SCHEMA)),
                                              return_shape=ResultShape.ONE_ONE))

    def _qual_to_clause(self, qual, ctype):
        """Convert our internal qual format into a WHERE clause that runs against an object's index entry.
        Returns a Postgres clause (as a Composable) and a tuple of arguments to be mogrified into it."""
        column_name, operator, value = qual

        # Our index is essentially a bloom filter: it returns True if an object _might_ have rows
        # that affect the result of a query with a given qual and False if it definitely doesn't.
        # Hence, we can combine qualifiers in a similar Boolean way (proof?)

        # If there's no index information for a given column, we have to assume it might match the qual.
        query = SQL("NOT index ? %s OR ")
        args = [column_name]

        # If the column has to be greater than (or equal to) X, it only might exist in objects
        # whose maximum value is greater than (or equal to) X.
        if operator in ('>', '>='):
            query += SQL("(index #>> '{{{},1}}')::" + ctype + "  " + operator + " %s").format((Identifier(column_name)))
            args.append(value)
        # Similar for smaller than, but here we check that the minimum value is smaller than X.
        elif operator in ('<', '<='):
            query += SQL("(index #>> '{{{},0}}')::" + ctype + " " + operator + " %s").format((Identifier(column_name)))
            args.append(value)
        elif operator in ('==', '<>'):
            query += SQL("%s " + ("" if operator == "==" else "NOT ")
                         + "BETWEEN (index #>> '{{{0},0}}')::" + ctype
                         + " AND (index #>> '{{{0},1}}')::" + ctype).format((Identifier(column_name)))
            args.append(value)
        # Currently, we ignore the LIKE (~~) qualifier since we can only make a judgement when the % pattern is at
        # the end of a string.
        else:
            # For all other operators, we don't know if they will match so we assume that they will.
            return SQL('TRUE'), ()
        return query, tuple(args)

    def _quals_to_clause(self, quals, column_types):
        def _internal_quals_to_clause(or_quals):
            clauses, args = zip(*[self._qual_to_clause(q, column_types[q[0]]) for q in or_quals])
            return SQL(" OR ").join(SQL("(") + c + SQL(")") for c in clauses), tuple([a for arg in args for a in arg])

        clauses, args = zip(*[_internal_quals_to_clause(q) for q in quals])
        return SQL(" AND ").join(SQL("(") + c + SQL(")") for c in clauses), tuple([a for arg in args for a in arg])

    def _filter_objects(self, object_ids, quals, column_types):
        # We need access to column types here since the objects might not have been fetched yet and so
        # we can't look at their column types.
        clause, args = self._quals_to_clause(quals, column_types)

        query = SQL("SELECT object_id FROM {}.{} WHERE object_id IN (") \
            .format(Identifier(SPLITGRAPH_META_SCHEMA), Identifier("objects"))
        query += SQL(",".join(itertools.repeat("%s", len(object_ids))) + ")")
        query += SQL(" AND ") + clause

        return self.object_engine.run_sql(query, list(object_ids) + list(args), return_shape=ResultShape.MANY_ONE)

    @contextmanager
    def ensure_objects(self, table, quals=None):
        """
        Resolves the objects needed to materialize a given table and makes sure they are in the local
        splitgraph_meta schema.

        Whilst inside this manager, the objects are guaranteed to exist. On exit from it, the objects are marked as
        unneeded and can be garbage collected.

        :param table: Table to materialize
        :param quals: Optional list of qualifiers in conjunctive normal form that will be matched against the index. If
            specified, objects that definitely don't match these qualifiers will be dropped.

            A list containing `[[qual_1, qual_2], [qual_3, qual_4]]` will be interpreted as
            (qual_1 OR qual_2) AND (qual_3 OR qual_4).

            Each qual is a tuple of `(column_name, operator, value)` where
            `operator` can be one of `>`, `>=`, `<`, `<=`, `=`, `~~` (LIKE), `<>`.
        :return: (SNAP object ID, list of DIFFs to apply to the SNAP (can be empty))
        """

        # Main cache management issue here is concurrency: since we have multiple processes per connection on the
        # server side, if we're accessing this from the FDW, multiple ObjectManagers can fiddle around in the cache
        # status table, triggering weird concurrency cases like:
        #   * We need to download some objects -- another manager wants the same objects. How do we make sure we don't
        #     download them twice? Do we wait on a row-level lock? What happens if another manager crashes?
        #   * We have decided which objects we need -- how do we make sure we don't get evicted by another manager
        #     between us making that decision and increasing the refcount?
        #   * What happens if we crash when we're downloading these objects?

        logging.info("Resolving objects for table %s:%s:%s", table.repository, table.image.image_hash, table.table_name)

        self.object_engine.run_sql("SET LOCAL synchronous_commit TO OFF")
        tracer = Tracer()

        # TODO TF work: returns multiple chunk groups (snaps/diffs) depending on how precise
        # the quals are
        # Resolve the table into a list of objects we want to fetch (one SNAP and optionally a list of DIFFs).
        # In the future, we can also take other things into account, such as how expensive it is to load a given object
        # (its size), location...
        snap, diffs = self._get_image_object_path(table)
        required_objects = diffs + [snap]
        tracer.log('resolve_objects')

        # Filter to see if we can discard any objects with the quals
        if quals:
            column_types = {c[1]: c[2] for c in table.table_schema}
            filtered_objects = self._filter_objects(required_objects, quals, column_types)
            if filtered_objects != required_objects:
                required_objects = filtered_objects
                objects_were_filtered = True
            else:
                objects_were_filtered = False
        else:
            objects_were_filtered = False
        tracer.log('filter_objects')

        # Increase the refcount on all of the objects we're giving back to the caller so that others don't GC them.
        logging.info("Claiming %d object(s)", len(required_objects))
        self._claim_objects(required_objects)
        tracer.log('claim_objects')
        # This also means that anybody else who tries to claim this set of objects will lock up until we're done with
        # them (though note that if anything commits in the download step, these locks will get released and we'll
        # be liable to be stepped on)

        try:
            to_fetch = self._prepare_fetch_list(required_objects)
        except SplitGraphException:
            self.object_engine.rollback()
            raise
        tracer.log('prepare_fetch_list')

        # Perform the actual download. If the table has no upstream but still has external locations, we download
        # just the external objects.
        if to_fetch:
            upstream = table.repository.get_upstream()
            object_locations = self.get_external_object_locations(required_objects)
            self.download_objects(upstream.objects if upstream else None, objects_to_fetch=to_fetch,
                                  object_locations=object_locations)
            difference = set(to_fetch).difference(set(self.get_downloaded_objects(limit_to=to_fetch)))
            if difference:
                logging.exception(
                    "Not all objects required to materialize %s:%s:%s have been fetched. Missing objects: %r",
                    table.repository.to_schema(), table.image.image_hash, table.table_name, difference)
                self.object_engine.rollback()
                raise SplitGraphException()
            self._set_ready_flags(to_fetch, is_ready=True)
        tracer.log('fetch_objects')

        # End the first phase (resolution and download) -- we commit here because we need a checkpoint to
        # roll back to in _create_and_register_temporary_snap.
        self.object_engine.commit()
        tracer.log('stage_1_commit')
        self.object_engine.run_sql("SET LOCAL synchronous_commit TO off")

        if diffs and not objects_were_filtered:
            # We want to return a SNAP + a DIFF chain. See if a DIFF chain ending (starting?) with a given DIFF
            # has been requested too many times.
            now = dt.utcnow()
            self._store_snap_cache_miss(diffs[0], now)
            if self._recent_snap_cache_misses(
                    diffs[0], now - timedelta(seconds=self.cache_misses_lookback)) >= self.cache_misses_for_snap:
                new_snap_id = self._create_and_register_temporary_snap(snap, diffs)
                # Instead of the old SNAP + DIFF chain, use the new SNAP (the rest will proceed in the same way:
                # we'll increase the refcount and set the last used time on the SNAP instead of the full chain,
                # yield it and then release the refcount.
                diffs = []
                snap = new_snap_id

                self._release_objects(required_objects)
                required_objects = [new_snap_id]
                self._claim_objects(required_objects)
                self._set_ready_flags(required_objects, True)
        tracer.log('snap_cache')

        # Extra stuff: stats to gather:
        #   * Object cache misses
        #   * SNAP cache misses
        #   * Table usage
        #   * time spent downloading, materializing, using LQs

        logging.info("Yielding to the caller")
        try:
            # Release the lock and yield to the caller.
            self.object_engine.commit()
            yield [snap] + list(reversed(diffs))
        finally:
            # Decrease the refcounts on the objects. Optionally, evict them.
            # If the caller crashes, we should still hit this and decrease the refcounts, but if the whole program
            # is terminated abnormally, we'll leak memory.
            tracer.log('caller')
            self.object_engine.run_sql("SET LOCAL synchronous_commit TO off")
            self._release_objects(required_objects)
            tracer.log('release_objects')
            logging.info("Releasing %d object(s)", len(required_objects))
            logging.info("Timing stats for %s/%s/%s/%s: \n%s", table.repository.namespace, table.repository.repository,
                         table.image.image_hash, table.table_name, tracer)
            self.object_engine.commit()

    def _create_and_register_temporary_snap(self, snap, diffs):
        # Maybe here we should give longer DIFF chains priority somehow?

        # TODO TF work: this is partially the rechunking code (melding diffs back into snaps) but on the
        # read side, not the write side. We might keep this (SNAP table regions that are queried often).

        # There's a race condition: we check if a temporary SNAP exists for a chain in the beginning,
        # when we resolve objects -- but then can spend some time downloading objects. Whilst we are doing that,
        # it's possible that something else has already created a SNAP and we'll get a PK error when trying to
        # register it. So, we try to register it first -- if we won the race, then we'll insert the SNAP into the
        # cache, the query will yield and we'll be able to actually create it. If not, we'll block until the other
        # worker has created the SNAP and then fail with a PK error, at which point we just roll back and use the SNAP.
        new_snap_id = get_random_object_id()
        try:
            self.object_engine.run_sql(insert("snap_cache", ("snap_id", "diff_id", "size")), (new_snap_id, diffs[0], 0))
            logging.info("Generating a temporary SNAP %s for a DIFF chain starting with %s", new_snap_id, diffs[0])
            # We'll need extra space to create the temporary SNAP: we can't always do it on the fly
            # (e.g. by just taking the old base SNAP and applying new DIFFs to it) since that operation
            # is destructive and some other users might be using this object.
            # But we also can't anticipate how much space we'll need: we could have a 1000-row SNAP + a
            # 1000-row DIFF that deletes all rows in that SNAP, so the result is an empty table. However, we
            # are bounded from the top by the sum of the sizes of all intermediate objects.
            # Nevertheless, we'll create the SNAP anyway and just keep it there, letting whoever else gets
            # to access the cache next run eviction if they need it.
            self.object_engine.copy_table(SPLITGRAPH_META_SCHEMA, snap, SPLITGRAPH_META_SCHEMA, new_snap_id,
                                          with_pk_constraints=True)
            logging.info("Applying %d DIFF object(s)..." % len(diffs))
            # self.object_engine.batch_apply_diff_objects([(SPLITGRAPH_META_SCHEMA, d) for d in reversed(diffs)],
            #                                             SPLITGRAPH_META_SCHEMA, new_snap_id)

            for diff in diffs:
                self.object_engine.apply_fragment(SPLITGRAPH_META_SCHEMA, diff, SPLITGRAPH_META_SCHEMA, new_snap_id)
            # We've created the SNAP -- now we need to record its size (can't do it straightaway because
            # we use the INSERT statement to claim a lock).
            size = self.object_engine.get_table_size(SPLITGRAPH_META_SCHEMA, new_snap_id)
            self.object_engine.run_sql(SQL("UPDATE {}.snap_cache SET size = %s WHERE snap_id = %s")
                                       .format(Identifier(SPLITGRAPH_META_SCHEMA)), (size, new_snap_id))
        except IntegrityError:
            self.object_engine.rollback()
            new_snap_id = self._get_snap_cache_for(diffs[0])
            logging.info("Using existing SNAP %s for a DIFF chain starting with %s", new_snap_id, diffs[0])

        return new_snap_id

    def _prepare_fetch_list(self, required_objects):
        """
        Calculates the missing objects and ensures there's enough space in the cache
        to download them.

        :param required_objects: Iterable of object IDs that are required to be on the engine.
        :return: Set of objects to fetch
        """
        objects_in_cache = self.get_downloaded_objects(limit_to=required_objects)
        to_fetch = set(required_objects).difference(objects_in_cache)
        if to_fetch:
            required_space = sum(o[4] for o in self.get_object_meta(list(to_fetch)))
            current_occupied = self.get_cache_occupancy()
            logging.info("Need to download %d object(s) (%s), cache occupancy: %s/%s",
                         len(to_fetch), pretty_size(required_space),
                         pretty_size(current_occupied), pretty_size(self.cache_size))
            # If the total cache size isn't large enough, there's nothing we can do without cooperating with the
            # caller and seeing if they can use the objects one-by-one.
            if required_space > self.cache_size:
                raise SplitGraphException("Not enough space in the cache to download the required objects!")
            if required_space > self.cache_size - current_occupied:
                to_free = required_space + current_occupied - self.cache_size
                logging.info("Need to free %s", pretty_size(to_free))
                self.run_eviction(self.get_full_object_tree(), required_objects, to_free)
        return to_fetch

    def _claim_objects(self, objects):
        """Adds objects to the cache_stats table, if they're not there already, marking them with ready=False
        (which must be set to True by the end of the operation).
        If they already exist, increases their refcounts and bumps their last used timestamp to now."""
        now = dt.utcnow()
        self.object_engine.run_sql_batch(insert("object_cache_status", ("object_id", "ready", "refcount", "last_used"))
                                         + SQL("ON CONFLICT (object_id) DO UPDATE "
                                               "SET refcount = {0}.{1}.refcount + 1, "
                                               "last_used = %s "
                                               "WHERE {0}.{1}.object_id = excluded.object_id")
                                         .format(Identifier(SPLITGRAPH_META_SCHEMA), Identifier("object_cache_status")),
                                         [(object_id, False, 1, now, now) for object_id in objects])

    def _set_ready_flags(self, objects, is_ready=True):
        if objects:
            self.object_engine.run_sql(SQL("UPDATE {0}.object_cache_status SET ready = %s WHERE object_id IN (" +
                                           ",".join(itertools.repeat("%s", len(objects))) + ")")
                                       .format(Identifier(SPLITGRAPH_META_SCHEMA)), [is_ready] + list(objects))

    def _release_objects(self, objects):
        """Decreases objects' refcounts."""
        if objects:
            self.object_engine.run_sql(SQL("UPDATE {}.{} SET refcount = refcount - 1 WHERE object_id IN (" +
                                           ",".join(itertools.repeat("%s", len(objects))) + ")")
                                       .format(Identifier(SPLITGRAPH_META_SCHEMA), Identifier("object_cache_status")),
                                       objects)

    def run_eviction(self, object_tree, keep_objects, required_space=None):
        """
        Delete enough objects with zero reference count (only those, since we guarantee that whilst refcount is >0,
        the object stays alive) to free at least `required_space` in the cache.

        :param object_tree: Object tree dictionary
        :param keep_objects: List of objects (besides those with nonzero refcount) that can't be deleted.
        :param required_space: Space, in bytes, to free. If the routine can't free at least this much space,
            it shall raise an exception. If None, removes all eligible objects.
        """

        now = dt.utcnow()
        snap_cache_sizes = self._get_snap_cache()

        def _eviction_score(object_size, last_used):
            # We want to evict objects in order to minimize
            # P(object is requested again) * (cost of redownloading the object).
            # To approximate the probability, we use an exponential decay function (1 if last_used = now, dropping down
            # to 0 as time since the object's last usage time passes).
            # To approximate the cost, we use the object's size, floored to a constant (so if the object has
            # size <= floor, we'd use the floor value -- this is to simulate the latency of re-fetching the object,
            # as opposed to the bandwidth)
            time_since_used = (now - last_used).total_seconds()
            time_factor = math.exp(-self.eviction_decay_constant * time_since_used)
            size_factor = object_size if object_size > self.eviction_floor else self.eviction_floor
            return time_factor * size_factor

        # TODO should SNAPs have priority over DIFFs (since they give access to more potential table versions),
        # but they kind of do since they are bigger.

        logging.info("Performing eviction...")
        # Maybe here we should also do the old cleanup (see if the objects aren't required
        #   by any of the current repositories at all).

        # Find deletion candidates: objects that we have locally, with refcount 0, that aren't in the whitelist.
        # Note this will also evict temporary materialized SNAPs: if an older version starts getting hit, the SNAP's
        # last_used timestamp will never get updated and it'll get evicted.

        # We need to lock the table to make sure that our calculations of what we're about to delete are consistent.
        # However, we can deadlock: we're already holding a lock on some objects and want to acquire an exclusive
        # lock and so will wait for other workers to release their objects -- however, they might be waiting on us
        # to release our objects.

        # Hence, we commit the object refcount increase (so that others can't GC them), releasing the lock,
        # and try to acquire the stronger lock.
        self.object_engine.commit()
        self.object_engine.lock_table(SPLITGRAPH_META_SCHEMA, "object_cache_status")

        candidates = [o for o in self.object_engine.run_sql(
            select("object_cache_status", "object_id,last_used", "refcount=0"),
            return_shape=ResultShape.MANY_MANY) if o[0] not in keep_objects]

        if required_space is None:
            # Just delete everything with refcount 0.
            to_delete = [o[0] for o in candidates]
            freed_space = sum(self._object_size(o, object_tree, snap_cache_sizes) for o in to_delete)
        else:
            if required_space > sum(self._object_size(o[0], object_tree, snap_cache_sizes) for o in candidates):
                raise SplitGraphException("Not enough space will be reclaimed after eviction!")

            # Sort them by deletion priority (lowest is smallest expected retrieval cost -- more likely to delete)
            to_delete = []
            candidates = sorted(candidates,
                                key=lambda o: _eviction_score(self._object_size(o[0], object_tree, snap_cache_sizes),
                                                              o[1]))
            freed_space = 0
            # Keep adding deletion candidates until we've freed enough space.
            for object_id, _ in candidates:
                to_delete.append(object_id)
                freed_space += self._object_size(object_id, object_tree, snap_cache_sizes)
                if freed_space >= required_space:
                    break

        logging.info("Will delete %d object(s), total size %s", len(to_delete), pretty_size(freed_space))
        self.delete_objects(to_delete)
        if to_delete:
            self.object_engine.run_sql(SQL("DELETE FROM {}.{} WHERE object_id IN (" +
                                           ",".join(itertools.repeat("%s", len(to_delete))) + ")")
                                       .format(Identifier(SPLITGRAPH_META_SCHEMA), Identifier("object_cache_status")),
                                       to_delete)
            self.object_engine.run_sql(SQL("DELETE FROM {}.{} WHERE snap_id IN (" +
                                           ",".join(itertools.repeat("%s", len(to_delete))) + ")")
                                       .format(Identifier(SPLITGRAPH_META_SCHEMA), Identifier("snap_cache")),
                                       to_delete)

        # Release the exclusive lock and relock the objects we want instead once again (???)
        self.object_engine.commit()
        self.object_engine.run_sql("SET LOCAL synchronous_commit TO off;")
        self._release_objects(keep_objects)
        self._claim_objects(keep_objects)

    def download_objects(self, source, objects_to_fetch, object_locations):
        """
        Fetches the required objects from the remote and stores them locally.
        Does nothing for objects that already exist.

        :param source: Remote ObjectManager. If None, will only try to download objects from the external location.
        :param objects_to_fetch: List of object IDs to download.
        :param object_locations: List of custom object locations, encoded as tuples (object_id, object_url, protocol).
        """

        existing_objects = self.get_downloaded_objects(limit_to=objects_to_fetch)
        objects_to_fetch = list(set(o for o in objects_to_fetch if o not in existing_objects))
        if not objects_to_fetch:
            return

        total_size = sum(o[4] for o in self.get_object_meta(objects_to_fetch))
        logging.info("Fetching %d object(s), total size %s", len(objects_to_fetch), pretty_size(total_size))

        # We don't actually seem to pass extra handler parameters when downloading objects since
        # we can have multiple handlers in this batch.
        external_objects = _fetch_external_objects(self.object_engine, object_locations, objects_to_fetch, {})

        remaining_objects_to_fetch = [o for o in objects_to_fetch if o not in external_objects]
        if not remaining_objects_to_fetch or not source:
            return

        self.object_engine.download_objects(remaining_objects_to_fetch, source.object_engine)
        return

    def upload_objects(self, target, objects_to_push, handler='DB', handler_params=None):
        """
        Uploads physical objects to the remote or some other external location.

        :param target: Target ObjectManager
        :param objects_to_push: List of object IDs to upload.
        :param handler: Name of the handler to use to upload objects. Use `DB` to push them to the remote, `FILE`
            to store them in a directory that can be accessed from the client and `HTTP` to upload them to HTTP.
        :param handler_params: For `HTTP`, a dictionary `{"username": username, "password", password}`. For `FILE`,
            a dictionary `{"path": path}` specifying the directory where the objects shall be saved.
        :return: A list of (object_id, url, handler) that specifies all objects were uploaded (skipping objects that
            already exist on the remote).
        """
        if handler_params is None:
            handler_params = {}

        # Get objects that exist on the remote engine
        existing_objects = target.get_existing_objects()

        objects_to_push = list(set(o for o in objects_to_push if o not in existing_objects))
        if not objects_to_push:
            logging.info("Nothing to upload.")
            return []
        total_size = sum(o[4] for o in self.get_object_meta(objects_to_push))
        logging.info("Uploading %d object(s), total size %s", len(objects_to_push), pretty_size(total_size))

        if handler == 'DB':
            self.object_engine.upload_objects(objects_to_push, target.object_engine)
            # We assume that if the object doesn't have an explicit location, it lives on the remote.
            return []

        external_handler = get_external_object_handler(handler, handler_params)
        with switch_engine(self.object_engine):
            uploaded = external_handler.upload_objects(objects_to_push)
        return [(oid, url, handler) for oid, url in zip(objects_to_push, uploaded)]

    def cleanup(self):
        """
        Deletes all objects in the object_tree not required by any current repository, including their dependencies and
        their remote locations. Also deletes all objects not registered in the object_tree.
        """
        # First, get a list of all objects required by a table.
        primary_objects = set(self.object_engine.run_sql(
            SQL("SELECT DISTINCT (object_id) FROM {}.tables").format(Identifier(SPLITGRAPH_META_SCHEMA)),
            return_shape=ResultShape.MANY_ONE))

        # Expand that since each object might have a parent it depends on.
        if primary_objects:
            while True:
                new_parents = set(parent_id for _, _, parent_id, _, _, _ in self.get_object_meta(list(primary_objects))
                                  if parent_id not in primary_objects and parent_id is not None)
                if not new_parents:
                    break
                else:
                    primary_objects.update(new_parents)

        # Go through the tables that aren't mountpoint-dependent and delete entries there.
        for table_name in ['objects', 'object_locations', 'object_cache_status']:
            query = SQL("DELETE FROM {}.{}").format(Identifier(SPLITGRAPH_META_SCHEMA), Identifier(table_name))
            if primary_objects:
                query += SQL(" WHERE object_id NOT IN (" + ','.join('%s' for _ in range(len(primary_objects))) + ")")
            self.object_engine.run_sql(query, list(primary_objects))

        # Delete objects from the snap_cache table (SNAPs that are linked to DIFFs that no longer exist).
        for table_name in ['snap_cache', 'snap_cache_misses']:
            query = SQL("DELETE FROM {}.{}").format(Identifier(SPLITGRAPH_META_SCHEMA), Identifier(table_name))
            if primary_objects:
                query += SQL(" WHERE diff_id NOT IN (" + ','.join('%s' for _ in range(len(primary_objects))) + ")")
            self.object_engine.run_sql(query, list(primary_objects))

        # Go through the physical objects and delete them as well
        # This is slightly dirty, but since the info about the objects was deleted on rm, we just say that
        # anything in splitgraph_meta that's not a system table is fair game.
        tables_in_meta = {c for c in self.object_engine.get_all_tables(SPLITGRAPH_META_SCHEMA) if c not in META_TABLES}

        # Need to test this cleanup to make sure we don't keep spurious objects here

        # Make sure not to delete objects that are still supposed to be in the snap cache.
        to_delete = tables_in_meta.difference(primary_objects).difference(self._get_snap_cache().keys())
        self.delete_objects(to_delete)
        return to_delete

    def delete_objects(self, objects):
        """
        Deletes objects from the Splitgraph cache

        :param objects: A sequence of objects to be deleted
        """
        objects = list(objects)
        for i in range(0, len(objects), 100):
            query = SQL(";").join(SQL("DROP TABLE IF EXISTS {}.{}")
                                  .format(Identifier(SPLITGRAPH_META_SCHEMA), Identifier(o))
                                  for o in objects[i:i + 100])
            self.object_engine.run_sql(query)
            self.object_engine.commit()


def _fetch_external_objects(engine, object_locations, objects_to_fetch, handler_params):
    non_remote_objects = []
    non_remote_by_method = defaultdict(list)
    for object_id, object_url, protocol in object_locations:
        if object_id in objects_to_fetch:
            non_remote_by_method[protocol].append((object_id, object_url))
            non_remote_objects.append(object_id)
    if non_remote_objects:
        logging.info("Fetching external objects...")
        for method, objects in non_remote_by_method.items():
            handler = get_external_object_handler(method, handler_params)
            # In case we're calling this from inside the FDW
            with switch_engine(engine):
                handler.download_objects(objects)
    return non_remote_objects


def _conflate_changes(changeset, new_changes):
    """
    Updates a changeset to incorporate the new changes. Assumes that the new changes are non-pk changing
    (e.g. PK-changing updates have been converted into a del + ins).
    """
    for change_pk, change_kind, change_data in new_changes:
        old_change = changeset.get(change_pk)
        if not old_change:
            changeset[change_pk] = (change_kind, change_data)
        else:
            if change_kind == 0:
                if old_change[0] == 1:  # Insert over delete: change to update
                    if change_data == {}:
                        del changeset[change_pk]
                    else:
                        changeset[change_pk] = (2, change_data)
                else:
                    raise SplitGraphException("Malformed audit log: existing PK %s inserted." % str(change_pk))
            elif change_kind == 1:  # Delete over insert/update: remove the old change
                del changeset[change_pk]
                if old_change[0] == 2:
                    # If it was an update, also remove the old row.
                    changeset[change_pk] = (1, change_data)
                if old_change[0] == 1:
                    # Delete over delete: can't happen.
                    raise SplitGraphException("Malformed audit log: deleted PK %s deleted again" % str(change_pk))
            elif change_kind == 2:  # Update over insert/update: merge the two changes.
                if old_change[0] == 0 or old_change[0] == 2:
                    old_change[1].update(change_data)


def get_random_object_id():
    """Assign each table a random ID that it will be stored as. Note that postgres limits table names to 63 characters,
    so the IDs shall be 248-bit strings, hex-encoded, + a letter prefix since Postgres doesn't seem to support table
    names starting with a digit."""
    # Make sure we're padded to 62 characters (otherwise if the random number generated is less than 2^247 we'll be
    # dropping characters from the hex format)
    return str.format('o{:062x}', getrandbits(248))
