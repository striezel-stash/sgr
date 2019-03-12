import pytest

from splitgraph.core import clone


def prepare_lq_repo(repo, commit_after_every, include_pk, snap_only=False):
    OPS = ["INSERT INTO fruits VALUES (3, 'mayonnaise')",
           "DELETE FROM fruits WHERE name = 'apple'",
           "DELETE FROM vegetables WHERE vegetable_id = 1",
           "UPDATE fruits SET name = 'guitar' WHERE fruit_id = 2"]

    repo.run_sql("ALTER TABLE fruits ADD COLUMN number NUMERIC DEFAULT 1")
    if include_pk:
        repo.run_sql("ALTER TABLE fruits ADD PRIMARY KEY (fruit_id)")
        repo.run_sql("ALTER TABLE vegetables ADD PRIMARY KEY (vegetable_id)")
        repo.commit()

    for o in OPS:
        repo.run_sql(o)
        print(o)

        if commit_after_every:
            repo.commit(snap_only=snap_only)
    if not commit_after_every:
        repo.commit(snap_only=snap_only)


@pytest.mark.parametrize("test_case", [
    ("SELECT * FROM fruits WHERE fruit_id = 3", [(3, 'mayonnaise', 1)]),
    ("SELECT * FROM fruits WHERE fruit_id = 2", [(2, 'guitar', 1)]),
    ("SELECT * FROM vegetables WHERE vegetable_id = 1", []),
    ("SELECT * FROM fruits WHERE fruit_id = 1", []),

    # EQ on string
    ("SELECT * FROM fruits WHERE name = 'guitar'", [(2, 'guitar', 1)]),

    # IN ( converted to =ANY(array([...]))
    ("SELECT * FROM fruits WHERE name IN ('guitar', 'mayonnaise') ORDER BY fruit_id",
     [(2, 'guitar', 1), (3, 'mayonnaise', 1)]),

    # LIKE (operator ~~)
    ("SELECT * FROM fruits WHERE name LIKE '%uitar'", [(2, 'guitar', 1)]),

    # Join between two FDWs
    ("SELECT * FROM fruits JOIN vegetables ON fruits.fruit_id = vegetables.vegetable_id",
     [(2, 'guitar', 1, 2, 'carrot')]),

    # Expression in terms of another column
    ("SELECT * FROM fruits WHERE fruit_id = number + 1 ", [(2, 'guitar', 1)]),
])
def test_layered_querying(pg_repo_local, test_case):
    # Future: move the LQ tests to be local (instantiate the FDW with some mocks and send the same query requests)
    # since it's much easier to test them like that.
    # Reset the repo every time so that the object manager doesn't have a chance to cache the SNAP.

    # Most of these tests are only interesting for the long DIFF chain case, so we don't include SNAPs here and
    # have PKs on the tables.
    prepare_lq_repo(pg_repo_local, commit_after_every=True, include_pk=True)

    # Discard the actual materialized table and query everything via FDW
    new_head = pg_repo_local.head
    new_head.checkout(layered=True)

    query, expected = test_case
    print("Query: %s, expected: %r" % test_case)
    assert pg_repo_local.run_sql(query) == expected


def test_layered_querying_against_snap(pg_repo_local):
    # Test the case where the query goes directly to the SNAP.
    prepare_lq_repo(pg_repo_local, snap_only=True, commit_after_every=False, include_pk=True)
    new_head = pg_repo_local.head
    new_head.checkout(layered=True)

    assert pg_repo_local.run_sql("SELECT * FROM fruits WHERE name IN ('guitar', 'mayonnaise') ORDER BY fruit_id") \
           == [(2, 'guitar', 1), (3, 'mayonnaise', 1)]


def test_layered_querying_type_conversion(pg_repo_local):
    # For type bigint, Multicorn for some reason converts quals to be strings. Test we can handle that.
    prepare_lq_repo(pg_repo_local, commit_after_every=False, include_pk=True)
    pg_repo_local.run_sql(
        "ALTER TABLE fruits ALTER COLUMN fruit_id TYPE bigint")
    pg_repo_local.commit()
    pg_repo_local.run_sql("INSERT INTO fruits VALUES (4, 'kumquat', 42)")
    new_head = pg_repo_local.commit()
    new_head.checkout(layered=True)

    # Make sure ANY works on integers (not converted to strings)
    assert pg_repo_local.run_sql("SELECT * FROM fruits WHERE fruit_id IN (3, 4)") == [(3, 'mayonnaise', 1),
                                                                                      (4, 'kumquat', 42)]


