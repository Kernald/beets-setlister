"""Tests for beetsplug.setlister."""

import json
import os

from beetsplug import setlister

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')


def _load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# _parse_setlist
# ---------------------------------------------------------------------------

def test_parse_setlist_extracts_event_and_tracks():
    page = _load('attended_page1.json')
    # The fixture's first populated concert is Mumford & Sons (27 songs).
    populated = next(s for s in page['setlist']
                     if s['artist']['name'] == 'Mumford & Sons')

    parsed = setlister._parse_setlist(populated)

    assert parsed['artist_name'] == 'Mumford & Sons'
    assert parsed['event_date'] == populated['eventDate']
    assert parsed['venue_name'] == populated['venue']['name']
    assert parsed['track_names'][0] == 'Ring of Fire'
    assert len(parsed['track_names']) == 27


def test_parse_setlist_returns_none_for_empty_setlist():
    page = _load('attended_page1.json')
    # Hudson Freeman is attended but has no recorded songs: {"set": []}.
    empty = next(s for s in page['setlist']
                 if s['artist']['name'] == 'Hudson Freeman')

    assert setlister._parse_setlist(empty) is None


# ---------------------------------------------------------------------------
# _get_mb_candidate
# ---------------------------------------------------------------------------

def test_get_mb_candidate_returns_none_when_no_candidates(monkeypatch):
    # A bulk attended run hits many tracks absent from both the library and
    # MusicBrainz; an empty candidate list must not crash the whole run.
    monkeypatch.setattr(setlister, 'item_candidates', lambda *a, **k: iter([]))

    result = setlister._get_mb_candidate('Unknown Track', 'Unknown Artist')
    assert result is None


# ---------------------------------------------------------------------------
# _get_attended_setlists (pagination + rate limiting)
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    """Returns a canned response per requested page; records page order."""
    def __init__(self, responses):
        self.responses = responses  # {page_number: FakeResponse}
        self.requested = []

    def get(self, url, params=None):
        page = params['p']
        self.requested.append(page)
        return self.responses.get(page, FakeResponse(status_code=404))


def _setlist_json(artist, tracks):
    sets = {'set': [{'song': [{'name': t} for t in tracks]}]} if tracks \
        else {'set': []}
    return {'artist': {'name': artist},
            'eventDate': '01-01-2020',
            'venue': {'name': 'Venue', 'city': {'name': 'City'}},
            'sets': sets}


def _page(total, items_per_page, page, setlists):
    return FakeResponse(payload={'total': total,
                                 'itemsPerPage': items_per_page,
                                 'page': page,
                                 'setlist': setlists})


def test_get_attended_setlists_paginates_and_skips_empty(monkeypatch):
    monkeypatch.setattr(setlister.time, 'sleep', lambda *a, **k: None)
    session = FakeSession({
        1: _page(3, 2, 1, [_setlist_json('A', ['s1']),
                           _setlist_json('Empty', [])]),
        2: _page(3, 2, 2, [_setlist_json('B', ['s2', 's3'])]),
    })

    result = setlister._get_attended_setlists(session, 'someuser')

    # Empty concert dropped; both populated concerts returned, in order.
    assert [s['artist_name'] for s in result] == ['A', 'B']
    # ceil(3/2) = 2 pages computed from the payload; page 3 never requested.
    assert session.requested == [1, 2]


class SeqSession:
    """Returns the next response in a list on each call (ignores the page)."""
    def __init__(self, responses):
        self._responses = iter(responses)

    def get(self, url, params=None):
        return next(self._responses)


def test_fetch_attended_page_retries_on_429_honoring_retry_after(monkeypatch):
    slept = []
    monkeypatch.setattr(setlister.time, 'sleep', lambda s: slept.append(s))
    session = SeqSession([
        FakeResponse(status_code=429, headers={'Retry-After': '3'}),
        FakeResponse(status_code=200, payload={'setlist': []}),
    ])

    data = setlister._fetch_attended_page(session, 'u', 1)

    assert data == {'setlist': []}
    assert slept == [3.0]  # waited exactly as long as the header said


