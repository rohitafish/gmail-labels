"""Covers filter_audit.py: reading a Gmail filter export, turning each filter
into a search, spotting duplicates and redundant filters, the verdicts, the
search cache, the CSV main() writes, and --prune.

Every address, domain and label here is invented. No real Gmail account is
used: gc.service is monkeypatched to return the small fake below, which
answers messages.list by looking the query up in a dict.
"""

import csv
import json
import sys
from datetime import UTC, datetime, timedelta

import pytest
from conftest import _Call, make_label, utc

import filter_audit as fa
import gmail_common as gc

NOW = utc(2026, 6, 1)


def entry_xml(fid, **props):
    lines = ''.join("<apps:property name='%s' value='%s'/>" % kv for kv in props.items())
    return ("<entry><category term='filter'></category><title>Mail Filter</title>"
            "<id>tag:mail.google.com,2008:filter:%s</id><content></content>%s"
            "<apps:property name='sizeOperator' value='s_sl'/>"
            "<apps:property name='sizeUnit' value='s_smb'/></entry>" % (fid, lines))


def write_export(path, *entries):
    path.write_text(
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<feed xmlns='http://www.w3.org/2005/Atom' "
        "xmlns:apps='http://schemas.google.com/apps/2006'>"
        "<title>Mail Filters</title><id>tag:mail.google.com,2008:filters:1,2</id>"
        + ''.join(entries) + '</feed>', encoding='utf-8')
    return path


class FakeSearchService:
    """messages.list(q=...) answers from `results`: query -> list of message
    datetimes, newest first. A query ending in ' after:YYYY/MM/DD' counts
    only the messages after that date."""

    def __init__(self, labels=(), results=None):
        self.label_list = list(labels)
        self.results = dict(results or {})
        self.calls = []
        self.errors = {}
        self.queries = []

    def users(self):
        return self

    def labels(self):
        svc = self

        class _Labels:
            def list(self, userId='me'):
                return _Call(svc, 'labels.list', lambda: {'labels': svc.label_list})
        return _Labels()

    def messages(self):
        svc = self

        class _Messages:
            def list(self, userId='me', q='', maxResults=None, includeSpamTrash=False):
                assert includeSpamTrash is True
                svc.queries.append(q)
                return _Call(svc, 'messages.list', lambda: svc._list(q, maxResults))

            def get(self, userId='me', id=None, format=None):
                return _Call(svc, 'messages.get', lambda: svc._get(id))
        return _Messages()

    def _list(self, q, max_results):
        base, _, after = q.partition(' after:')
        dates = self.results.get(base, [])
        if after:
            cutoff = datetime.strptime(after, '%Y/%m/%d').replace(tzinfo=UTC)
            dates = [d for d in dates if d >= cutoff]
        page = dates[:max_results]
        res = {'messages': [{'id': '%s|%d' % (base, i)} for i in range(len(page))]}
        if len(dates) > max_results:
            res['nextPageToken'] = 'more'
        return res

    def _get(self, message_id):
        base, i = message_id.rsplit('|', 1)
        when = self.results[base][int(i)]
        return {'internalDate': str(int(when.timestamp() * 1000))}


# ------------------------- reading and queries -------------------------

def test_load_filters_reads_ids_and_properties_in_order(tmp_path):
    path = write_export(tmp_path / 'f.xml',
                        entry_xml('111', hasTheWord='shop.example.com', label='Shops'),
                        entry_xml('222', **{'from': 'news@club.example.com'}))
    filters = fa.load_filters(path)
    assert [f['id'] for f in filters] == ['111', '222']
    assert [f['n'] for f in filters] == [1, 2]
    assert filters[0]['props']['label'] == 'Shops'


