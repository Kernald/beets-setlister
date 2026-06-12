# Copyright 2015, Tom Jaspers <contact@tomjaspers.be>.
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.

"""Generates playlists from setlist.fm
https://github.com/tomjaspers/beets-setlister
"""

from __future__ import (division, absolute_import, print_function,
                        unicode_literals)

from beets.plugins import BeetsPlugin
from beets import ui
from beets.library import Item
from beets.dbcore.query import AndQuery, OrQuery, MatchQuery
from beets.util import mkdirall, normpath, syspath
from beets.autotag.distance import Distance
from beets.metadata_plugins import item_candidates
import os
import requests
import time

import subprocess


def _get_best_match(items, track_name, artist_name):
    """ Returns the best match (according to a track_name/artist_name distance)
    from a list of Items
    """

    def calc_distance(track_info, track_name, artist_name):
        dist = Distance()

        dist.add_string('track_title', track_name, track_info.title)

        if track_info.artist:
            dist.add_string('track_artist',
                            artist_name,
                            track_info.artist)

        return dist.distance

    matches = [(i, calc_distance(i, track_name, artist_name)) for i in items]
    matches.sort(key=lambda match: match[1])

    return matches[0]


def _get_mb_candidate(track_name, artist_name, threshold=0.2):
    """Returns the best candidate from MusicBrainz for a track_name/artist_name
    """
    candidates = list(item_candidates(Item(), artist_name, track_name))
    if not candidates:
        return None
    best_match = _get_best_match(candidates, track_name, artist_name)

    return best_match[0] if best_match[1] <= threshold else None


def _find_item_in_lib(lib, track_name, artist_name):
    """Finds an Item in the library based on the track_name.

    The track_name is not guaranteed to be perfect (i.e. as soon on MB),
    so in that case we query MB and look for the track id and query our
    lib with that.
    """

    # todo: sometimes returns matches by other artists when requested artist
    # has no matching tracks

    # Query the library based on the track name
    query = MatchQuery('title', track_name)
    lib_results = lib._fetch(Item, query=query)

    # Maybe the provided track name isn't all too good
    # todo: fails e.g. for Opeth - Reverie/Harlequin Forest
    #  due to mismatch in `/`

    # Search for the track on MusicBrainz, and use that info to retry our lib
    if not lib_results:
        mb_candidate = _get_mb_candidate(track_name, artist_name)
        if mb_candidate:
            query = OrQuery((
                        AndQuery((
                            MatchQuery('title', mb_candidate.title),
                            MatchQuery('artist', mb_candidate.artist),
                        )),
                        MatchQuery('mb_trackid', mb_candidate.track_id)
                    ))
            lib_results = lib._fetch(Item, query=query)

    if not lib_results:
        return None

    # If we get multiple Item results from our library, choose best match
    # using the distance
    if len(lib_results) > 1:
        return _get_best_match(lib_results, track_name, artist_name)[0]

    return lib_results[0]


def _setlist_name(setlist):
    """Name (and playlist filename stem) for a parsed setlist."""
    return u'{0} at {1} ({2})'.format(setlist['artist_name'],
                                      setlist['venue_name'],
                                      setlist['event_date'])


def _save_playlist(m3u_path, items):
    """Saves a list of Items as a playlist at m3u_path
    """
    mkdirall(m3u_path)
    with open(syspath(m3u_path), 'w') as f:
        for item in items:
            f.write(item.path.decode('utf-8') + u'\n')


# Reference: https://api.setlist.fm/docs/1.0/resource__1.0_search_setlists.html
SETLISTFM_ENDPOINT = 'https://api.setlist.fm/rest/1.0/search/setlists'


def _parse_setlist(setlist):
    """Turn one raw setlist.fm setlist object into the event/track info we
    care about, or return None when the setlist has no songs (setlist.fm has
    plenty of attended events with an empty `{"set": []}`).
    """
    track_names = [song['name']
                   for subset in setlist['sets']['set']
                   for song in subset.get('song', [])]

    if not track_names:
        return None

    return {'artist_name': setlist['artist']['name'],
            'venue_name': setlist['venue']['name'],
            'event_date': setlist['eventDate'],
            'track_names': track_names}


