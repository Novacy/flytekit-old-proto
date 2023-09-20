from __future__ import annotations

import base64 as _base64
import hashlib as _hashlib
import http.server as _BaseHTTPServer
import logging
import multiprocessing
import os as _os
import re as _re
import typing
import urllib.parse as _urlparse
import webbrowser as _webbrowser
from dataclasses import dataclass
from http import HTTPStatus as _StatusCodes
from multiprocessing import get_context
from urllib.parse import urlencode as _urlencode

import requests as _requests

from .default_html import get_default_success_html
from .exceptions import AccessTokenNotFoundError
from .keyring import Credentials

_code_verifier_length = 64
_random_seed_length = 40
_utf_8 = "utf-8"


def _generate_code_verifier():
    """
    Generates a 'code_verifier' as described in https://tools.ietf.org/html/rfc7636#section-4.1
    Adapted from https://github.com/openstack/deb-python-oauth2client/blob/master/oauth2client/_pkce.py.
    :return str:
    """
    code_verifier = _base64.urlsafe_b64encode(_os.urandom(_code_verifier_length)).decode(_utf_8)
    # Eliminate invalid characters.
    code_verifier = _re.sub(r"[^a-zA-Z0-9_\-.~]+", "", code_verifier)
    if len(code_verifier) < 43:
        raise ValueError("Verifier too short. number of bytes must be > 30.")
    elif len(code_verifier) > 128:
        raise ValueError("Verifier too long. number of bytes must be < 97.")
    return code_verifier


def _generate_state_parameter():
    state = _base64.urlsafe_b64encode(_os.urandom(_random_seed_length)).decode(_utf_8)
    return _re.sub("[^a-zA-Z0-9-_.,]+", "", state)


def _create_code_challenge(code_verifier):
    """
    Adapted from https://github.com/openstack/deb-python-oauth2client/blob/master/oauth2client/_pkce.py.
    :param str code_verifier: represents a code verifier generated by generate_code_verifier()
    :return str: urlsafe base64-encoded sha256 hash digest
    """
    code_challenge = _hashlib.sha256(code_verifier.encode(_utf_8)).digest()
    code_challenge = _base64.urlsafe_b64encode(code_challenge).decode(_utf_8)
    # Eliminate invalid characters
    code_challenge = code_challenge.replace("=", "")
    return code_challenge


class AuthorizationCode(object):
    def __init__(self, code, state):
        self._code = code
        self._state = state

    @property
    def code(self):
        return self._code

    @property
    def state(self):
        return self._state


@dataclass
class EndpointMetadata(object):
    """
    This class can be used to control the rendering of the page on login successful or failure
    """

    endpoint: str
    success_html: typing.Optional[bytes] = None
    failure_html: typing.Optional[bytes] = None


class OAuthCallbackHandler(_BaseHTTPServer.BaseHTTPRequestHandler):
    """
    A simple wrapper around BaseHTTPServer.BaseHTTPRequestHandler that handles a callback URL that accepts an
    authorization token.
    """

    def do_GET(self):
        url = _urlparse.urlparse(self.path)
        if url.path.strip("/") == self.server.redirect_path.strip("/"):
            self.send_response(_StatusCodes.OK)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.handle_login(dict(_urlparse.parse_qsl(url.query)))
            if self.server.remote_metadata.success_html is None:
                self.wfile.write(bytes(get_default_success_html(self.server.remote_metadata.endpoint), "utf-8"))
            self.wfile.flush()
        else:
            self.send_response(_StatusCodes.NOT_FOUND)

    def handle_login(self, data: dict):
        self.server.handle_authorization_code(AuthorizationCode(data["code"], data["state"]))


