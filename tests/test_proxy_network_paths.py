# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: Netresearch DTT GmbH

"""Proxy selection at each Jira network entry point, without network access."""

import importlib.util
import json
import sys
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

import pytest
import requests
from click.testing import CliRunner
from requests.utils import select_proxy

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/jira-communication/scripts"
sys.path.insert(0, str(SCRIPTS))

import lib.config as jira_config  # noqa: E402
from lib.client import get_jira_client  # noqa: E402
from lib.preview import preflight_render  # noqa: E402

JIRA_URL = "https://jira.example.com"
AMBIENT_PROXY = "http://127.0.0.1:7897"
FILE_PROXY = "http://127.0.0.1:10069"


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


attachment = _module("proxy_jira_attachment", SCRIPTS / "core/jira-attachment.py")
validate = _module("proxy_jira_validate", SCRIPTS / "core/jira-validate.py")
setup = _module("proxy_jira_setup", SCRIPTS / "core/jira-setup.py")
worklog = _module("proxy_jira_worklog", SCRIPTS / "utility/jira-worklog-query.py")
move = _module("proxy_jira_move", SCRIPTS / "workflow/jira-move.py")
oracle = _module("proxy_render_oracle", ROOT / "scripts/verify-render-oracle.py")
corpus = _module("proxy_render_corpus", ROOT / "scripts/generate-strikethrough-corpus.py")


@pytest.fixture(autouse=True)
def ambient_proxy(monkeypatch):
    for name in (
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("https_proxy", AMBIENT_PROXY)


@pytest.fixture
def transport(monkeypatch):
    calls = []
    statuses = {}

    def send(_adapter, request, **kwargs):
        calls.append((request.method, request.url, select_proxy(request.url, kwargs["proxies"])))
        path = urlparse(request.url).path
        if path == "/rest/api/2/issue/KEY-1":
            body = json.dumps(
                {"fields": {"attachment": [{"filename": "file.txt", "content": f"{JIRA_URL}/file.txt"}]}}
            ).encode()
        elif path == "/rest/api/2/issue/MOVE-1":
            issue_type = (
                "Bug" if sum(method == "GET" and "/issue/MOVE-1" in url for method, url, _ in calls) > 1 else "Task"
            )
            body = json.dumps(
                {
                    "fields": {
                        "summary": "Example",
                        "issuetype": {"name": issue_type},
                        "status": {"name": "Open"},
                        "project": {"key": "MOVE"},
                    }
                }
            ).encode()
        elif path == "/file.txt":
            body = b"file contents"
        elif path.endswith("/render"):
            body = b"<p>hello</p>"
        elif path.endswith("/myself"):
            body = b'{"name":"tester","displayName":"Tester"}'
        elif path.endswith("/attachments"):
            body = b'[{"id":"1"}]'
        else:
            body = b"{}"
        response = requests.Response()
        response.status_code = (
            302
            if path == "/file.txt" and statuses.get("redirect")
            else 204
            if request.method == "PUT" and path == "/rest/api/2/issue/MOVE-1"
            else statuses.get(request.method, 200)
        )
        response.url = request.url
        response.request = request
        response._content = body
        response._content_consumed = True
        response.headers["Content-Type"] = "text/html" if path.endswith("/render") else "application/json"
        if response.status_code == 302:
            response.headers["Location"] = "https://cdn.example.com/blob"
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return calls, statuses


def _env_file(tmp_path):
    path = tmp_path / ".env.jira"
    path.write_text(f"JIRA_URL={JIRA_URL}\nJIRA_PERSONAL_TOKEN=test-token\nJIRA_HTTPS_PROXY={FILE_PROXY}\n")
    return path


def test_client_session_get_post_put_use_file_proxy(tmp_path, transport):
    calls, _ = transport
    with mock.patch("lib.client.Jira") as jira_class:
        jira_class.return_value._session = requests.Session()
        client = get_jira_client(env_file=str(_env_file(tmp_path)))
    for method in ("get", "post", "put"):
        getattr(client._session, method)(f"{JIRA_URL}/rest/api/2/myself")
    assert [(method, proxy) for method, _, proxy in calls] == [
        ("GET", FILE_PROXY),
        ("POST", FILE_PROXY),
        ("PUT", FILE_PROXY),
    ]


def test_atlassian_client_request_uses_file_proxy(tmp_path, transport):
    calls, _ = transport
    client = get_jira_client(env_file=str(_env_file(tmp_path)))
    assert client.myself()["name"] == "tester"
    assert calls == [("GET", f"{JIRA_URL}/rest/api/2/myself", FILE_PROXY)]


def test_tempo_requests_use_client_proxy(tmp_path, transport):
    calls, _ = transport
    client = get_jira_client(env_file=str(_env_file(tmp_path)))
    assert worklog.detect_tempo(client)
    assert worklog._search_tempo_worklogs(client, {}) == []
    assert [(method, proxy) for method, _, proxy in calls] == [("GET", FILE_PROXY), ("POST", FILE_PROXY)]


def test_move_request_uses_client_proxy(tmp_path, transport):
    calls, _ = transport
    result = CliRunner().invoke(
        move.cli, ["--env-file", str(_env_file(tmp_path)), "issue", "MOVE-1", "MOVE", "--issue-type", "Bug"]
    )
    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    assert [(method, proxy) for method, _, proxy in calls] == [
        ("GET", FILE_PROXY),
        ("PUT", FILE_PROXY),
        ("GET", FILE_PROXY),
    ]


@pytest.mark.parametrize(
    "command", [["download", f"{JIRA_URL}/file.txt", "file.txt"], ["download-all", "KEY-1", "--dir", "files"]]
)
def test_attachment_download_requests_use_file_proxy(tmp_path, monkeypatch, transport, command):
    calls, _ = transport
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(attachment.cli, ["--env-file", str(_env_file(tmp_path)), *command])
    assert result.exit_code == 0, result.output
    assert calls and all(proxy == FILE_PROXY for _, _, proxy in calls)


def test_attachment_upload_uses_client_proxy(tmp_path, transport):
    calls, _ = transport
    upload = tmp_path / "file.txt"
    upload.write_text("file contents")
    result = CliRunner().invoke(attachment.cli, ["--env-file", str(_env_file(tmp_path)), "add", "KEY-1", str(upload)])
    assert result.exit_code == 0, result.output
    assert calls[-1][0] == "POST"
    assert calls[-1][2] == FILE_PROXY


def test_attachment_cdn_redirect_uses_environment_proxy(tmp_path, monkeypatch, transport):
    calls, statuses = transport
    statuses["redirect"] = True
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        attachment.cli, ["--env-file", str(_env_file(tmp_path)), "download", f"{JIRA_URL}/file.txt", "file.txt"]
    )
    assert result.exit_code == 0, result.output
    assert [(urlparse(url).hostname, proxy) for _, url, proxy in calls] == [
        ("jira.example.com", FILE_PROXY),
        ("cdn.example.com", AMBIENT_PROXY),
    ]