def _get_setlist(session, artist_name, date=None):
    """Query setlist.fm for an artist and return the first
    complete setlist, alongside some information about the event
    """
    # Query setlistfm using the artist_name
    response = session.get(SETLISTFM_ENDPOINT, params={
               'artistName': artist_name,
               'date': date,
               })

    if not response.status_code == 200:
        return None

    # Setlist.fm can have some events with empty setlists
    # We'll just pick the first event with a non-empty setlist
    setlists = response.json()['setlist']
    if not isinstance(setlists, list):
        setlists = [setlists]
    for setlist in setlists:
        parsed = _parse_setlist(setlist)
        if parsed:
            return parsed

    return None


# Reference:
# https://api.setlist.fm/docs/1.0/resource__1.0_user__userId__attended.html
ATTENDED_ENDPOINT = 'https://api.setlist.fm/rest/1.0/user/{user}/attended'

# setlist.fm exposes no rate-limit headers; standard keys allow ~2 req/s. We
# only issue ceil(total / itemsPerPage) requests, but space them politely.
_PAGE_PAUSE = 0.5

# On HTTP 429 setlist.fm sends no Retry-After in practice, so we fall back to
# exponential backoff. These bound how patient we are before giving up.
_MAX_RETRIES = 5
_BACKOFF_BASE = 1.0


