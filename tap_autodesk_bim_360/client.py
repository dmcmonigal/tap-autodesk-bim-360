import json
from datetime import datetime, timedelta

import backoff
import requests
import singer
from singer import metrics
from ratelimit import limits, sleep_and_retry, RateLimitException
from requests.exceptions import ConnectionError, Timeout

LOGGER = singer.get_logger()

class Server5xxError(Exception):
    pass

class BIM360Client(object):
    BASE_URL = 'https://developer.api.autodesk.com/hq/v1/accounts/{account_id}/'

    def __init__(self, config):
        self.__user_agent = config.get('user_agent')
        self.__session = requests.Session()
        self.__account_id = config.get('account_id')
        self.__client_id = config.get('client_id')
        self.__client_secret = config.get('client_secret')
        self.__refresh_token = config.get('refresh_token')

        self.__user_access_token = None
        self.__user_expires_at = None
        self.__app_access_token = None
        self.__app_expires_at = None

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.__session.close()

    def refresh_user_access_token(self):
        data = self.request(
            'POST',
            url='',
            auth=(self.__client_id, self.__client_secret),
            data={
                'refresh_token': self.__refresh_token,
                'grant_type': 'refresh_token'
            })

        self.__user_access_token = data['access_token']

        self.__user_expires_at = datetime.utcnow() + \
            timedelta(seconds=data['expires_in'] - 10) # pad by 10 seconds for clock drift

    def refresh_app_access_token(self):
        data = self.request(
            'POST',
            url='https://developer.api.autodesk.com/authentication/v1/authenticate',
            data={
                'client_id': self.__client_id,
                'client_secret': self.__client_secret,
                'grant_type': 'client_credentials',
                'scope': 'data:read account:read'
            })

        self.__app_access_token = data['access_token']

        self.__app_expires_at = datetime.utcnow() + \
            timedelta(seconds=data['expires_in'] - 10) # pad by 10 seconds for clock drift

    @backoff.on_exception(backoff.expo,
                          (Server5xxError,
                           RateLimitException,
                           ConnectionError,
                           Timeout),
                          max_tries=5,
                          factor=3)
    @sleep_and_retry
    @limits(calls=300, period=60)
    def request(self,
                method,
                auth='user',
                path=None,
                url=None,
                **kwargs):
        if url is None:
            if auth == 'user' and \
                (self.__user_access_token is None or \
                 self.__user_expires_at <= datetime.utcnow()):
                    self.refresh_user_access_token()
            elif auth == 'app' and \
                (self.__app_access_token is None or \
                 self.__app_expires_at <= datetime.utcnow()):
                    self.refresh_app_access_token()

        if url is None and path:
            url = '{}{}'.format(
                self.BASE_URL.format(account_id=self.__account_id),
                path)

        if 'endpoint' in kwargs:
            endpoint = kwargs['endpoint']
            del kwargs['endpoint']
        else:
            endpoint = None

        if 'headers' not in kwargs:
            kwargs['headers'] = {}

        if auth == 'user':
            access_token = self.__user_access_token
        elif auth == 'app':
            access_token = self.__app_access_token
        else:
            raise Exception('Auth mode "{}" not supported'.format(auth))

        kwargs['headers']['Authorization'] = 'Bearer {}'.format(access_token)

        if self.__user_agent:
            kwargs['headers']['User-Agent'] = self.__user_agent

        with metrics.http_request_timer(endpoint) as timer:
            response = self.__session.request(method, url, **kwargs)
            timer.tags[metrics.Tag.http_status_code] = response.status_code

        if response.status_code >= 500:
            raise Server5xxError()

        if response.status_code == 429:
            LOGGER.warn('Rate limit hit - 429')
            raise RateLimitException()

        response.raise_for_status()        

        return response.json()

    def get(self, path, **kwargs):
        return self.request('GET', path=path, **kwargs)