@pytest.mark.parametrize(('props', 'query'), [
    ({'from': 'a@x.example.com'}, 'from:(a@x.example.com)'),
    ({'to': 'me@x.example.com', 'subject': 'Invoice'}, 'to:(me@x.example.com) subject:(Invoice)'),
    ({'hasTheWord': 'x.example.com OR y.example.com'}, '(x.example.com OR y.example.com)'),
    ({'doesNotHaveTheWord': 'unsubscribe'}, '-{unsubscribe}'),
    ({'hasAttachment': 'true'}, 'has:attachment'),
    ({'size': '5', 'sizeOperator': 's_sl', 'sizeUnit': 's_smb'}, 'larger:5M'),
    ({'size': '200', 'sizeOperator': 's_ss', 'sizeUnit': 's_skb'}, 'smaller:200K'),
    ({'sizeOperator': 's_sl', 'sizeUnit': 's_smb', 'label': 'X'}, ''),
])
def test_build_query(props, query):
    assert fa.build_query(props) == query


def test_describe_actions_lists_everything_but_the_label():
    props = {'label': 'X', 'forwardTo': 'b@x.example.com', 'shouldArchive': 'true',
             'smartLabelToApply': '^smartlabel_promo'}
    assert fa.describe_actions(props) == 'category forward Archive'


# ------------------------- overlaps -------------------------

def f(n, **props):
    return {'n': n, 'id': str(n), 'props': props}


def test_identical_filters_are_duplicates_of_the_first():
    filters = [f(1, hasTheWord='shop.example.com', label='Shops'),
               f(2, hasTheWord='Shop.Example.com ', label='Shops'),
               f(3, hasTheWord='shop.example.com', label='Other')]
    result = fa.find_overlaps(filters)
    assert result == {2: ('DUPLICATE', 'Same criteria and actions as filter 1.')}


def test_a_narrower_filter_with_the_same_actions_is_redundant():
    filters = [f(1, hasTheWord='shop.example.com', label='Shops'),
               f(2, hasTheWord='news@mail.shop.example.com', label='Shops'),
               f(3, hasTheWord='deals.shop.example.com', label='Shops'),
               f(4, hasTheWord='other@shop.example.com', label='Elsewhere'),
               f(5, hasTheWord='shop.example.com sale', label='Shops')]
    result = fa.find_overlaps(filters)
    assert set(result) == {2, 3}
    assert result[2][0] == 'REDUNDANT'
    assert 'Filter 1 (shop.example.com)' in result[3][1]


def test_redundancy_is_only_judged_within_the_same_field():
    filters = [f(1, **{'from': 'shop.example.com', 'label': 'Shops'}),
               f(2, hasTheWord='news@shop.example.com', label='Shops')]
    assert fa.find_overlaps(filters) == {}


# ------------------------- classify -------------------------

LABELS = {'Shops', 'Clubs'}


def m(days_ago, recent=0):
    return {'last': (NOW - timedelta(days=days_ago)).isoformat(), 'recent': recent}


@pytest.mark.parametrize(('props', 'overlap', 'match', 'verdict', 'action'), [
    ({'label': 'Shops'}, None, None, 'NO CRITERIA', 'REVIEW'),
    ({'hasTheWord': 'x.example.com'}, None, None, 'NO ACTION', 'DELETE'),
    ({'hasTheWord': 'x.example.com', 'label': 'Shops'}, ('DUPLICATE', 'n'), None,
     'DUPLICATE', 'DELETE'),
    ({'hasTheWord': 'x.example.com', 'label': 'Shops'}, None,
     {'last': None, 'recent': 0}, 'NEVER MATCHED', 'DELETE'),
    ({'hasTheWord': 'x.example.com', 'label': 'Shops'}, None, m(900), 'DEAD', 'DELETE'),
    ({'hasTheWord': 'x.example.com', 'label': 'Shops'}, None, m(500), 'QUIET', 'REVIEW'),
    ({'hasTheWord': 'x.example.com', 'label': 'Shops'}, None, m(10, 3), 'ACTIVE', 'KEEP'),
    ({'hasTheWord': 'x.example.com', 'label': 'Gone'}, None, m(10, 3), 'ACTIVE', 'REVIEW'),
    ({'hasTheWord': 'x.example.com', 'label': 'Shops', 'shouldNeverSpam': 'true'}, None,
     m(900), 'DEAD', 'REVIEW'),
    ({'hasTheWord': 'x.example.com', 'forwardTo': 'b@x.example.com'}, None,
     {'last': None, 'recent': 0}, 'NEVER MATCHED', 'REVIEW'),
])
def test_classify(props, overlap, match, verdict, action):
    got_verdict, got_action, _note = fa.classify(props, overlap, match, LABELS, NOW)
    assert (got_verdict, got_action) == (verdict, action)