def _page_count(data):
    """Number of pages from a setlist.fm paginated response."""
    total = data.get('total', 0)
    per_page = data.get('itemsPerPage') or 1
    return max(1, -(-total // per_page))  # ceil division


def _fetch_attended_page(session, user, page):
    """Fetch one page of a user's attended setlists. Returns the parsed JSON,
    or None when the page does not exist / cannot be fetched.

    On HTTP 429 we honor a `Retry-After` header if present, otherwise back off
    exponentially, retrying up to `_MAX_RETRIES` times before giving up.
    """
    backoff = _BACKOFF_BASE
    for _ in range(_MAX_RETRIES):
        response = session.get(ATTENDED_ENDPOINT.format(user=user),
                               params={'p': page})
        if response.status_code == 200:
            return response.json()
        if response.status_code == 429:
            retry_after = response.headers.get('Retry-After')
            time.sleep(float(retry_after) if retry_after else backoff)
            backoff *= 2
            continue
        return None  # 404 and other errors: page unavailable
    return None


def _get_attended_setlists(session, user):
    """Return parsed setlists for every concert `user` has marked as attended.

    Pages are walked from 1 to ceil(total / itemsPerPage); a missing/empty page
    stops the walk early as a defensive guard against an unreliable `total`.
    """
    setlists = []

    data = _fetch_attended_page(session, user, 1)
    if data is None:
        return setlists

    pages = _page_count(data)
    for page in range(1, pages + 1):
        if page > 1:
            time.sleep(_PAGE_PAUSE)
            data = _fetch_attended_page(session, user, page)
            if data is None:
                break

        raw = data.get('setlist') or []
        if not isinstance(raw, list):
            raw = [raw]
        if not raw:
            break
        for setlist in raw:
            parsed = _parse_setlist(setlist)
            if parsed:
                setlists.append(parsed)

    return setlists


class SetlisterPlugin(BeetsPlugin):
    def __init__(self):
        super(SetlisterPlugin, self).__init__()
        self.config.add({
            'playlist_dir': None,
            'api_key': '',
            'user': None,
        })

        if not os.path.isdir(
                os.path.expanduser(
                    self.config['playlist_dir'].get(str)
                )
        ):
            self._log.warning(u'You have to configure a valid `playlist_dir`')
            return

        if not self.config['api_key']:
            self._log.warning(
                u'You have to provide your setlist.fm API key. '
                u'Request a key at https://www.setlist.fm/settings/apps and '
                u'configure it as `api_key` as `api_key`'
            )
            return

        self.session = requests.Session()
        self.session.headers = {
            'Accept': 'application/json',
            'User-Agent': 'beets',
            'x-api-key': self.config['api_key'].get(str)
        }

    def setlister(self, lib, artist_name, date=None, play=False,
                  attended=False, user=None):
        """Glue everything together
        """

        if attended:
            if artist_name or date or play:
                self._log.warning(
                    u'--attended ignores the artist argument, --date and '
                    u'--play')
            self._generate_attended_playlists(lib, user)
            return

        # Support `$ beet setlister red hot chili peppers`
        if isinstance(artist_name, list):
            artist_name = ' '.join(artist_name)

        if not artist_name:
            self._log.warning(u'You have to provide an artist')
            return

        # Extract setlist information from setlist.fm
        try:
            setlist = _get_setlist(self.session, artist_name, date)
        except Exception:
            self._log.info(u'error scraping setlist.fm for {0}'.format(
                            artist_name))
            return

        if not setlist:
            self._log.info(u'could not find a setlist for {0}'.format(
                           artist_name))
            return

        if self._generate_playlist(lib, setlist) and play:
            # todo: Double check whether this is sensible ~ beets documentation
            #  (it probably isn't)
            m3u_path = normpath(os.path.join(
                self.config['playlist_dir'].as_filename(),
                _setlist_name(setlist) + '.m3u'))
            subprocess.Popen(['xdg-open', m3u_path.decode('utf-8')])

    def _generate_attended_playlists(self, lib, user=None):
        """Generate a playlist for every concert `user` has marked attended."""
        user = user or self.config['user'].get()
        if not user:
            self._log.warning(
                u'Set `setlister.user` (or pass --user) to generate attended '
                u'playlists')
            return

        setlists = _get_attended_setlists(self.session, user)
        if not setlists:
            self._log.info(u'No attended setlists found for {0}'.format(user))
            return

        written = sum(1 for setlist in setlists
                      if self._generate_playlist(lib, setlist))
        self._log.info(
            u'{0} playlist(s) written, {1} concert(s) skipped '
            u'(no matching tracks)'.format(written, len(setlists) - written))

    def _generate_playlist(self, lib, setlist):
        """Match a parsed setlist against the library and write its playlist.

        Returns True when a playlist was written, False when the concert had no
        matching tracks (in which case no file is created).
        """
        setlist_name = _setlist_name(setlist)
        self._log.info(u'Setlist: {0} ({1} tracks)'.format(
                        setlist_name, len(setlist['track_names'])))

        # Match the setlist' tracks with items in our library
        items, _ = self.find_items_in_lib(lib,
                                          setlist['track_names'],
                                          setlist['artist_name'])

        if not items:
            self._log.info(
                u'No library tracks matched "{0}", skipping'.format(
                    setlist_name))
            return False

        # Save the items as a playlist
        m3u_path = normpath(os.path.join(
                                self.config['playlist_dir'].as_filename(),
                                setlist_name + '.m3u'))

        _save_playlist(m3u_path, items)
        self._log.info(
            u'Saved playlist at "{0}"'.format(m3u_path.decode('utf-8'))
        )
        return True

    def find_items_in_lib(self, lib, track_names, artist_name):
        """Returns a list of items found, and a list of items not found,
        from a given list of track names.
        """
        items, missing_items = [], []
        for track_nr, track_name in enumerate(track_names):
            item = _find_item_in_lib(lib, track_name, artist_name)
            if item:
                items += [item]
                message = ui.colorize('text_success', u'found')
            else:
                missing_items += [item]
                message = ui.colorize('text_error', u'not found')
            self._log.info("{0} {1}: {2}".format(
                          (track_nr+1), track_name, message))
        return items, missing_items

    def commands(self):
        def func(lib, opts, args):
            self.setlister(lib, ui.decargs(args), opts.date, opts.play,
                           attended=opts.attended, user=opts.user)

        cmd = ui.Subcommand(
            'setlister',
            help='create playlist from an artists\' latest setlist'
        )
        cmd.parser.add_option('-d', '--date', dest='date', default=None,
                              help='setlist of a specific date (dd-MM-yyyy)')
        cmd.parser.add_option('-p', '--play', action='store_true',
                              help='play the playlist (boolean)')
        cmd.parser.add_option('-a', '--attended', dest='attended',
                              action='store_true', default=False,
                              help='generate a playlist for every concert the '
                                   'configured user attended')
        cmd.parser.add_option('-u', '--user', dest='user', default=None,
                              help='setlist.fm username for --attended '
                                   '(overrides the `user` config option)')

        cmd.func = func

        return [cmd]
