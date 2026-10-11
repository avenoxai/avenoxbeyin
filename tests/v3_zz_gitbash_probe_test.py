"""Temporary probe for #204: does the installed Windows hook command survive Git Bash? Not for merge."""
import json
import os
import shutil
import subprocess
import sys
import time
import unittest

import v3_hook_test as base


def say(*parts):
    sys.stderr.write('PROBE204| ' + ' '.join(str(p) for p in parts) + '\n')
    sys.stderr.flush()


class GitBashProbe(base.HookInstallerTest):
    @unittest.skipUnless(os.name == 'nt', 'Windows only')
    def test_zz_probe(self):
        self.install()
        self.seed()
        claude = json.loads((self.vault / '.claude/settings.local.json').read_text(encoding='utf-8'))
        codex = json.loads((self.vault / '.codex/hooks.json').read_text(encoding='utf-8'))
        commands = {}
        for name, data in (('claude', claude), ('codex', codex)):
            found = [hook['command'] for matcher in data['hooks']['SessionStart'] for hook in matcher.get('hooks', [])
                     if 'EncodedCommand' in str(hook.get('command', ''))]
            say(name, 'SessionStart encoded commands:', len(found))
            if found:
                commands[name] = found[0]
                say(name, 'command head:', found[0][:150])
                say(name, 'decoded:', base.decoded_command(found[0]))
        payload = json.dumps(dict(self.payload, hook_event_name='SessionStart', prompt='Nebula calibration'))
        say('payload length:', len(payload))
        bashes = [p for p in (r'C:\Program Files\Git\bin\bash.exe', r'C:\Program Files\Git\usr\bin\bash.exe')
                  if os.path.exists(p)]
        say('git bash candidates:', bashes, '| which bash:', shutil.which('bash'))
        say('PATHEXT on runner:', os.environ.get('PATHEXT'), '| COMSPEC:', os.environ.get('COMSPEC'))
        say('runner-only env names:', sorted(set(os.environ) - set(self.env)))
        full_env = dict(os.environ, **self.env)
        command = commands['claude']
        bash = bashes[0]

        def plus(*names):
            return dict(self.env, **{name: os.environ[name] for name in names if name in os.environ})

        def minus(*names):
            return {key: value for key, value in full_env.items() if key.upper() not in names}

        runs = [('round2 | isolated', [bash, '-c', command], False, self.env),
                ('round2 | isolated + PATHEXT', [bash, '-c', command], False, plus('PATHEXT')),
                ('round2 | isolated + COMSPEC', [bash, '-c', command], False, plus('COMSPEC')),
                ('round2 | isolated + PSModulePath', [bash, '-c', command], False, plus('PSModulePath')),
                ('round2 | isolated + SystemDrive + ProgramFiles + ProgramData', [bash, '-c', command], False,
                 plus('SystemDrive', 'ProgramFiles', 'ProgramData', 'ProgramFiles(x86)', 'ProgramW6432')),
                ('round2 | full - PATHEXT', [bash, '-c', command], False, minus('PATHEXT')),
                ('round2 | full - COMSPEC', [bash, '-c', command], False, minus('COMSPEC')),
                ('round2 | full', [bash, '-c', command], False, full_env)]
        for label, argv, shell, env in runs:
            started = time.time()
            try:
                result = subprocess.run(argv, shell=shell, input=payload, text=True, encoding='utf-8', errors='replace',
                                        capture_output=True, cwd=self.vault, env=env, timeout=90)
                out, err, rc = result.stdout or '', result.stderr or '', result.returncode
            except subprocess.TimeoutExpired as error:
                out, err, rc = str(error.stdout or ''), str(error.stderr or ''), 'TIMEOUT'
            try:
                context = 'context_ok=' + str('Synthetic Reviewer' in
                                              json.loads(out)['hookSpecificOutput']['additionalContext'])
            except Exception as error:
                context = 'context_parse=' + type(error).__name__
            say('RUN', label, '| rc', rc, '| sec', round(time.time() - started, 1), '| stdout_len', len(out), '|', context)
            say('    stdout:', repr(out[:240]))
            say('    stderr:', repr(err[:700]))
            errors = [str(p.relative_to(self.root)) + ' ' + p.read_text(encoding='utf-8', errors='replace')[:400]
                      for p in self.root.rglob('hook-error*.json')]
            if errors:
                say('    hook-error:', errors)


for _name in dir(base.HookInstallerTest):
    if _name.startswith('test_'):
        setattr(GitBashProbe, _name, None)