def test_classify_notes_a_missing_label_and_the_trash_caveat():
    _v, _a, note = fa.classify({'hasTheWord': 'x.example.com', 'label': 'Gone',
                                'shouldTrash': 'true'}, None, m(900), LABELS, NOW)
    assert 'Its label "Gone" no longer exists.' in note
    assert '30 days' in note


# ------------------------- search -------------------------

def test_search_reports_newest_match_and_recent_count():
    since = NOW - timedelta(days=365)
    svc = FakeSearchService(results={'(x.example.com)': [NOW - timedelta(days=d)
                                                     for d in (5, 40, 400)]})
    assert fa.search(svc, '(x.example.com)', since) == {
        'last': (NOW - timedelta(days=5)).isoformat(), 'recent': 2}


def test_search_skips_the_count_when_the_newest_match_is_old():
    svc = FakeSearchService(results={'(x.example.com)': [NOW - timedelta(days=800)]})
    result = fa.search(svc, '(x.example.com)', NOW - timedelta(days=365))
    assert result['recent'] == 0
    assert svc.calls == ['messages.list', 'messages.get']


def test_search_with_no_match_and_a_capped_count(monkeypatch):
    monkeypatch.setattr(fa, 'COUNT_CAP', 2)
    svc = FakeSearchService(results={'(busy.example.com)': [NOW - timedelta(days=d)
                                                        for d in (1, 2, 3)]})
    assert fa.search(svc, '(none.example.com)', NOW)['last'] is None
    assert fa.search(svc, '(busy.example.com)', NOW - timedelta(days=365))['recent'] == '2+'


# ------------------------- cache -------------------------

def test_cache_round_trip_drops_stale_and_malformed_entries(tmp_path):
    path = str(tmp_path / 'cache.json')
    fresh = {'last': None, 'recent': 0, 'fetched': NOW.isoformat()}
    old = dict(fresh, fetched=(NOW - timedelta(days=30)).isoformat())
    fa.save_cache(path, {'(a)': fresh, '(b)': old, '(c)': {'last': None},
                         '(d)': dict(fresh, fetched='nonsense'), '(e)': 'junk'})
    assert fa.load_cache(path, False, NOW) == {'(a)': fresh}
    assert fa.load_cache(path, True, NOW) == {}
    assert oct(__import__('os').stat(path).st_mode & 0o777) == '0o600'


@pytest.mark.parametrize('content', ['not json', json.dumps({'version': 2}), '[]'])
def test_unreadable_or_foreign_cache_is_ignored(tmp_path, content):
    path = tmp_path / 'cache.json'
    path.write_text(content)
    assert fa.load_cache(str(path), False, NOW) == {}
    assert fa.load_cache(str(tmp_path / 'missing.json'), False, NOW) == {}


# ------------------------- main and prune -------------------------

def _run(monkeypatch, tmp_path, svc, *argv):
    monkeypatch.setattr(fa.gc, 'service', lambda scope, token_filename: svc)
    monkeypatch.setattr(sys, 'argv', ['filter_audit.py', '--xml', str(tmp_path / 'f.xml'),
                                      '--out', str(tmp_path / 'out.csv'),
                                      '--cache', str(tmp_path / 'cache.json'), *argv])
    fa.main()


def _rows(tmp_path):
    with open(tmp_path / 'out.csv', encoding='utf-8-sig') as fh:
        return list(csv.DictReader(fh))