def test_preview_uses_file_proxy(tmp_path, transport):
    calls, _ = transport
    verdict = preflight_render("hello", env_file=str(_env_file(tmp_path)))
    assert verdict.available
    assert calls == [("POST", f"{JIRA_URL}/rest/api/1.0/render", FILE_PROXY)]


def test_validate_connectivity_uses_file_proxy(tmp_path, monkeypatch, transport):
    calls, _ = transport
    monkeypatch.setattr(validate, "LazyJiraClient", lambda **kwargs: mock.Mock(myself=lambda: {"name": "tester"}))
    config = jira_config.load_env(str(_env_file(tmp_path)))
    ok, _ = validate.check_connectivity(config, project=None, env_file=str(tmp_path / ".env.jira"))
    assert ok
    assert [(method, proxy) for method, _, proxy in calls] == [("HEAD", FILE_PROXY)]


def test_validate_all_profiles_uses_profile_proxy(tmp_path, monkeypatch, transport, capsys):
    calls, _ = transport
    profiles = tmp_path / "profiles.json"
    profiles.write_text(
        json.dumps(
            {
                "default": "work",
                "profiles": {"work": {"url": JIRA_URL, "token": "test-token", "https_proxy": FILE_PROXY}},
            }
        )
    )
    monkeypatch.setattr(jira_config, "PROFILES_FILE", profiles)
    assert validate.validate_all_profiles(output_json=True) == 0
    capsys.readouterr()
    assert [(method, proxy) for method, _, proxy in calls] == [("HEAD", FILE_PROXY)]


def test_setup_url_check_uses_environment_proxy(tmp_path, transport):
    calls, statuses = transport
    statuses["HEAD"] = 405
    ok, _ = setup.validate_url(JIRA_URL)
    assert ok
    assert [(method, proxy) for method, _, proxy in calls] == [("HEAD", AMBIENT_PROXY), ("GET", AMBIENT_PROXY)]


def test_setup_credentials_check_uses_environment_proxy(transport):
    calls, _ = transport
    ok, _ = setup.validate_credentials(JIRA_URL, "server", personal_token="test-token")
    assert ok
    assert calls and all(proxy == AMBIENT_PROXY for _, _, proxy in calls)


def test_corpus_renderer_uses_file_proxy(tmp_path, monkeypatch, transport, capsys):
    calls, _ = transport
    fixture = tmp_path / "corpus.json"
    fixture.write_text("{}")
    monkeypatch.setattr(corpus, "FIXTURE", fixture)
    monkeypatch.setattr(corpus, "build_cases", lambda: ["hello"])
    monkeypatch.setattr(
        sys,
        "argv",
        ["generate-strikethrough-corpus.py", "--live", "--env-file", str(_env_file(tmp_path)), "--workers", "1"],
    )
    assert corpus.main() == 0
    capsys.readouterr()
    assert calls == [("POST", f"{JIRA_URL}/rest/api/1.0/render", FILE_PROXY)]


def test_oracle_renderer_uses_file_proxy(tmp_path, monkeypatch, transport, capsys):
    calls, _ = transport
    fixture = tmp_path / "oracle.json"
    fixture.write_text(json.dumps({"cases": [{"markup": "hello", "rendered": "<p>hello</p>"}]}))
    monkeypatch.setattr(oracle, "FIXTURE", fixture)
    monkeypatch.setattr(sys, "argv", ["verify-render-oracle.py", "--live", "--env-file", str(_env_file(tmp_path))])
    assert oracle.main() == 0
    capsys.readouterr()
    assert calls == [("POST", f"{JIRA_URL}/rest/api/1.0/render", FILE_PROXY)]