class OAuthHTTPServer(_BaseHTTPServer.HTTPServer):
    """
    A simple wrapper around the BaseHTTPServer.HTTPServer implementation that binds an authorization_client for handling
    authorization code callbacks.
    """

    def __init__(
        self,
        server_address: typing.Tuple[str, int],
        remote_metadata: EndpointMetadata,
        request_handler_class: typing.Type[_BaseHTTPServer.BaseHTTPRequestHandler],
        bind_and_activate: bool = True,
        redirect_path: str = None,
        queue: multiprocessing.Queue = None,
    ):
        _BaseHTTPServer.HTTPServer.__init__(self, server_address, request_handler_class, bind_and_activate)
        self._redirect_path = redirect_path
        self._remote_metadata = remote_metadata
        self._auth_code = None
        self._queue = queue

    @property
    def redirect_path(self) -> str:
        return self._redirect_path

    @property
    def remote_metadata(self) -> EndpointMetadata:
        return self._remote_metadata

    def handle_authorization_code(self, auth_code: str):
        self._queue.put(auth_code)
        self.server_close()

    def handle_request(self, queue: multiprocessing.Queue = None) -> typing.Any:
        self._queue = queue
        return super().handle_request()


class _SingletonPerEndpoint(type):
    """
    A metaclass to create per endpoint singletons for AuthorizationClient objects
    """

    _instances: typing.Dict[str, AuthorizationClient] = {}

    def __call__(self, *args, **kwargs):
        endpoint = ""
        if args:
            endpoint = args[0]
        elif "auth_endpoint" in kwargs:
            endpoint = kwargs["auth_endpoint"]
        else:
            raise ValueError("parameter auth_endpoint is required")
        if endpoint not in self._instances:
            self._instances[endpoint] = super(
                _SingletonPerEndpoint, self
            ).__call__(*args, **kwargs)
        return self._instances[endpoint]


