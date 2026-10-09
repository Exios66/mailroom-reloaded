"""Exporter identity checks using recorded runs and synthetic local datasets."""

import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from mailroom_reloaded.eval import runner
from mailroom_reloaded.eval.dataset import sha256_text


@pytest.fixture
def export_run(tmp_path, monkeypatch):
    """Evaluate a synthetic local dataset and provide its exporter arguments."""
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'jev_export_gate_features.py'
    spec = importlib.util.spec_from_file_location('export_under_test', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine('sqlite:///:memory:')
    monkeypatch.setattr(module.db, 'get_engine', lambda: engine)
    monkeypatch.setattr(runner, '_engine', lambda: engine)
    monkeypatch.setattr(runner.run_ledger, 'ledger_for', lambda _: None)
    monkeypatch.setattr(runner.run_ledger, 'open_run', lambda *a, **k: None)
    monkeypatch.setattr(runner.run_ledger, 'close_run', lambda *a, **k: None)
    source = tmp_path / 'dataset'
    source.mkdir()
    body = 'original text'
    (source / 'default.jsonl').write_text(json.dumps({
        'filename': 'a.txt', 'doc_text': body, 'content_sha256': sha256_text(body),
    }) + '\n')
    (source / 'ground_truth.jsonl').write_text(json.dumps({
        'filename': 'a.txt', 'expected': 'contract', 'retry_expected': True,
    }) + '\n')

    async def run_all(cfg, run_id, selected, gts, graded, db_engine):
        """Record synthetic gate features through the evaluation persistence path."""
        for doc in selected:
            row = runner._base_row(run_id, doc, gts[doc.filename], mode='pipeline', latency_s=0, graded=False)
            row['gate_features'] = json.dumps({'classify': {'confidence': 0.7}})
            runner._insert(db_engine, row)

    monkeypatch.setattr(runner, '_run_all', run_all)
    run_id = runner.run_eval(runner.EvalConfig(local_dir=source, split='test'))
    output = tmp_path / 'features.jsonl'
    args = ['--run-id', run_id, '--local-dir', str(source), '--split', 'test', '--out', str(output)]
    try:
        yield module, engine, source, output, args
    finally:
        engine.dispose()


def test_export_matching_run_preserves_split_and_label(export_run):
    """Export labels and the original split when dataset identities match."""
    module, engine, _, output, args = export_run
    assert module.main(args) == 0
    result = json.loads(output.read_text())
    assert result['split'] == 'test'
    assert result['retry_expected'] is True
    with engine.connect() as conn:
        assert conn.execute(text('SELECT content_sha256 FROM eval_docs')).scalar_one() == sha256_text('original text')


@pytest.mark.parametrize('option,value', [
    ('--dataset-repo', 'other/repo'), ('--config', 'fixtures'),
    ('--revision', 'other-revision'), ('--split', 'train'), ('--local-dir', '/different'),
])
def test_export_rejects_dataset_mismatch_before_loading(export_run, monkeypatch, capsys, option, value):
    """Reject any differing dataset selector before loading ground truth."""
    module, _, _, output, args = export_run
    monkeypatch.setattr(module, 'load_split', lambda *a, **k: pytest.fail('loaded mismatched dataset'))
    assert module.main([*args, option, value]) == 1
    assert 'mismatch' in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize('mutation', ['changed', 'missing', 'unrecorded_hash', 'unrecorded_dataset'])
def test_export_rejects_unverifiable_documents_without_overwriting(export_run, capsys, mutation):
    """Keep an existing export intact when content or provenance is unverifiable."""
    module, engine, source, output, args = export_run
    output.write_text('existing export')
    if mutation == 'changed':
        (source / 'default.jsonl').write_text(json.dumps({
            'filename': 'a.txt', 'doc_text': 'changed', 'content_sha256': sha256_text('changed'),
        }) + '\n')
    elif mutation == 'missing':
        (source / 'default.jsonl').write_text('')
        (source / 'ground_truth.jsonl').write_text('')
    else:
        with engine.begin() as conn:
            conn.execute(text('UPDATE eval_docs SET content_sha256 = NULL' if mutation == 'unrecorded_hash'
                              else 'DELETE FROM eval_runs'))
    assert module.main(args) == 1
    assert 'FATAL' in capsys.readouterr().err
    assert output.read_text() == 'existing export'


def test_export_rejects_hash_with_matching_doc_id_prefix(export_run):
    """Require the full content hash even when the shortened document ID matches."""
    module, engine, _, output, args = export_run
    with engine.begin() as conn:
        conn.execute(text('UPDATE eval_docs SET content_sha256 = :sha'),
                     {'sha': sha256_text('original text')[:16] + '0' * 48})
    assert module.main(args) == 1
    assert not output.exists()
