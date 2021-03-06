# coding=utf-8
# Author: Nic Wolfe <nic@wolfeden.ca>
#
# This file is part of Medusa.
#
# Medusa is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Medusa is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Medusa. If not, see <http://www.gnu.org/licenses/>.
"""tv_cache code."""
from __future__ import unicode_literals

import datetime
import itertools
import time
import traceback

from six import text_type
from . import app, db, logger, show_name_helpers
from .helper.common import episode_num
from .helper.exceptions import AuthException
from .name_parser.parser import InvalidNameException, InvalidShowException, NameParser
from .rss_feeds import getFeed
from .show.show import Show


class CacheDBConnection(db.DBConnection):
    """Cache database class."""

    def __init__(self, provider_id):
        """Initialize the class."""
        db.DBConnection.__init__(self, 'cache.db')

        # Create the table if it's not already there
        try:
            if not self.hasTable(provider_id):
                logger.log('Creating cache table for provider {0}'.format(provider_id), logger.DEBUG)
                self.action(
                    b'CREATE TABLE [{provider_id}] (name TEXT, season NUMERIC, episodes TEXT, indexerid NUMERIC, '
                    b'url TEXT, time NUMERIC, quality NUMERIC, release_group TEXT)'.format(provider_id=provider_id))
            else:
                sql_results = self.select(b'SELECT url, COUNT(url) AS count FROM [{provider_id}] '
                                          b'GROUP BY url HAVING count > 1'.format(provider_id=provider_id))

                for cur_dupe in sql_results:
                    self.action(b'DELETE FROM [{provider_id}] WHERE url = ?'.format(provider_id=provider_id), [cur_dupe[b'url']])

            # remove wrong old index
            self.action(b'DROP INDEX IF EXISTS idx_url')

            # add unique index to prevent further dupes from happening if one does not exist
            logger.log('Creating UNIQUE URL index for {0}'.format(provider_id), logger.DEBUG)
            self.action(b'CREATE UNIQUE INDEX IF NOT EXISTS idx_url_{0}  ON [{1}] (url)'.
                        format(provider_id, provider_id))

            # add release_group column to table if missing
            if not self.hasColumn(provider_id, 'release_group'):
                self.addColumn(provider_id, 'release_group', 'TEXT', '')

            # add version column to table if missing
            if not self.hasColumn(provider_id, 'version'):
                self.addColumn(provider_id, 'version', 'NUMERIC', '-1')

            # add seeders column to table if missing
            if not self.hasColumn(provider_id, 'seeders'):
                self.addColumn(provider_id, 'seeders', 'NUMERIC', '-1')

            # add leechers column to table if missing
            if not self.hasColumn(provider_id, 'leechers'):
                self.addColumn(provider_id, 'leechers', 'NUMERIC', '-1')

            # add size column to table if missing
            if not self.hasColumn(provider_id, 'size'):
                self.addColumn(provider_id, 'size', 'NUMERIC', '-1')

            # add pubdate column to table if missing
            if not self.hasColumn(provider_id, 'pubdate'):
                self.addColumn(provider_id, 'pubdate', 'NUMERIC', '')

            # add proper_tags column to table if missing
            if not self.hasColumn(provider_id, 'proper_tags'):
                self.addColumn(provider_id, 'proper_tags', 'TEXT', '')

        except Exception as e:
            if str(e) != 'table [{provider_id}] already exists'.format(provider_id=provider_id):
                raise

        # Create the table if it's not already there
        try:
            if not self.hasTable('lastUpdate'):
                self.action(b'CREATE TABLE lastUpdate (provider TEXT, time NUMERIC)')
        except Exception as e:
            logger.log('Error while searching {provider_id}, skipping: {e!r}'.
                       format(provider_id=provider_id, e=e), logger.DEBUG)
            logger.log(traceback.format_exc(), logger.DEBUG)
            if str(e) != 'table lastUpdate already exists':
                raise


