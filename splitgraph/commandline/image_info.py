"""
sgr commands related to getting information out of / about images
"""

from collections import Counter
from pprint import pprint

import click

import splitgraph.core.repository
from splitgraph import get_engine
from splitgraph.commandline._common import image_spec_parser, pluralise
from splitgraph.core._drawing import render_tree
from splitgraph.core.engine import get_current_repositories


@click.command(name='log')
@click.argument('repository', type=splitgraph.core.repository.to_repository)
@click.option('-t', '--tree', is_flag=True)
def log_c(repository, tree):
    """
    Show the history of a Splitgraph repository.

    By default, this shows the history of the current branch, starting from the HEAD pointer and following its
    parent chain.

    If ``-t`` or ``--tree`` is passed, this instead renders the full image tree. The repository doesn't need to have
    been checked out in this case.
    """
    if tree:
        render_tree(repository)
    else:
        head = repository.get_image(repository.get_head())
        log = head.get_log()
        for entry in log:
            image_info = repository.get_image(entry)
            print("%s %s %s %s" % ("H->" if entry == head else "   ", entry, image_info.created,
                                   image_info.comment or ""))


@click.command(name='diff')
@click.option('-v', '--verbose', default=False, is_flag=True,
              help='Include the actual differences rather than just the total number of updated rows.')
@click.option('-t', '--table-name', help='Show the differences for a single table.')
@click.argument('repository', type=splitgraph.core.repository.to_repository)
@click.argument('tag_or_hash_1', required=False)
@click.argument('tag_or_hash_2', required=False)
def diff_c(verbose, table_name, repository, tag_or_hash_1, tag_or_hash_2):
    """
    Show differences between two Splitgraph images.

    The two images must be in the same repository. The actual targets of this command depend
    on the number of arguments passed:

    ``sgr diff REPOSITORY``

        Return the differences between the current HEAD image and the checked out schema.

    ``sgr diff REPOSITORY TAG_OR_HASH``

        Return the differences between the image and its parent.

    ``sgr diff REPOSITORY TAG_OR_HASH_1 TAG_OR_HASH_2``

        Return the differences from the first (earlier) image to the second image.
    """
    tag_or_hash_1, tag_or_hash_2 = _get_actual_hashes(repository, tag_or_hash_1, tag_or_hash_2)

    diffs = {table_name: repository.diff(table_name, tag_or_hash_1, tag_or_hash_2, aggregate=not verbose)
             for table_name in
             ([table_name] if table_name else sorted(
                 repository.engine.get_all_tables(repository.to_schema())))}

    if tag_or_hash_2 is None:
        print("Between %s and the current working copy: " % tag_or_hash_1[:12])
    else:
        print("Between %s and %s: " % (tag_or_hash_1[:12], tag_or_hash_2[:12]))

    for table, diff_result in diffs.items():
        _emit_table_diff(table, diff_result, verbose)


def _emit_table_diff(table_name, diff_result, verbose):
    to_print = "%s: " % table_name
    if isinstance(diff_result, (list, tuple)):
        if verbose:
            change_count = dict(Counter(d[1] for d in diff_result).most_common())
            added = change_count.get(0, 0)
            removed = change_count.get(1, 0)
            updated = change_count.get(2, 0)
        else:
            added, removed, updated = diff_result

        count = []
        if added:
            count.append("added " + pluralise('row', added))
        if removed:
            count.append("removed " + pluralise('row', removed))
        if updated:
            count.append("updated " + pluralise('row', removed))
        if added + removed + updated == 0:
            count = ['no changes']
        print(to_print + ', '.join(count) + '.')

        if verbose:
            for pk, kind, change in diff_result:
                print("%r: " % (pk,) + ['+', '-', 'U'][kind] + " %r" % change)
    else:
        # Whole table was either added or removed
        print(to_print + ("table added" if diff_result else "table removed"))


def _get_actual_hashes(repository, image_1, image_2):
    if image_1 is None and image_2 is None:
        # Comparing current working copy against the last commit
        image_1 = repository.get_head()
    elif image_2 is None:
        image_1 = repository.resolve_image(image_1)
        # One parameter: diff from that and its parent.
        image_2 = repository.get_image(image_1).parent_id
        if image_2 is None:
            print("%s has no parent to compare to!" % image_1)
        image_1, image_2 = image_2, image_1  # snap_1 has to come first
    else:
        image_1 = repository.resolve_image(image_1)
        image_2 = repository.resolve_image(image_2)
    return image_1, image_2


@click.command(name='show')
@click.argument('image_spec', type=image_spec_parser(default='HEAD'))
@click.option('-v', '--verbose', default=False, is_flag=True,
              help='Also show all tables in this image and the objects they map to.')
def show_c(image_spec, verbose):
    """
    Show information about a Splitgraph image. This includes its parent, comment and creation time.

    Image spec must be of the format ``[NAMESPACE/]REPOSITORY[:HASH_OR_TAG]``. If no tag is specified, ``HEAD`` is used.
    """
    repository, image = image_spec
    image = repository.resolve_image(image)

    print("Commit %s:%s" % (repository.to_schema(), image))
    image_info = repository.get_image(image)
    print(image_info.comment or "")
    print("Created at %s" % image_info.created.isoformat())
    if image_info.parent_id:
        print("Parent: %s" % image_info.parent_id)
    else:
        print("No parent (root commit)")
    if verbose:
        print()
        print("Tables:")
        for t in image_info.get_tables():
            table_objects = image_info.get_table(t).objects
            if len(table_objects) == 1:
                print("  %s: %s (%s)" % (t, table_objects[0][0], table_objects[0][1]))
            else:
                print("  %s:" % t)
                for obj in table_objects:
                    print("    %s (%s)" % obj)


@click.command(name='sql')
@click.argument('sql')
@click.option('-s', '--schema', help='Run SQL against this schema.')
@click.option('-a', '--show-all', is_flag=True, help='Returns all results of the query.')
def sql_c(sql, schema, show_all):
    """
    Run an SQL statement against the Splitgraph engine.

    There are no restrictions on the contents of the statement: this is the same as running it
    from any other PostgreSQL client.

    If ``--schema`` is specified, the statement is run with the ``search_path`` set to that schema. This means
    that these statements are equivalent::

        sgr sql "SELECT * FROM \"noaa/climate\".table"
        sgr sql -s noaa/climate "SELECT * FROM table"
    """
    if schema:
        get_engine().run_sql("SET search_path TO %s", (schema,))
    results = get_engine().run_sql(sql)
    if results is None:
        return

    if len(results) > 10 and not show_all:
        pprint(results[:10])
        print("...")
    else:
        pprint(results)


@click.command(name='status')
@click.argument('repository', required=False, type=splitgraph.core.repository.to_repository)
def status_c(repository):
    """
    Show the status of the Splitgraph engine. If a repository is passed, show information about
    the repository. If not, show information about all repositories local to the engine.
    """
    if repository is None:
        repositories = get_current_repositories(get_engine())
        print("Local repositories: ")
        for mp_name, mp_hash in repositories:
            # Maybe should also show the remote DB address/server
            print("%s: \t %s" % (mp_name, mp_hash))
        print("\nUse sgr status repository to get information about a given repository.")
    else:
        head = repository.get_head()
        if not head:
            print("%s: nothing checked out." % str(repository))
            return
        parent, children = repository.get_image(head).get_parent_children()
        print("%s: on image %s." % (str(repository), head))
        if parent is not None:
            print("Parent: %s" % parent)
        if len(children) > 1:
            print("Children: " + "\n".join(children))
        elif len(children) == 1:
            print("Child: %s" % children[0])