def test_fetch_attended_page_backs_off_without_retry_after(monkeypatch):
    slept = []
    monkeypatch.setattr(setlister.time, 'sleep', lambda s: slept.append(s))
    session = SeqSession([
        FakeResponse(status_code=429),
        FakeResponse(status_code=429),
        FakeResponse(status_code=200, payload={'setlist': []}),
    ])

    data = setlister._fetch_attended_page(session, 'u', 1)

    assert data == {'setlist': []}
    assert slept == [1.0, 2.0]  # exponential fallback


def test_fetch_attended_page_returns_none_for_missing_page():
    session = SeqSession([FakeResponse(status_code=404)])

    assert setlister._fetch_attended_page(session, 'u', 99) is None


# ---------------------------------------------------------------------------
# SetlisterPlugin._generate_playlist
# ---------------------------------------------------------------------------

def _plugin_with_playlist_dir(playlist_dir):
    from beets import config
    config['setlister']['playlist_dir'] = str(playlist_dir)
    config['setlister']['api_key'] = 'dummy'
    return setlister.SetlisterPlugin()


def _parsed(artist, venue, date, tracks):
    return {'artist_name': artist, 'venue_name': venue,
            'event_date': date, 'track_names': tracks}


def test_generate_playlist_writes_named_file_when_tracks_match(tmp_path):
    from beets.library import Library, Item
    plugin = _plugin_with_playlist_dir(tmp_path)
    lib = Library(':memory:')
    lib.add(Item(title='Ring of Fire', artist='Mumford & Sons',
                 path=b'/music/rof.flac'))

    wrote = plugin._generate_playlist(
        lib, _parsed('Mumford & Sons', 'Venue', '01-01-2020',
                     ['Ring of Fire', 'Not In Library']))

    assert wrote is True
    m3u = tmp_path / 'Mumford & Sons at Venue (01-01-2020).m3u'
    assert m3u.exists()
    assert m3u.read_text().strip() == '/music/rof.flac'


def test_generate_playlist_skips_and_writes_nothing_when_no_match(tmp_path):
    from beets.library import Library
    plugin = _plugin_with_playlist_dir(tmp_path)
    lib = Library(':memory:')  # empty library

    wrote = plugin._generate_playlist(
        lib, _parsed('Nobody', 'Venue', '01-01-2020', ['X', 'Y']))

    assert wrote is False
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# _generate_attended_playlists (end-to-end glue)
# ---------------------------------------------------------------------------

def test_generate_attended_playlists_writes_matches_and_skips_rest(
        tmp_path, monkeypatch):
    monkeypatch.setattr(setlister.time, 'sleep', lambda *a, **k: None)
    from beets.library import Library, Item
    plugin = _plugin_with_playlist_dir(tmp_path)
    plugin.session = FakeSession({
        1: _page(2, 20, 1, [
            _setlist_json('Mumford & Sons', ['Ring of Fire']),  # in library
            _setlist_json('Nobody', ['Unknown Song']),  # not in library
        ]),
    })
    lib = Library(':memory:')
    lib.add(Item(title='Ring of Fire', artist='Mumford & Sons',
                 path=b'/music/rof.flac'))

    plugin._generate_attended_playlists(lib, user='Kernald')

    # Only the concert with a matching track produced a file.
    assert sorted(p.name for p in tmp_path.iterdir()) == \
        ['Mumford & Sons at Venue (01-01-2020).m3u']


def test_generate_attended_playlists_warns_without_user(tmp_path):
    from beets.library import Library
    plugin = _plugin_with_playlist_dir(tmp_path)
    plugin.config['user'] = None

    plugin._generate_attended_playlists(Library(':memory:'), user=None)

    # No username available anywhere -> nothing written.
    assert list(tmp_path.iterdir()) == []
