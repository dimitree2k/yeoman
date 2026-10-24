import json
import sqlite3
import stat
import tempfile
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner
from yeoman_gateway.cli.people_commands import people_app
from yeoman_gateway.knowledge._store import KnowledgeStore
from yeoman_gateway.people.schema import open_people_db


@pytest.fixture
def stores(tmp_path):
    knowledge = tmp_path / 'knowledge.db'
    KnowledgeStore(knowledge).close()
    people = tmp_path / 'people.db'
    open_people_db(people).close()
    with closing(sqlite3.connect(knowledge)) as conn, conn:
        for sid, kid, status in [('s1', 'k1', 'assertion'), ('s2', 'k2', 'assertion'),
                                 ('s3', None, 'assertion'), ('s4', 'old', 'revoked')]:
            conn.execute('''INSERT INTO knowledge_statements
                (statement_id,workspace_id,scope_key,author_principal,speaker_person_id,status,
                 visibility_scope,group_rule,valid_from_ms,extractor_version,content_hash,
                 dedupe_key,created_ms,updated_ms)
                VALUES (?, 'w','s','a',?,?,'author_only','none',0,'v','h',?,0,0)''',
                         (sid, kid, status, sid))
        conn.execute("INSERT INTO knowledge_statement_people(statement_id,person_id,role,"
                     "evidence_source_id,evidence_revision,attribution) "
                     "VALUES ('s1','k2','subject','e',1,'explicit')")
        conn.execute("INSERT INTO knowledge_person_attributes VALUES "
                     "('s1','k3','city','\"private\"','v','positive',1,0,0)")
    with closing(sqlite3.connect(people)) as conn, conn:
        conn.execute('INSERT INTO persons(id) VALUES (1)')
        conn.execute('INSERT INTO persons(id,merged_into) VALUES (2,1)')
        for kid in ('k1', 'k2', 'k3'):
            conn.execute("INSERT INTO person_ids(person_id,kind,value) "
                         "VALUES (2,'knowledge_person',?)", (kid,))
    return knowledge, people


def invoke(stores, *args):
    return CliRunner().invoke(people_app, ['knowledge-mapping', '--knowledge-db', str(stores[0]),
                                         '--people-db', str(stores[1]), *args])


def test_all_mapped_json_and_no_writes(stores):
    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in stores]
    result = invoke(stores, '--json')
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report['persons'] == {'mapped': 3, 'unmapped': 0, 'ambiguous': 0}
    assert report['sources']['speaker'] == {'mapped': 2, 'unmapped': 0, 'ambiguous': 0}
    assert report['sources']['roles']['subject']['mapped'] == 1
    assert report['sources']['attributes']['mapped'] == 1
    assert report['statements'] == {'mapped': 2, 'unmapped': 0, 'ambiguous': 0,
                                    'no_speaker': 1}
    assert 'k1' not in result.output and 'private' not in result.output
    assert [(p.read_bytes(), p.stat().st_mtime_ns) for p in stores] == before
    assert not any(p.with_name(p.name + '-wal').exists() for p in stores)


@pytest.mark.parametrize('kind', ['unmapped', 'ambiguous', 'hidden', 'ended'])
def test_problem_details(stores, tmp_path, kind):
    with closing(sqlite3.connect(stores[1])) as conn, conn:
        if kind == 'ambiguous':
            conn.execute('DROP INDEX person_ids_active')
            conn.execute('INSERT INTO persons(id) VALUES (3)')
            conn.execute("INSERT INTO person_ids(person_id,kind,value) "
                         "VALUES (3,'knowledge_person','k1')")
        else:
            update = {'unmapped': 'DELETE FROM person_ids',
                      'hidden': 'UPDATE person_ids SET hidden=1',
                      'ended': 'UPDATE person_ids SET valid_until_ms=5'}[kind]
            conn.execute(update + " WHERE value='k1'")
    private = tempfile.TemporaryDirectory(dir='/tmp', prefix='p3-details-')
    details = Path(private.name) / 'private' / 'details.json'
    result = invoke(stores, '--json', '--details', str(details))
    assert result.exit_code == 1, result.output
    category = 'ambiguous' if kind == 'ambiguous' else 'unmapped'
    assert json.loads(result.output)['persons'][category] == 1
    assert json.loads(result.output)['statements'][category] == 1
    rows = json.loads(details.read_text())
    assert rows == [{'knowledge_id': 'k1', 'reason': 'multiple_terminals' if
                     kind == 'ambiguous' else 'no_active_bridge'}]
    assert stat.S_IMODE(details.stat().st_mode) == 0o600
    assert stat.S_IMODE(details.parent.stat().st_mode) == 0o700
    private.cleanup()