def _test_lazy_lq_checkout(pg_repo_local):
    assert len(pg_repo_local.objects.get_downloaded_objects()) == 0
    # Do a lazy LQ checkout -- no objects should be downloaded yet
    pg_repo_local.images['latest'].checkout(layered=True)
    assert len(pg_repo_local.objects.get_downloaded_objects()) == 0
    # Actual LQ still downloads the objects, but one by one.
    # Hit fruits -- 2 objects should be downloaded (the second SNAP and the actual DIFF -- old SNAP not downloaded)
    assert pg_repo_local.run_sql("SELECT * FROM fruits WHERE fruit_id = 2") == [(2, 'guitar', 1)]
    assert len(pg_repo_local.objects.get_downloaded_objects()) == 2
    # Hit vegetables -- 2 more objects should be downloaded
    assert pg_repo_local.run_sql("SELECT * FROM vegetables WHERE vegetable_id = 1") == []
    assert len(pg_repo_local.objects.get_downloaded_objects()) == 4


def test_lq_remote(local_engine_empty, pg_repo_remote):
    # Test layered querying works when we initialize it on a cloned repo that doesn't have any
    # cached objects (all are on the remote).

    # 1 DIFF on top of fruits, 1 DIFF on top of vegetables
    prepare_lq_repo(pg_repo_remote, commit_after_every=False, include_pk=True)
    pg_repo_local = clone(pg_repo_remote, download_all=False)
    _test_lazy_lq_checkout(pg_repo_local)


def test_lq_external(local_engine_empty, pg_repo_remote):
    # Test layered querying works when we initialize it on a cloned repo that doesn't have any
    # cached objects (all are on S3 or other external location).

    pg_repo_local = clone(pg_repo_remote)
    pg_repo_local.images['latest'].checkout()
    prepare_lq_repo(pg_repo_local, commit_after_every=False, include_pk=True)

    # Setup: upstream has the same repository as in the previous test but with no cached objects (all are external).
    remote = pg_repo_local.push(handler='S3', handler_options={})
    pg_repo_local.rm()
    pg_repo_remote.objects.delete_objects(remote.objects.get_downloaded_objects())
    pg_repo_remote.engine.commit()
    pg_repo_local.objects.cleanup()

    assert len(pg_repo_local.objects.get_existing_objects()) == 0
    assert len(pg_repo_local.objects.get_downloaded_objects()) == 0
    assert len(remote.objects.get_existing_objects()) == 6
    assert len(remote.objects.get_downloaded_objects()) == 0

    # Proceed as per the previous test
    pg_repo_local = clone(pg_repo_remote, download_all=False)
    _test_lazy_lq_checkout(pg_repo_local)


def _prepare_fully_remote_repo(local_engine_empty, pg_repo_remote):
    # Setup: same as external, with an extra DIFF on top of the fruits table.
    pg_repo_local = clone(pg_repo_remote)
    pg_repo_local.images['latest'].checkout()
    prepare_lq_repo(pg_repo_local, commit_after_every=True, include_pk=True)
    pg_repo_local.run_sql("INSERT INTO fruits VALUES (4, 'kumquat')")
    pg_repo_local.commit()
    remote = pg_repo_local.push(handler='S3', handler_options={})
    pg_repo_local.rm()
    pg_repo_remote.objects.delete_objects(remote.objects.get_downloaded_objects())
    pg_repo_remote.engine.commit()
    pg_repo_local.objects.cleanup()


def test_lq_external_snap_cache(local_engine_empty, pg_repo_remote):
    # Test that after hitting a table via LQ 5 times, the query gets satisfied via a SNAP instead.
    _prepare_fully_remote_repo(local_engine_empty, pg_repo_remote)

    pg_repo_local = clone(pg_repo_remote, download_all=False)
    pg_repo_local.images['latest'].checkout(layered=True)
    assert len(pg_repo_local.objects.get_downloaded_objects()) == 0

    for _ in range(4):
        assert pg_repo_local.run_sql("SELECT * FROM fruits ORDER BY fruit_id") == \
               [(2, 'guitar', 1),
                (3, 'mayonnaise', 1),
                (4, 'kumquat', 1)]
        assert pg_repo_local.objects._get_snap_cache() == {}
        # Downloaded objects: fruits -> 1 original SNAP and 4 DIFFs.
        assert len(pg_repo_local.objects.get_downloaded_objects()) == 5

    # Now the SNAP gets cached instead:
    assert pg_repo_local.run_sql("SELECT * FROM fruits ORDER BY fruit_id") == \
           [(2, 'guitar', 1),
            (3, 'mayonnaise', 1),
            (4, 'kumquat', 1)]
    # Old 4 objects + temporary SNAP.
    assert len(pg_repo_local.objects.get_downloaded_objects()) == 6
    # Check the SNAP cache contents
    assert list(pg_repo_local.objects._get_snap_cache().values()) == [
        (pg_repo_local.images['latest'].get_table('fruits').get_object('DIFF'), 8192)]
