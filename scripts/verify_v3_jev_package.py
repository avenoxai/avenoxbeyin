#!/usr/bin/env python3
"""Installed-ZIP optionality proof, with a verified real 3.1.0 baseline. No provider I/O."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import zipfile

from verify_v3_release import verified, verify
from v3_package_helpers import isolated_env, run_python


def check(candidate, baseline):
    candidate, baseline = Path(candidate).resolve(), Path(baseline).resolve()
    new, old = verified(candidate), verified(baseline)
    if old['version'] != '3.1.0':
        raise ValueError('This regression requires the released 3.1.0 baseline')
    lifecycle = verify(candidate, baseline)  # Same bytes: install, update, rollback, interrupted recovery.
    checks = []
    with tempfile.TemporaryDirectory(prefix='beyin-jev-package-') as tmp:
        root = Path(tmp)
        for name, package in (('old', baseline), ('new', candidate)):
            with zipfile.ZipFile(package) as archive:
                archive.extractall(root / name)
        env = isolated_env(root / 'home')  # Deliberately no TypeSafe key or inherited provider environment.
        env['TYPESAFE_ENV_FILE'] = str(root / 'must-not-read.env')
        for saved in ('off', 'shadow', 'on'):
            vault = root / ('Installed ' + saved); vault.mkdir()
            state = root / ('state-' + saved)
            installed = run_python(root / 'old/scripts/install_v3.py', ['--vault', vault, '--state', state], root, env)
            assert installed.returncode == 0, installed.stderr
            def cli(*args, payload=None):
                response = run_python(vault / 'beyin.py', args, root, env, payload)
                assert response.returncode == 0, response.stderr
                return json.loads(response.stdout)
            cli('jev', saved, '--disable', 'auto_context')
            config = json.loads((state / 'jev.json').read_text(encoding='utf-8'))
            config.update(model='jev-1.13.0', rubric_version='installed-proof', timeout=1)
            (state / 'jev.json').write_text(json.dumps(config), encoding='utf-8')
            original = (state / 'jev.json').read_bytes()
            assert cli('update', '--package', candidate)['status'] == 'updated'
            assert (state / 'jev.json').read_bytes() == original
            status = cli('jev', 'status')
            assert status['mode'] == saved and not status['automatic_model_calls']
            cli('jev', 'off')
            disabled = (state / 'jev.json').read_bytes()
            status = cli('jev', 'status')
            assert status['mode'] == 'off' and not status['key_checked']
            # Exercise the ACTUAL newly installed command and Markdown synchronization.
            cli('note-create', '--file', '-', payload=dict(source='notes/proof.md',
                text='Quartz kararı kesinleşti.', metadata=dict(id='proof', kind='fact', project='demo',
                aliases=['proof-alias'], remote_allowed=False)))
            found = cli('context', 'proof-alias', '--project', 'demo')
            record = next(r for r in found['records'] if r['id'] == 'proof')
            assert record['remote_allowed'] is False
            proposal = dict(status='proposed', project='demo', claim='Quartz kararı kesinleşti.', evidence=[dict(
                record_id='proof', source_sha256=record['source_sha256'], quote='Quartz kararı kesinleşti.')])
            verdict = cli('jev-memory', '--project', 'demo', '--file', '-', payload=proposal)
            assert verdict['mechanical_verified'] and verdict['diagnostics'] == ['semantic_off']
            assert not any(verdict[k] for k in ('approved', 'memory_written', 'task_created', 'task_completed'))
            assert not (state / '.cache/jev').exists()
            assert cli('rollback')['status'] == 'rolled_back'
            assert (state / 'jev.json').read_bytes() == disabled
            assert cli('jev', 'status')['mode'] == 'off'
            assert cli('update', '--package', candidate)['status'] == 'updated'
            assert cli('jev', 'status')['mode'] == 'off'
            assert (state / 'jev.json').read_bytes() == disabled
            checks.append(dict(initial_mode=saved, mode_after_upgrade=saved, mode_after_disable_rollback_update='off',
                               advanced_config_preserved=True, installed_memory_command=True,
                               markdown_alias_policy_roundtrip=True, provider_cache_created=False))
        # The optional local Laya provider lives in a nested `laya` block. A released client
        # that predates it rejects the unknown key, so after a rollback the advisor is off
        # (config_invalid) instead of treating the file as TypeSafe and sending the key to a
        # local port. Nothing listens on the Laya port here and no provider call is made.
        vault = root / 'Installed laya'; vault.mkdir(); state = root / 'state-laya'
        laya_env = dict(env, TYPESAFE_API_KEY='MARKER-NOT-A-REAL-KEY')
        installed = run_python(root / 'old/scripts/install_v3.py', ['--vault', vault, '--state', state], root, laya_env)
        assert installed.returncode == 0, installed.stderr
        def cli(*args, payload=None):
            response = run_python(vault / 'beyin.py', args, root, laya_env, payload)
            assert response.returncode == 0, response.stderr
            return json.loads(response.stdout)
        assert cli('update', '--package', candidate)['status'] == 'updated'
        chosen = cli('jev', 'shadow', '--provider', 'laya', '--disable', 'auto_context')
        assert chosen['provider'] == 'laya' and chosen['config_valid'] and 'warning' not in chosen
        assert chosen['laya']['base_url'].startswith('http://127.0.0.1:') and chosen['laya']['model'] == 'multilingual'
        assert chosen['shadow_only'] is True and chosen['mode'] == 'shadow'
        configured = (state / 'jev.json').read_bytes()
        assert set(json.loads(configured)['laya']) == {'base_url', 'model'}
        # Laya is shadow-only: the installed command refuses `on` and leaves the file alone.
        refused = run_python(vault / 'beyin.py', ['jev', 'on'], root, laya_env)
        assert refused.returncode == 1 and json.loads(refused.stderr)['message'] == 'laya_shadow_only', refused.stderr
        assert (state / 'jev.json').read_bytes() == configured
        cli('note-create', '--file', '-', payload=dict(source='notes/laya-proof.md', text='Laya geri alma kaniti.',
            metadata=dict(id='laya-proof', kind='fact', project='demo', aliases=['laya-proof-alias'])))
        assert cli('rollback')['status'] == 'rolled_back'
        assert (state / 'jev.json').read_bytes() == configured
        old_status = cli('jev', 'status')
        assert old_status['config_valid'] is False and old_status['mode'] == 'off' and not old_status['automatic_model_calls']
        found = cli('context', 'laya-proof-alias', '--project', 'demo', '--jev')
        assert found['jev']['mode'] == 'off' and found['jev']['diagnostics'] == ['config_invalid']
        assert [r['id'] for r in found['records']] == ['laya-proof']
        assert not (state / '.cache/jev').exists() and not (state / 'jev-calls.jsonl').exists()
        assert cli('update', '--package', candidate)['status'] == 'updated'
        restored = cli('jev', 'status')
        assert restored['config_valid'] and restored['provider'] == 'laya' and restored['mode'] == 'shadow'
        back = cli('jev', 'off', '--provider', 'typesafe')
        assert back['provider'] == 'typesafe' and 'laya' in json.loads((state / 'jev.json').read_text(encoding='utf-8'))
        checks.append(dict(initial_mode='shadow', provider='laya', on_refused='laya_shadow_only', mode_after_rollback='off',
                           config_valid_after_rollback=False, provider_cache_created=False,
                           restored_after_update=True))
        clean = root / 'Clean'; clean.mkdir(); state = root / 'clean-state'
        installed = run_python(root / 'new/scripts/install_v3.py', ['--vault', clean, '--state', state], root, env)
        assert installed.returncode == 0, installed.stderr
        response = run_python(clean / 'beyin.py', ['jev', 'status'], root, env)
        assert response.returncode == 0, response.stderr
        status = json.loads(response.stdout)
        assert status['mode'] == 'off' and not status['key_checked'] and not status['automatic_model_calls']
        assert not (state / '.cache/jev').exists()
    return dict(status='passed', platform=sys.platform, python=sys.version.split()[0],
                baseline_version=old['version'], candidate_version=new['version'],
                baseline_sha256=hashlib.sha256(baseline.read_bytes()).hexdigest(),
                candidate_sha256=hashlib.sha256(candidate.read_bytes()).hexdigest(),
                lifecycle=lifecycle, saved_mode_checks=checks, clean_install_default='off',
                live_provider_calls=0, desktop_cold_session='not_run')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--baseline', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(check(args.package, args.baseline), indent=2))