class AuthorizationClient(metaclass=_SingletonPerEndpoint):
    """
    Authorization client that stores the credentials in keyring and uses oauth2 standard flow to retrieve the
    credentials. NOTE: This will open an web browser to retreive the credentials.
    """

    def __init__(
        self,
        endpoint: str,
        auth_endpoint: str,
        token_endpoint: str,
        scopes: typing.Optional[typing.List[str]] = None,
        client_id: typing.Optional[str] = None,
        redirect_uri: typing.Optional[str] = None,
        endpoint_metadata: typing.Optional[EndpointMetadata] = None,
        verify: typing.Optional[typing.Union[bool, str]] = None,
    ):
        """
        Create new AuthorizationClient

        :param endpoint: str endpoint to connect to
        :param auth_endpoint: str endpoint where auth metadata can be found
        :param token_endpoint: str endpoint to retrieve token from
        :param scopes: list[str] oauth2 scopes
        :param client_id
        :param verify: (optional) Either a boolean, in which case it controls whether we verify
            the server's TLS certificate, or a string, in which case it must be a path
            to a CA bundle to use. Defaults to ``True``. When set to
            ``False``, requests will accept any TLS certificate presented by
            the server, and will ignore hostname mismatches and/or expired
            certificates, which will make your application vulnerable to
            man-in-the-middle (MitM) attacks. Setting verify to ``False``
            may be useful during local development or testing.
        """
        self._endpoint = endpoint
        self._auth_endpoint = auth_endpoint
        if endpoint_metadata is None:
            remote_url = _urlparse.urlparse(self._auth_endpoint)
            self._remote = EndpointMetadata(endpoint=remote_url.hostname)
        else:
            self._remote = endpoint_metadata
        self._token_endpoint = token_endpoint
        self._client_id = client_id
        self._scopes = scopes or []
        self._redirect_uri = redirect_uri
        self._code_verifier = _generate_code_verifier()
        code_challenge = _create_code_challenge(self._code_verifier)
        self._code_challenge = code_challenge
        state = _generate_state_parameter()
        self._state = state
        self._verify = verify
        self._headers = {"content-type": "application/x-www-form-urlencoded"}

        self._params = {
            "client_id": client_id,  # This must match the Client ID of the OAuth application.
            "response_type": "code",  # Indicates the authorization code grant
            "scope": " ".join(s.strip("' ") for s in self._scopes).strip(
                "[]'"
            ),  # ensures that the /token endpoint returns an ID and refresh token
            # callback location where the user-agent will be directed to.
            "redirect_uri": self._redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }

    def __repr__(self):
        return f"AuthorizationClient({self._auth_endpoint}, {self._token_endpoint}, {self._client_id}, {self._scopes}, {self._redirect_uri})"

    def _create_callback_server(self):
        server_url = _urlparse.urlparse(self._redirect_uri)
        server_address = (server_url.hostname, server_url.port)
        return OAuthHTTPServer(
            server_address,
            self._remote,
            OAuthCallbackHandler,
            redirect_path=server_url.path,
        )

    def _request_authorization_code(self):
        scheme, netloc, path, _, _, _ = _urlparse.urlparse(self._auth_endpoint)
        query = _urlencode(self._params)
        endpoint = _urlparse.urlunparse((scheme, netloc, path, None, query, None))
        logging.debug(f"Requesting authorization code through {endpoint}")
        _webbrowser.open_new_tab(endpoint)

    def _credentials_from_response(self, auth_token_resp) -> Credentials:
        """
        The auth_token_resp body is of the form:
        {
          "access_token": "foo",
          "refresh_token": "bar",
          "token_type": "Bearer"
        }
        """
        response_body = auth_token_resp.json()
        refresh_token = None
        if "access_token" not in response_body:
            raise ValueError('Expected "access_token" in response from oauth server')
        if "refresh_token" in response_body:
            refresh_token = response_body["refresh_token"]
        if "expires_in" in response_body:
            expires_in = response_body["expires_in"]
        access_token = response_body["access_token"]

        return Credentials(access_token, refresh_token, self._endpoint, expires_in=expires_in)

    def _request_access_token(self, auth_code) -> Credentials:
        if self._state != auth_code.state:
            raise ValueError(f"Unexpected state parameter [{auth_code.state}] passed")
        self._params.update(
            {
                "code": auth_code.code,
                "code_verifier": self._code_verifier,
                "grant_type": "authorization_code",
            }
        )

        resp = _requests.post(
            url=self._token_endpoint,
            data=self._params,
            headers=self._headers,
            allow_redirects=False,
            verify=self._verify,
        )
        if resp.status_code != _StatusCodes.OK:
            # TODO: handle expected (?) error cases:
            #  https://auth0.com/docs/flows/guides/device-auth/call-api-device-auth#token-responses
            raise Exception(
                f"Failed to request access token with response: [{resp.status_code}] {resp.content}"
            )
        return self._credentials_from_response(resp)

    def get_creds_from_remote(self) -> Credentials:
        """
        This is the entrypoint method. It will kickoff the full authentication flow and trigger a web-browser to
        retrieve credentials
        """
        # In the absence of globally-set token values, initiate the token request flow
        ctx = get_context("fork")
        q = ctx.Queue()

        # First prepare the callback server in the background
        server = self._create_callback_server()

        server_process = ctx.Process(target=server.handle_request, args=(q,))
        server_process.daemon = True

        try:
            server_process.start()

            # Send the call to request the authorization code in the background
            self._request_authorization_code()

            # Request the access token once the auth code has been received.
            auth_code = q.get()
            return self._request_access_token(auth_code)
        finally:
            server_process.terminate()

    def refresh_access_token(self, credentials: Credentials) -> Credentials:
        if credentials.refresh_token is None:
            raise ValueError("no refresh token available with which to refresh authorization credentials")

        resp = _requests.post(
            url=self._token_endpoint,
            data={
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "refresh_token": credentials.refresh_token,
            },
            headers=self._headers,
            allow_redirects=False,
            verify=self._verify,
        )
        if resp.status_code != _StatusCodes.OK:
            # In the absence of a successful response, assume the refresh token is expired. This should indicate
            # to the caller that the AuthorizationClient is defunct and a new one needs to be re-initialized.
            raise AccessTokenNotFoundError(f"Non-200 returned from refresh token endpoint {resp.status_code}")

        return self._credentials_from_response(resp)
