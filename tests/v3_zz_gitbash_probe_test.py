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
        python = sys.executable.replace('\\', '/')
        full_env = dict(os.environ, **self.env)
        noconv = dict(self.env, MSYS_NO_PATHCONV='1', MSYS2_ARG_CONV_EXCL='*')
        runs = []
        for name, command in commands.items():
            runs.append((name + ' | cmd shell=True', command, True, self.env))
            for bash in bashes:
                tag = name + ' | ' + ('usr-bin' if '\\usr\\' in bash else 'bin')
                runs += [(tag + ' -c', [bash, '-c', command], False, self.env),
                         (tag + ' -lc', [bash, '-lc', command], False, self.env),
                         (tag + ' -c full-env', [bash, '-c', command], False, full_env),
                         (tag + ' -lc full-env', [bash, '-lc', command], False, full_env),
                         (tag + ' -c noconv', [bash, '-c', command], False, noconv)]
        launcher = next(iter(commands.values())).split(' -NoProfile ')[0] if commands else 'powershell.exe'
        for bash in bashes:
            tag = 'diag | ' + ('usr-bin' if '\\usr\\' in bash else 'bin')
            stdin_python = '"' + python + '" -c "import sys; print(len(sys.stdin.buffer.read()))"'
            stdin_ps = launcher + " -NoProfile -NonInteractive -Command '[Console]::In.ReadToEnd().Length'"
            runs += [(tag + ' -c stdin-len-python', [bash, '-c', stdin_python], False, self.env),
                     (tag + ' -lc stdin-len-python', [bash, '-lc', stdin_python], False, self.env),
                     (tag + ' -c stdin-len-powershell', [bash, '-c', stdin_ps], False, self.env),
                     (tag + ' -lc stdin-len-powershell', [bash, '-lc', stdin_ps], False, self.env),
                     (tag + ' -c pwd', [bash, '-c', 'pwd; echo "flags=$-"'], False, self.env),
                     (tag + ' -lc pwd', [bash, '-lc', 'pwd; echo "flags=$-"'], False, self.env)]
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
