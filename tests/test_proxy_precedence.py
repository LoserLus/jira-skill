# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: Netresearch DTT GmbH

"""Proxy precedence for Jira requests, without making network connections."""

import json
import os
import sys
from pathlib import Path
from unittest import mock

import requests
from requests.adapters import BaseAdapter
from requests.utils import select_proxy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/jira-communication/scripts"))

import lib.config as jira_config  # noqa: E402
from lib.client import get_jira_client  # noqa: E402

JIRA_URL = "https://jira.example.com"
AMBIENT_PROXY = "http://127.0.0.1:7897"
JIRA_HTTP_PROXY = "http://127.0.0.1:10068"
JIRA_HTTPS_PROXY = "http://127.0.0.1:10069"
PROXY_ENV_VARS = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
)


class RecordingAdapter(BaseAdapter):
    def __init__(self):
        self.selected_proxies = []

    def send(self, request, **kwargs):
        self.selected_proxies.append(select_proxy(request.url, kwargs["proxies"]))
        response = requests.Response()
        response.status_code = 200
        response.url = request.url
        response.request = request
        response.headers["Content-Type"] = "application/json"
        response._content = b"{}"
        return response

    def close(self):
        pass


def _clear_proxy_environment(monkeypatch):
    for name in PROXY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _env_file(tmp_path, extra=""):
    path = tmp_path / ".env.jira"
    path.write_text(f"JIRA_URL={JIRA_URL}\nJIRA_PERSONAL_TOKEN=test-token\n{extra}")
    return str(path)


def _client_with_recorder(**kwargs):
    with mock.patch("lib.client.Jira") as jira_class:
        jira_class.return_value._session = requests.Session()
        client = get_jira_client(**kwargs)
    adapter = RecordingAdapter()
    client._session.mount("http://", adapter)
    client._session.mount("https://", adapter)
    return client, adapter


def _selected_proxy(client, adapter, scheme="https"):
    client._session.get(f"{scheme}://jira.example.com/rest/api/2/myself")
    return adapter.selected_proxies[-1]


def test_env_jira_proxy_overrides_ambient_proxy(tmp_path, monkeypatch):
    _clear_proxy_environment(monkeypatch)
    for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, AMBIENT_PROXY)
    monkeypatch.setenv("NO_PROXY", "jira.example.com")
    env_file = _env_file(
        tmp_path,
        f"JIRA_HTTP_PROXY={JIRA_HTTP_PROXY}\nJIRA_HTTPS_PROXY={JIRA_HTTPS_PROXY}\n",
    )

    client, adapter = _client_with_recorder(env_file=env_file)

    assert _selected_proxy(client, adapter, "http") == JIRA_HTTP_PROXY
    assert _selected_proxy(client, adapter, "https") == JIRA_HTTPS_PROXY
    assert os.environ["HTTPS_PROXY"] == AMBIENT_PROXY


def test_profile_proxy_overrides_ambient_and_legacy_file(tmp_path, monkeypatch):
    _clear_proxy_environment(monkeypatch)
    monkeypatch.setenv("https_proxy", AMBIENT_PROXY)
    profiles_file = tmp_path / "profiles.json"
    profiles_file.write_text(
        json.dumps(
            {
                "default": "work",
                "profiles": {
                    "work": {
                        "url": JIRA_URL,
                        "auth": "pat",
                        "token": "test-token",
                        "https_proxy": JIRA_HTTPS_PROXY,
                    }
                },
            }
        )
    )
    monkeypatch.setattr(jira_config, "PROFILES_FILE", profiles_file)
    monkeypatch.setattr(
        jira_config,
        "DEFAULT_ENV_FILE",
        Path(_env_file(tmp_path, "JIRA_HTTPS_PROXY=http://127.0.0.1:10070\n")),
    )

    client, adapter = _client_with_recorder(profile="work")

    assert _selected_proxy(client, adapter) == JIRA_HTTPS_PROXY


def test_unconfigured_scheme_uses_ambient_proxy(tmp_path, monkeypatch):
    _clear_proxy_environment(monkeypatch)
    monkeypatch.setenv("http_proxy", AMBIENT_PROXY)
    monkeypatch.setenv("https_proxy", AMBIENT_PROXY)
    env_file = _env_file(tmp_path, f"JIRA_HTTPS_PROXY={JIRA_HTTPS_PROXY}\n")

    client, adapter = _client_with_recorder(env_file=env_file)

    assert _selected_proxy(client, adapter, "http") == AMBIENT_PROXY
    assert _selected_proxy(client, adapter, "https") == JIRA_HTTPS_PROXY


def test_no_jira_proxy_uses_requests_environment_proxy(tmp_path, monkeypatch):
    _clear_proxy_environment(monkeypatch)
    monkeypatch.setenv("https_proxy", AMBIENT_PROXY)
    client, adapter = _client_with_recorder(env_file=_env_file(tmp_path))

    assert _selected_proxy(client, adapter) == AMBIENT_PROXY


def test_no_jira_proxy_honors_environment_no_proxy(tmp_path, monkeypatch):
    _clear_proxy_environment(monkeypatch)
    monkeypatch.setenv("https_proxy", AMBIENT_PROXY)
    monkeypatch.setenv("NO_PROXY", "jira.example.com")
    client, adapter = _client_with_recorder(env_file=_env_file(tmp_path))

    assert _selected_proxy(client, adapter) is None


def test_no_configured_or_environment_proxy_connects_directly(tmp_path, monkeypatch):
    _clear_proxy_environment(monkeypatch)
    client, adapter = _client_with_recorder(env_file=_env_file(tmp_path))

    assert _selected_proxy(client, adapter) is None