class TVCache(object):
    """TVCache class."""

    def __init__(self, provider, **kwargs):
        """Initialize class."""
        self.provider = provider
        self.provider_id = self.provider.get_id()
        self.provider_db = None
        self.minTime = kwargs.pop('min_time', 10)
        self.search_params = kwargs.pop('search_params', dict(RSS=['']))

    def _get_db(self):
        """Initialize provider database if not done already."""
        if not self.provider_db:
            self.provider_db = CacheDBConnection(self.provider_id)

        return self.provider_db

    def _clear_cache(self):
        """Perform reqular cache cleaning as required."""
        # if cache trimming is enabled
        if app.CACHE_TRIMMING:
            # trim items older than MAX_CACHE_AGE days
            self.trim_cache(days=app.MAX_CACHE_AGE)

    def trim_cache(self, days=None):
        """
        Remove old items from cache.

        :param days: Number of days to retain
        """
        if days:
            now = int(time.time())  # current timestamp
            retention_period = now - (days * 86400)
            logger.log('Removing cache entries older than {x} days from {provider}'.format
                       (x=days, provider=self.provider_id))
            cache_db_con = self._get_db()
            cache_db_con.action(
                b'DELETE FROM [{provider}] '
                b'WHERE time < ? '.format(provider=self.provider_id),
                [retention_period]
            )

    def _get_title_and_url(self, item):
        """Return title and url from item."""
        return self.provider._get_title_and_url(item)

    def _get_result_info(self, item):
        """Return seeders and leechers from item."""
        return self.provider._get_result_info(item)

    def _get_size(self, item):
        """Return size of the item."""
        return self.provider._get_size(item)

    def _get_pubdate(self, item):
        """Return publish date of the item."""
        return self.provider._get_pubdate(item)

    def _get_rss_data(self):
        """Return rss data."""
        return {'entries': self.provider.search(self.search_params)} if self.search_params else None

    def _check_auth(self, data):
        """Check if we are autenticated."""
        return True

    def _check_item_auth(self, title, url):
        """Check item auth."""
        return True

    def update_cache(self):
        """Update provider cache."""
        # check if we should update
        if not self.should_update():
            return

        try:
            data = self._get_rss_data()
            if self._check_auth(data):
                # clear cache
                self._clear_cache()

                # set updated
                self.set_last_update()

                # get last 5 rss cache results
                recent_results = self.provider.recent_results
                found_recent_results = 0  # A counter that keeps track of the number of items that have been found in cache

                cl = []
                index = 0
                for index, item in enumerate(data['entries'] or []):
                    if item['link'] in {cache_item['link'] for cache_item in recent_results}:
                        found_recent_results += 1

                    if found_recent_results >= self.provider.stop_at:
                        logger.log('Hit the old cached items, not parsing any more for: {0}'.format
                                   (self.provider_id), logger.DEBUG)
                        break
                    try:
                        ci = self._parse_item(item)
                        if ci is not None:
                            cl.append(ci)
                    except UnicodeDecodeError as e:
                        logger.log('Unicode decoding error, missed parsing item from provider {0}: {1!r}'.format
                                   (self.provider.name, e), logger.WARNING)

                cache_db_con = self._get_db()
                if cl:
                    cache_db_con.mass_action(cl)

                # finished processing, let's save the newest x (index) items and store these in cache with a max of 5
                # (overwritable per provider, throug hthe max_recent_items attribute.
                self.provider.recent_results = data['entries'][0:min(index, self.provider.max_recent_items)]

        except AuthException as e:
            logger.log('Authentication error: {0!r}'.format(e), logger.ERROR)

    def update_cache_manual_search(self, manual_data=None):
        """Update cache using manual search results."""
        # clear cache
        self._clear_cache()

        try:
            cl = []
            for item in manual_data:
                logger.log('Adding to cache item found in manual search: {0}'.format(item.name), logger.DEBUG)
                ci = self.add_cache_entry(item.name, item.url, item.seeders, item.leechers, item.size, item.pubdate)
                if ci is not None:
                    cl.append(ci)
        except Exception as e:
            logger.log('Error while adding to cache item found in manual seach for provider {0},'
                       ' skipping: {1!r}'.format(self.provider.name, e), logger.WARNING)

        results = []
        cache_db_con = self._get_db()
        if cl:
            logger.log('Mass updating cache table with manual results for provider: {0}'.
                       format(self.provider.name), logger.DEBUG)
            results = cache_db_con.mass_action(cl)

        return any(results)

    def get_rss_feed(self, url, params=None):
        """Get rss feed entries."""
        if self.provider.login():
            return getFeed(url, params=params, request_hook=self.provider.get_url)
        return {'entries': []}

    @staticmethod
    def _translate_title(title):
        """Sanitize title."""
        return '{0}'.format(title.replace(' ', '.'))

    @staticmethod
    def _translate_link_url(url):
        """Sanitize url."""
        return url.replace('&amp;', '&')

    def _parse_item(self, item):
        """Parse item to create cache entry."""
        title, url = self._get_title_and_url(item)
        seeders, leechers = self._get_result_info(item)
        size = self._get_size(item)
        pubdate = self._get_pubdate(item)

        self._check_item_auth(title, url)

        if title and url:
            title = self._translate_title(title)
            url = self._translate_link_url(url)

            # logger.log('Attempting to add item to cache: ' + title, logger.DEBUG)
            return self.add_cache_entry(title, url, seeders, leechers, size, pubdate)

        else:
            logger.log(
                'The data returned from the {0} feed is incomplete, this result is unusable'.format(self.provider.name),
                logger.DEBUG)

        return False

    def _get_last_update(self):
        """Get last provider update."""
        cache_db_con = self._get_db()
        sql_results = cache_db_con.select(b'SELECT time FROM lastUpdate WHERE provider = ?', [self.provider_id])

        if sql_results:
            last_time = int(sql_results[0][b'time'])
            if last_time > int(time.mktime(datetime.datetime.today().timetuple())):
                last_time = 0
        else:
            last_time = 0

        return datetime.datetime.fromtimestamp(last_time)

    def _get_last_search(self):
        """Get provider last search."""
        cache_db_con = self._get_db()
        sql_results = cache_db_con.select(b'SELECT time FROM lastSearch WHERE provider = ?', [self.provider_id])

        if sql_results:
            last_time = int(sql_results[0][b'time'])
            if last_time > int(time.mktime(datetime.datetime.today().timetuple())):
                last_time = 0
        else:
            last_time = 0

        return datetime.datetime.fromtimestamp(last_time)

    def set_last_update(self, to_date=None):
        """Set provider last update."""
        if not to_date:
            to_date = datetime.datetime.today()

        cache_db_con = self._get_db()
        cache_db_con.upsert(
            b'lastUpdate',
            {b'time': int(time.mktime(to_date.timetuple()))},
            {b'provider': self.provider_id}
        )

    def set_last_search(self, to_date=None):
        """Ser provider last search."""
        if not to_date:
            to_date = datetime.datetime.today()

        cache_db_con = self._get_db()
        cache_db_con.upsert(
            b'lastSearch',
            {b'time': int(time.mktime(to_date.timetuple()))},
            {b'provider': self.provider_id}
        )

    lastUpdate = property(_get_last_update)
    lastSearch = property(_get_last_search)

    def should_update(self):
        """Check if we should update provider cache."""
        # if we've updated recently then skip the update
        if datetime.datetime.today() - self.lastUpdate < datetime.timedelta(minutes=self.minTime):
            logger.log('Last update was too soon, using old cache: {0}. '
                       'Updated less then {1} minutes ago'.format(self.lastUpdate, self.minTime), logger.DEBUG)
            return False
        logger.log("Updating providers cache", logger.DEBUG)

        return True

    def should_clear_cache(self):
        """Check if we should clear cache."""
        # # if daily search hasn't used our previous results yet then don't clear the cache
        # if self.lastUpdate > self.lastSearch:
        #     return False
        return False

    def add_cache_entry(self, name, url, seeders, leechers, size, pubdate):
        """Add item into cache database."""
        try:
            parse_result = NameParser().parse(name)
        except (InvalidNameException, InvalidShowException) as error:
            logger.log('{0}'.format(error), logger.DEBUG)
            return None

        if not parse_result or not parse_result.series_name:
            return None

        # if we made it this far then lets add the parsed result to cache for usager later on
        season = parse_result.season_number if parse_result.season_number is not None else 1
        episodes = parse_result.episode_numbers

        if season is not None and episodes is not None:
            # store episodes as a seperated string
            episode_text = '|{0}|'.format('|'.join({str(episode) for episode in episodes if episode}))

            # get the current timestamp
            cur_timestamp = int(time.mktime(datetime.datetime.today().timetuple()))

            # get quality of release
            quality = parse_result.quality

            assert isinstance(name, text_type)

            # get release group
            release_group = parse_result.release_group

            # get version
            version = parse_result.version

            # Store proper_tags as proper1|proper2|proper3
            proper_tags = '|'.join(parse_result.proper_tags)

            logger.log('Added RSS item: [{0}] to cache: [{1}]'.format(name, self.provider_id), logger.DEBUG)

            return [
                b'INSERT OR REPLACE INTO [{provider_id}] '
                b'(name, season, episodes, indexerid, url, time, quality, release_group, '
                b'version, seeders, leechers, size, pubdate, proper_tags) '
                b'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)'.format(provider_id=self.provider_id),
                [name, season, episode_text, parse_result.show.indexerid, url, cur_timestamp, quality,
                 release_group, version, seeders, leechers, size, pubdate, proper_tags]]

    def search_cache(self, episode, forced_search=False, down_cur_quality=False):
        """Search cache for needed episodes."""
        needed_eps = self.find_needed_episodes(episode, forced_search, down_cur_quality)
        return needed_eps[episode] if episode in needed_eps else []

    def list_propers(self, date=None):
        """Method is currently not used anywhere.

        It can be usefull with some small modifications. First we'll need to flag the propers in db.
        Then this method can be used to retrieve those, and let the properFinder use results from cache,
        before moving on with hitting the providers.
        """
        cache_db_con = self._get_db()
        sql = b"SELECT * FROM [{provider_id}] WHERE proper_tags != ''".format(provider_id=self.provider_id)

        if date:
            sql += b' AND time >= {0}'.format(int(time.mktime(date.timetuple())))

        propers_results = cache_db_con.select(sql)
        return [x for x in propers_results if x[b'indexerid']]

    def find_needed_episodes(self, episode, forced_search=False, down_cur_quality=False):
        """Find needed episodes."""
        needed_eps = {}
        cl = []

        cache_db_con = self._get_db()
        if not episode:
            sql_results = cache_db_con.select(b'SELECT * FROM [{provider_id}]'.format(provider_id=self.provider_id))
        elif not isinstance(episode, list):
            sql_results = cache_db_con.select(
                b'SELECT * FROM [{provider_id}] WHERE indexerid = ? AND season = ? AND episodes LIKE ?'.format(provider_id=self.provider_id),
                [episode.show.indexerid, episode.season, b'%|{0}|%'.format(episode.episode)])
        else:
            for ep_obj in episode:
                cl.append([
                    b'SELECT * FROM [{0}] WHERE indexerid = ? AND season = ? AND episodes LIKE ? AND quality IN ({1})'.
                    format(self.provider_id, ','.join([str(x) for x in ep_obj.wanted_quality])),
                    [ep_obj.show.indexerid, ep_obj.season, b'%|{0}|%'.format(ep_obj.episode)]])

            if cl:
                # Only execute the query if we have results
                sql_results = cache_db_con.mass_action(cl, fetchall=True)
                sql_results = list(itertools.chain(*sql_results))
            else:
                sql_results = []
                logger.log("No cached results in {provider} for show '{show_name}' episode '{ep}'".format
                           (provider=self.provider_id, show_name=ep_obj.show.name,
                            ep=episode_num(ep_obj.season, ep_obj.episode)), logger.DEBUG)

        # for each cache entry
        for cur_result in sql_results:
            # ignored/required words, and non-tv junk
            if not show_name_helpers.filterBadReleases(cur_result[b'name']):
                continue

            # get the show object, or if it's not one of our shows then ignore it
            show_obj = Show.find(app.showList, int(cur_result[b'indexerid']))
            if not show_obj:
                continue

            # skip if provider is anime only and show is not anime
            if self.provider.anime_only and not show_obj.is_anime:
                logger.log('{0} is not an anime, skiping'.format(show_obj.name), logger.DEBUG)
                continue

            # get season and ep data (ignoring multi-eps for now)
            cur_season = int(cur_result[b'season'])
            if cur_season == -1:
                continue

            cur_ep = cur_result[b'episodes'].split('|')[1]
            if not cur_ep:
                continue

            cur_ep = int(cur_ep)

            cur_quality = int(cur_result[b'quality'])
            cur_release_group = cur_result[b'release_group']
            cur_version = cur_result[b'version']

            # if the show says we want that episode then add it to the list
            if not show_obj.want_episode(cur_season, cur_ep, cur_quality, forced_search, down_cur_quality):
                logger.log('Ignoring {0}'.format(cur_result[b'name']), logger.DEBUG)
                continue

            ep_obj = show_obj.get_episode(cur_season, cur_ep)

            # build a result object
            title = cur_result[b'name']
            url = cur_result[b'url']

            logger.log('Found result {0} at {1}'.format(title, url))

            result = self.provider.get_result([ep_obj])
            result.show = show_obj
            result.url = url
            result.seeders = cur_result[b'seeders']
            result.leechers = cur_result[b'leechers']
            result.size = cur_result[b'size']
            result.pubdate = cur_result[b'pubdate']
            result.proper_tags = cur_result[b'proper_tags']
            result.name = title
            result.quality = cur_quality
            result.release_group = cur_release_group
            result.version = cur_version
            result.content = None

            # add it to the list
            if ep_obj not in needed_eps:
                needed_eps[ep_obj] = [result]
            else:
                needed_eps[ep_obj].append(result)

        # datetime stamp this search so cache gets cleared
        self.set_last_search()

        return needed_eps