def test_main_writes_the_csv_sorted_delete_review_keep(tmp_path, monkeypatch, capsys):
    now = datetime.now(UTC)
    write_export(tmp_path / 'f.xml',
                 entry_xml('101', hasTheWord='live.example.com', label='Shops'),
                 entry_xml('102', hasTheWord='dead.example.com', label='Shops'),
                 entry_xml('103', hasTheWord='dead.example.com', label='Shops'),
                 entry_xml('104', hasTheWord='-quiet.example.com', label='Clubs'),
                 entry_xml('105', hasTheWord='live2.example.com', label='Shops'))
    svc = FakeSearchService(
        labels=[make_label('Shops'), make_label('Clubs')],
        results={'(live.example.com)': [now - timedelta(days=3)],
                 '(live2.example.com)': [now - timedelta(days=3)],
                 '(-quiet.example.com)': [now - timedelta(days=500)]})

    _run(monkeypatch, tmp_path, svc)
    rows = _rows(tmp_path)

    assert [(r['FILTER_ID'], r['VERDICT'], r['ACTION']) for r in rows] == [
        ('103', 'DUPLICATE', 'DELETE'),
        ('102', 'NEVER MATCHED', 'DELETE'),
        ('104', 'QUIET', 'REVIEW'),
        ('101', 'ACTIVE', 'KEEP'),
        ('105', 'ACTIVE', 'KEEP'),
    ]
    assert rows[2]['CRITERIA'] == '(-quiet.example.com)'
    assert rows[3]['RECENT_MATCHES'] == '1'
    out = capsys.readouterr().out
    assert 'Suggested: DELETE 2, REVIEW 1, KEEP 2' in out
    assert '1 labels are fed by 2+ filters' in out

    # A second run reuses every cached search.
    svc.calls.clear()
    _run(monkeypatch, tmp_path, svc)
    assert 'messages.list' not in svc.calls


def test_main_limit_and_progress_saves(tmp_path, monkeypatch, capsys):
    entries = [entry_xml(str(i), hasTheWord='s%d.example.com' % i, label='Shops')
               for i in range(30)]
    write_export(tmp_path / 'f.xml', *entries)
    svc = FakeSearchService(labels=[make_label('Shops')])

    _run(monkeypatch, tmp_path, svc, '--limit', '26')

    assert len(_rows(tmp_path)) == 26
    assert '...25/26 filters checked' in capsys.readouterr().out


def test_prune_drops_delete_rows_and_keeps_the_rest(tmp_path, monkeypatch, capsys):
    write_export(tmp_path / 'f.xml',
                 entry_xml('101', hasTheWord='keep.example.com', label='Shops'),
                 entry_xml('102', hasTheWord='drop.example.com', label='Shops'))
    (tmp_path / 'edited.csv').write_text(
        'N,FILTER_ID,ACTION\n1,101,REVIEW\n2,102,delete\n', encoding='utf-8')
    out = tmp_path / 'pruned.xml'

    _run(monkeypatch, tmp_path, None, '--prune', str(tmp_path / 'edited.csv'),
         '--pruned-out', str(out))

    kept = fa.load_filters(out)
    assert [x['id'] for x in kept] == ['101']
    assert kept[0]['props']['hasTheWord'] == 'keep.example.com'
    text = out.read_text()
    assert "xmlns:apps=" in text and 'filters:101<' in text
    assert 'Kept 1 filters, dropped 1.' in capsys.readouterr().out


def test_prune_refuses_a_csv_from_another_export(tmp_path):
    write_export(tmp_path / 'f.xml', entry_xml('101', hasTheWord='a.example.com', label='X'))
    (tmp_path / 'other.csv').write_text('FILTER_ID,ACTION\n999,DELETE\n')
    (tmp_path / 'wrong.csv').write_text('LABEL,ACTION\nX,SKIP\n')
    with pytest.raises(SystemExit, match='not in'):
        fa.prune(tmp_path / 'f.xml', tmp_path / 'other.csv', tmp_path / 'o.xml')
    with pytest.raises(SystemExit, match='FILTER_ID'):
        fa.prune(tmp_path / 'f.xml', tmp_path / 'wrong.csv', tmp_path / 'o.xml')


def test_defaults_point_inside_the_repo():
    assert fa.DEFAULT_XML.startswith(gc.HERE)
    assert fa.FILTER_CACHE_FILE.startswith(gc.HERE)
