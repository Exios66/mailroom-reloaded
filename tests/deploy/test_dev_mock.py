"""The default dev mock speaks the real client's structured-call protocol."""

import importlib.util
import json
from pathlib import Path

from fastapi.testclient import TestClient
from openai import OpenAI

from mailroom_reloaded.agents.sorter import _sorter_response_format
from mailroom_reloaded.ingest.bert import Handoff, SortMode
from mailroom_reloaded.schemas.extraction import assess_payload, response_format


def test_mock_completion_compatible_with_openai_client():
    path = Path(__file__).resolve().parents[2] / 'deploy/mock_openai.py'
    spec = importlib.util.spec_from_file_location('dev_mock', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with TestClient(module.app) as http:
        assert http.get('/health').status_code == 200
        with OpenAI(
            base_url='http://testserver/v1', api_key='synthetic',
            http_client=http,
        ) as client:
            result = client.chat.completions.create(
                model='dev', messages=[{'role': 'user', 'content': 'synthetic letter'}],
                response_format=response_format('correspondence'),
            )
            assert result.choices[0].finish_reason == 'stop'
            assert assess_payload('correspondence', result.choices[0].message.content).schema_valid
            for mode in (SortMode.FULL, SortMode.SUBCLASS_ONLY):
                result = client.chat.completions.create(
                    model='dev', messages=[{'role': 'user', 'content': 'synthetic letter'}],
                    response_format=_sorter_response_format(
                        Handoff(mode, 'correspondence', '', 'test')
                    ),
                )
                parsed = json.loads(result.choices[0].message.content)
                assert parsed['doc_subclass'] == 'email'
                assert parsed['confidence'] > 0.95


def test_mock_archives_a_document(tmp_path, monkeypatch):
    from fakes.openai_server import FakeOpenAI

    from mailroom_reloaded.llm.tooling import reset_tool_support_cache
    from mailroom_reloaded.pipeline.flow import run_document
    from mailroom_reloaded.storage import db
    from mailroom_reloaded.storage.bins import Bins

    spec = importlib.util.spec_from_file_location(
        'dev_mock_pipeline', Path(__file__).resolve().parents[2] / 'deploy/mock_openai.py'
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = FakeOpenAI()
    server.app = module.app
    server.start()
    monkeypatch.setenv('MOCK_BASE_URL', server.base_url)
    monkeypatch.setenv('DEFAULT_PROVIDER', 'mock')
    monkeypatch.setenv('MAILROOM_BASE_DIR', str(tmp_path))
    monkeypatch.setattr(db, '_default_engine', None)
    reset_tool_support_cache()
    try:
        bins = Bins(tmp_path)
        document = bins.inbox / 'synthetic-letter.txt'
        document.write_text('Dear team, please reply by Friday about the deal.')
        state = run_document(document, worker_id='dev-mock-test')
        assert state.status == 'archived'
        assert list((bins.archive / 'correspondence').glob('*.txt'))
    finally:
        server.stop()
        if db._default_engine is not None:
            db._default_engine.dispose()
        reset_tool_support_cache()