def test_details_refuse_git_tree(stores, tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    (root / '.git').write_text('gitdir: elsewhere')
    result = invoke(stores, '--details', str(root / 'new' / 'details.json'))
    assert result.exit_code != 0
    assert not (root / 'new').exists()


def test_details_cannot_replace_input_database():
    from yeoman_gateway.people.knowledge_mapping import write_mapping_details

    with tempfile.TemporaryDirectory(dir='/tmp', prefix='p3-protected-') as root:
        database = Path(root) / 'people.db'
        database.write_bytes(b'database')
        alias = Path(root) / 'alias'
        alias.hardlink_to(database)
        with pytest.raises(ValueError):
            write_mapping_details(alias, [], protected=(database,))
        assert database.read_bytes() == b'database'
        assert alias.read_bytes() == b'database'


def test_details_refuses_shared_parent_without_chmod():
    from yeoman_gateway.people.knowledge_mapping import write_mapping_details

    with tempfile.TemporaryDirectory(dir='/tmp', prefix='p3-shared-') as root:
        parent = Path(root)
        parent.chmod(0o755)
        with pytest.raises(ValueError):
            write_mapping_details(parent / 'details.json', [])
        assert stat.S_IMODE(parent.stat().st_mode) == 0o755
        assert not (parent / 'details.json').exists()


def test_duplicate_bridges_to_same_terminal_are_not_ambiguous(stores):
    with closing(sqlite3.connect(stores[1])) as conn, conn:
        conn.execute('DROP INDEX person_ids_active')
        conn.execute("INSERT INTO person_ids(person_id,kind,value) "
                     "VALUES (1,'knowledge_person','k1')")
    result = invoke(stores, '--json')
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['persons']['mapped'] == 3


def test_existing_wal_commits_visible_without_writes(stores):
    with closing(sqlite3.connect(stores[1])) as writer:
        writer.execute("DELETE FROM person_ids WHERE value='k1'")
        writer.commit()
        paths = [*stores, stores[1].with_name('people.db-wal')]
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]
        result = invoke(stores, '--json')
        assert result.exit_code == 1, result.output
        assert json.loads(result.output)['persons']['unmapped'] == 1
        assert [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths] == before


def test_query_only_connections_and_concurrent_clean_db_refusal(stores):
    from yeoman_gateway.people.knowledge_mapping import _read_database

    with pytest.raises(ValueError, match='changed'):
        with _read_database(stores[1]) as reader:
            reader.execute('PRAGMA query_only=ON')
            with pytest.raises(sqlite3.OperationalError):
                reader.execute('DELETE FROM persons')
            with closing(sqlite3.connect(stores[1])) as writer, writer:
                writer.execute("UPDATE persons SET notes='new' WHERE id=1")


def test_defaults_honor_yeoman_home(stores):
    from yeoman_shared.utils.helpers import get_operational_data_path

    root = get_operational_data_path()
    for source, target in zip(stores, [root / 'knowledge' / 'knowledge.db',
                                       root / 'people' / 'people.db'], strict=True):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    result = CliRunner().invoke(people_app, ['knowledge-mapping'])
    assert result.exit_code == 0, result.output
    assert 'persons.mapped: 3' in result.output
    assert 'k1' not in result.output


def test_details_refuses_git_path_even_when_symlink_points_outside():
    from yeoman_gateway.people.knowledge_mapping import write_mapping_details

    with tempfile.TemporaryDirectory(dir='/tmp', prefix='p3-symlink-') as root:
        root = Path(root)
        repo = root / 'repo'
        repo.mkdir(mode=0o700)
        (repo / '.git').write_text('gitdir: synthetic')
        target = root / 'private.json'
        target.write_text('preserve')
        link = repo / 'details.json'
        link.symlink_to(target)
        with pytest.raises(ValueError, match='git work tree'):
            write_mapping_details(link, [])
        assert target.read_text() == 'preserve'
