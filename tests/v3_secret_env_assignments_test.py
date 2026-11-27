"""Prefixed environment assignments: synthetic credentials and offline transports only."""
import itertools
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_package_helpers import install, isolated_env, run_python

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3 as runtime
import beyin_v3_jev as advisor
import beyin_v3_jev_client as client
from beyin_v3_secrets import redact

VALUE = 'abcd1234efgh5678ijkl'
NAMES = ('TYPESAFE_API_KEY', 'OPENAI_API_KEY', 'MY_SECRET', 'DB_PASSWORD')
QUERY = 'how do we deploy the quartz site'

# The setx rule of 3.17.0, verbatim, used as the reference for setx and set -x
# input that holds a backtick.
_NAME_3_17_0 = (r"(?!(?:pad|eos|bos|unk|sep|cls|mask)_token(?a:(?![a-z0-9_])))"
                r"(?a:(?:[a-z_][a-z0-9_]*_|_)?)"
                r"(?:api[_ -]?key|access[_ -]?token|token|secret(?:[_ -]?access)?[_ -]?key"
                r"|private[_ -]?key|secret|password|passwd)")
SETX_3_17_0_VALUE = re.compile(
    r"""(?i)(?a:(?<![a-z0-9_-]))(?:(?:setx(?:[ \t]+/m)?|set[ \t]+-[a-z]{0,3}x[a-z]{0,3})"""
    r"""[ \t]+["\']?""" + _NAME_3_17_0 + r"""["\']?[ \t]+|setenvironmentvariable\([ \t]*["\']"""
    + _NAME_3_17_0 + r"""["\'][ \t]*,[ \t]*)"""
    r"""(?:\\{0,8}["\']\S{8,}\\{0,8}["\']|(?=[^\s,;]*\d)[^"\'\s,;][^\s,;]{7,})""")
# Generated from the 3.17.0 source of beyin_v3_secrets.redact: text, the
# synthetic value and the 3.17.0 count.
SETX_3_17_0 = (
    ('setx API_KEY `Xy7kL9amN2pQ4zR8 devam',
     'Xy7kL9amN2pQ4zR8', 1),
    ('setx API_KEY `Xy7kL9amN2pQ4zR8, sonra',
     'Xy7kL9amN2pQ4zR8', 1),
    ('setx API_KEY `Xy7kL9amN2pQ4zR8. Sonra',
     'Xy7kL9amN2pQ4zR8', 1),
    ("setx API_KEY `Xy7kL9amN2pQ4zR8'yi kullan",
     'Xy7kL9amN2pQ4zR8', 1),
    ('setx API_KEY `Xy7kL9amN2pQ4zR8"',
     'Xy7kL9amN2pQ4zR8', 1),
    ('setx API_KEY `Xy7kL9amN2pQ4zR8)',
     'Xy7kL9amN2pQ4zR8', 1),
    ('Komut: setx API_KEY `Qw3rTy7uIo9pAs1D ve terminali yeniden aç',
     'Qw3rTy7uIo9pAs1D', 1),
    ('set -x API_KEY `Xy7kL9amN2pQ4zR8 devam',
     'Xy7kL9amN2pQ4zR8', 1),
    ('set -gx GITHUB_TOKEN `Xy7kL9amN2pQ4zR8 devam',
     'Xy7kL9amN2pQ4zR8', 1),
    ('setx DB_PASSWORD `$Xy7kL9am2 /M',
     '`$Xy7kL9am2', 1),
    ('setx /m API_KEY `$Xy7kL9am2 /M',
     '`$Xy7kL9am2', 1),
    ('setx API_KEY `$Xy7kL9am2 && echo ok',
     '`$Xy7kL9am2', 1),
    ('setx API_KEY `$ab`$Xy7kL9am2',
     '`$ab`$Xy7kL9am2', 1),
    ('setx API_KEY `"Xy7kL9amN2pQ4zR8`"',
     '`"Xy7kL9amN2pQ4zR8`"', 1),
    ('setx API_KEY `"Xy7kL9amN2pQ4zR8`" /M',
     '`"Xy7kL9amN2pQ4zR8`"', 1),
    ('setx API_KEY Xy7kL9amN2pQ4zR8`$Ab12 /M',
     'Xy7kL9amN2pQ4zR8`$Ab12', 1),
    ('setx API_KEY `#Xy7kL9amN2pQ4zR8 | Out-Null',
     '`#Xy7kL9amN2pQ4zR8', 1),
    ('PS> setx DB_PASSWORD `0Xy7kL9amN2pQ4zR8 2>$null',
     '`0Xy7kL9amN2pQ4zR8', 1),
    ('[Environment]::SetEnvironmentVariable("API_KEY", `$Xy7kL9am2, "User")',
     '`$Xy7kL9am2', 1),
    ('setx API_KEY ```Xy7kL9amN2pQ4zR8` devam',
     'Xy7kL9amN2pQ4zR8', 1),
    ('setx API_KEY ``Xy7kL9amN2pQ4zR8` devam',
     'Xy7kL9amN2pQ4zR8', 1),
    ('setx API_KEY ````Xy7kL9amN2pQ4zR8`` x',
     'Xy7kL9amN2pQ4zR8', 1),
    ('setx API_KEY `kisa`mN2pQ4zR8 /M',
     'kisa`mN2pQ4zR8', 1),
    ('set -x API_KEY `kisa`mN2pQ4zR8',
     'kisa`mN2pQ4zR8', 1),
    ('setx API_KEY `Xy7kL9`amN2pQ4zR8',
     'Xy7kL9`amN2pQ4zR8', 1),
    ('setx API_KEY `````````````````Xy7kL9amN2pQ4zR8`````````````````',
     'Xy7kL9amN2pQ4zR8', 1),
    ('setx API_KEY Xy7kL9a` x',
     'Xy7kL9a', 1),
    ('setx API_KEY Ab1cde```` x',
     'Ab1cde', 1),
    ('setx API_KEY Xy7``kL9amN2 x',
     'Xy7``kL9amN2', 1),
    ("setx API_KEY Xy7kL9amN2`'abcdefgh",
     "Xy7kL9amN2`'abcdefgh", 1),
    ('setx API_KEY Xy7kL9am`N2pQ4zR8',
     'Xy7kL9am`N2pQ4zR8', 1),
    ('setx API_KEY `Xy7kL9am,N2pQ4 x',
     'Xy7kL9am,N2pQ4', 1),
    ('setx API_KEY `Xy7kL9am,N2pQ4zR8`',
     'Xy7kL9am,N2pQ4zR8', 1),
    ('set -x API_KEY `Xy7kL9amN2pQ4zR8\nsonra',
     'Xy7kL9amN2pQ4zR8', 1),
    ('`setx API_KEY Xy7kL9amN2pQ4zR8`',
     'Xy7kL9amN2pQ4zR8', 1),
    ('`setx API_KEY Xy7kL9amN2pQ4zR8` komutu',
     'Xy7kL9amN2pQ4zR8', 1),
    ("`setx API_KEY Xy7kL9amN2pQ4zR8`'yi çalıştır",
     'Xy7kL9amN2pQ4zR8', 1),
    ('`set -x API_KEY Xy7kL9amN2pQ4zR8`.',
     'Xy7kL9amN2pQ4zR8', 1),
)


class SecretEnvAssignmentsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='beyin-secret-env-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.state = self.root / 'state'

    def test_prefixed_assignments_preserve_key_and_value_syntax(self):
        for name in (*NAMES, 'SERVICE_V2_ACCESS_TOKEN', 'CI_TOKEN', 'DB_PASSWD',
                     '_API_KEY', '_SERVICE_API_KEY', 'my_service_api_key'):
            for before, assignment, after in (
                    ('', f'{name}={VALUE}', ''),
                    ('export ', f'{name}="{VALUE}"', ''),
                    ('export ', f"{name}='{VALUE}'", ''),
                    ('before\n', f'{name} = {VALUE}', '\nafter'),
                    ('{', f'"{name}": "{VALUE}"', '}'),
                    ('before ', f"'{name}': '{VALUE}'", ' after')):
                with self.subTest(name=name, assignment=assignment):
                    text = before + assignment + after
                    self.assertEqual(redact(text, self.state), (before + '[REDACTED]' + after, 1))

    def test_public_config_and_nonsecret_identifiers_are_unchanged(self):
        for name in ('PUBLIC_API_KEY_NAME', 'API_KEY_COUNT', 'MY_SECRET_NAME',
                     'DB_PASSWORD_FILE', 'OPENAI_API_KEY_ID', 'TOKENIZER_MODEL',
                     'API_KEYBOARD', 'MYSECRET', 'DBPASSWORD', 'PUBLIC_KEY',
                     'SERVICE_URL', '123_API_KEY'):
            text = f'{name}={VALUE}'
            with self.subTest(name=name):
                self.assertEqual(redact(text, self.state), (text, 0))
        for text in ('TYPESAFE_API_KEY is the variable name.',
                     'OPENAI_API_KEY=short', 'MY_SECRET="short"',
                     'export DB_PASSWORD=""', 'PUBLIC_API_URL=https://example.com/docs',
                     'npm_config_registry=https://registry.npmjs.org/'):
            with self.subTest(text=text):
                self.assertEqual(redact(text, self.state), (text, 0))

    def test_existing_unprefixed_key_forms_still_redact(self):
        for name in ('api_key', 'api-key', 'API KEY', 'access_token', 'token',
                     'secret', 'password', 'passwd'):
            with self.subTest(name=name):
                self.assertEqual(redact(name + '=' + VALUE, self.state), ('[REDACTED]', 1))

    def test_markdown_assignment_label_and_provider_formats(self):
        self.assertEqual(redact("Use `SECRET_KEY=` in the example.", self.state),
                         ("Use `SECRET_KEY=` in the example.", 0))
        # Backtick-quoted secret values must be redacted without swallowing trailing prose (#907).
        self.assertEqual(redact(f"API_KEY=`{VALUE}`", self.state), ("[REDACTED]", 1))
        self.assertEqual(redact(f"API_KEY: `{VALUE}` sonra", self.state), ("[REDACTED] sonra", 1))
        self.assertEqual(redact("token: `kisa`", self.state), ("token: `kisa`", 0))
        self.assertEqual(redact("secret = `abc def ghi jkl`", self.state), ("secret = `abc def ghi jkl`", 0))
        for text in (f"APP_API_KEY={VALUE}", f"SECRET_KEY={VALUE}",
                     f"Authorization: Token {VALUE}", f"glpat-{VALUE}"):
            with self.subTest(format=text.split()[0]):
                self.assertEqual(redact(text, self.state), ("[REDACTED]", 1))

    def test_backtick_quoted_values_are_masked_whole_and_keep_markdown(self):
        # 3.16.0 stopped the unquoted value at a backtick, which left NAME=`value`
        # visible (#907); each case below keeps all of VALUE out of the result.
        for text, expected in (
                (f"export API_KEY=`{VALUE}`", "export [REDACTED]"),
                (f"```bash\nexport OPENAI_API_KEY=`{VALUE}`\n```", "```bash\nexport [REDACTED]\n```"),
                (f"API_KEY=`{VALUE}`.", "[REDACTED]."),
                (f"PASSWORD: `{VALUE}`; next", "[REDACTED]; next"),
                (f"secret=`{VALUE}`)", "[REDACTED])"),
                (f"API_KEY=`\"{VALUE}\"`", "[REDACTED]"),
                (f"API_KEY=``{VALUE}``", "[REDACTED]"),
                (f"- `OPENAI_API_KEY=`{VALUE}``", "- `[REDACTED]`"),
                (f"`API_KEY={VALUE}`", "`[REDACTED]`"),
                (f"API_KEY=`{VALUE}", "[REDACTED]"),
                (f"API_KEY=`{VALUE}\r\nsonra`", "[REDACTED]\r\nsonra`"),
                (f"API_KEY=`{VALUE}` ve TOKEN=`{VALUE}x`", "[REDACTED] ve [REDACTED]")):
            with self.subTest(text=text):
                result, count = redact(text, self.state)
                self.assertEqual(result, expected)
                self.assertGreaterEqual(count, 1)
                self.assertNotIn(VALUE[:8], result)
        # Variable names, short or spaced inline code and non-secret names stay text.
        for text in ("Use `SECRET_KEY` or `API_KEY=` in the example.",
                     "`SECRET_KEY=`\n`NEXT_SETTING_NAME`",
                     "Set `API_KEY=`, `TOKEN=` ve `SECRET=` adlari.",
                     "TOKEN: `npm run build`", "password: `correct horse battery staple`",
                     "TOKEN=`config.secrets.token --flag`", "`KEY=${VAR}`",
                     f"KEY=`{VALUE}`", "TOKEN=```\nnot_a_secret_here\n```"):
            with self.subTest(text=text):
                self.assertEqual(redact(text, self.state), (text, 0))

    def test_double_backtick_value_holding_a_backtick_is_masked_whole(self):
        # Markdown writes a value that holds a backtick as ``value``; the branch
        # closed on the inner backtick, reported a mask and left the tail visible.
        store = runtime.MemoryStore(self.state, self.vault)
        inner = 'Xy7#kL9a`mN2$pQ4!'
        for text, expected in (
                (f"DB_PASSWORD=``{inner}``", "[REDACTED]"),
                (f"API_KEY=``abcdefgh`ijklmnop``", "[REDACTED]"),
                (f"export DB_PASSWORD=``{inner}`` ve devam", "export [REDACTED] ve devam"),
                (f"password: ``{inner}``.", "[REDACTED]."),
                (f"- `OPENAI_API_KEY=``{inner}```", "- `[REDACTED]`"),
                (f"API_KEY=``{inner}", "[REDACTED]"),
                (f"API_KEY=``{inner}`", "[REDACTED]"),
                (f"API_KEY=``{inner}\r\nsonra``", "[REDACTED]\r\nsonra``"),
                (f"API_KEY=``a`bcdefgh`` ve TOKEN=``{inner}``", "[REDACTED] ve [REDACTED]")):
            with self.subTest(text=text):
                result, count = redact(text, self.state)
                self.assertEqual(result, expected)
                self.assertGreaterEqual(count, 1)
                for part in ('Xy7#kL9a', 'mN2$pQ4!', 'ijklmnop', 'bcdefgh'):
                    self.assertNotIn(part, result)
                with self.assertRaises(ValueError):
                    advisor._safe(store, {'statement': text})
        # A backtick that a space follows ends a spaced span, which stays prose
        # rather than masking only its first word; variable names stay text.
        for text in ("API_KEY=``abcdefgh` ijk``", "API_KEY=`` Xy7#kL9a`mN2 ``",
                     "Token: ``GITHUB_TOKEN``"):
            with self.subTest(text=text):
                self.assertEqual(redact(text, self.state), (text, 0))

    def test_fenced_block_after_a_secret_label_stays_text_and_passes_jev(self):
        # The separator crosses newlines, so a label line followed by a code fence
        # reached the backtick branch; an unclosed run of three backticks took the
        # info string as a value and dropped the label and the opening fence.
        store = runtime.MemoryStore(self.state, self.vault)
        for text in ("## API key:\n```javascript\nconst x = 1\n```",
                     "Access token:\n```typescript\nconst t = process.env.TOKEN\n```\nSonraki paragraf",
                     "Password:\n```sh-session\n$ ls\n```", "Token: ```javascript",
                     "Secret key:\r\n````markdown\r\n```js\r\nx\r\n```\r\n````"):
            with self.subTest(text=text):
                self.assertEqual(redact(text, self.state), (text, 0))
                advisor._safe(store, {'statement': text})
        # A closed triple-backtick value is still masked whole.
        self.assertEqual(redact(f"API_KEY=```{VALUE}```", self.state), ("[REDACTED]", 1))

    def test_longer_backtick_span_holding_backticks_is_masked_whole(self):
        # Markdown writes a value that holds `` with a triple span (```a``b```).
        # The three-or-more branch closed on the first inner backtick, reported
        # a mask and left the tail in the note and in memory.sqlite3.
        store = runtime.MemoryStore(self.state, self.vault)
        for text, expected in (
                ("API_KEY=```Xy7#kL9a`mN2$pQ4!```", "[REDACTED]"),
                ("API_KEY=```Xy7#kL9a``mN2$pQ4!``` ve", "[REDACTED] ve"),
                ("API_KEY=````Xy7#kL9a```mN2$pQ4!````", "[REDACTED]"),
                ("API key: ```Xy7#kL9a`mN2$pQ4!``` kullan", "[REDACTED] kullan"),
                ("API_KEY=```Xy7#kL9a`mN2$pQ4!`", "[REDACTED]"),
                # A single span that a backtick interrupts, or a closing run
                # glued to more text, goes whole instead of leaving its tail.
                ("API_KEY=`Xy7#kL9a`mN2$pQ4!` sonra", "[REDACTED] sonra"),
                ("API_KEY=`Xy7#kL9a`mN2$pQ4! sonra", "[REDACTED] sonra"),
                ("API_KEY=``Xy7#kL9a``mN2$pQ4!``", "[REDACTED]"),
                ("password: `Xy7#kL9amN2$pQ4!`'yi sakla", "[REDACTED] sakla")):
            with self.subTest(text=text):
                result, count = redact(text, self.state)
                self.assertEqual((result, count), (expected, 1))
                self.assertNotIn('mN2$pQ4!', result)
                with self.assertRaises(ValueError):
                    advisor._safe(store, {'statement': text})
        # A spaced span stays prose instead of losing only its first word.
        for text in ("API_KEY=```abcdefgh` ijk```", "API_KEY=````abcdefgh``` ijk````",
                     "Token: ```GITHUB_TOKEN```"):
            with self.subTest(text=text):
                self.assertEqual(redact(text, self.state), (text, 0))
                advisor._safe(store, {'statement': text})

    def test_backtick_spans_are_masked_whole_or_left_as_written(self):
        # Class check over a fixed grid rather than single examples: every
        # opening run of 1-6 backticks, a value holding 0-3 backticks in a row,
        # a closing run of 0-6 backticks and the text after it. The result is
        # the input unchanged (and Jev accepts it) or no 6-character piece of
        # the value is left (and Jev rejects it); never a mask with a tail.
        store = runtime.MemoryStore(self.state, self.vault)
        values = (('Xy7#kL9a', 'mN2$pQ4!'), ('Xy7#k', 'L9amN2$pQ4!'),
                  ('Xy7#kL9amN2', '$pQ4!z'), ('Xy7#​kL9a', 'mN2$pQ4!'), ('kisa', ''))
        afters = ('', ' ve devam', '.', ', sonra', "'yi kullan", 'değerini',
                  '\nsonraki satır', '\r\nsonraki satır', ' `kod` sonra', '\nsonra',
                  '\r\nsonra', '” dedi', "'lerindekilerden geç", '…')
        failures, outcomes = [], {'masked': 0, 'kept': 0}
        for prefix, opening, inner, (head, tail), closing, after in itertools.product(
                ('API_KEY=', 'password: ', '- `OPENAI_API_KEY='), range(1, 7), range(4),
                values, range(7), afters):
            value = head + '`' * inner + tail
            text = prefix + '`' * opening + value + '`' * closing + after
            result, count = redact(text, self.state)
            try:
                advisor._safe(store, {'statement': text})
                accepted = True
            except ValueError:
                accepted = False
            if result == text:
                outcomes['kept'] += 1
                ok = count == 0 and accepted
            else:
                outcomes['masked'] += 1
                pieces = {plain[i:i + 6] for plain in (value, value.replace('​', ''))
                          for i in range(len(plain) - 5)}
                ok = (count >= 1 and '[REDACTED]' in result and not accepted
                      and not any(piece in result for piece in pieces))
            if not ok:
                failures.append((text, result, count, accepted))
        self.assertEqual(failures[:10], [])
        self.assertEqual(sum(outcomes.values()), 3 * 6 * 4 * 5 * 7 * 14)
        self.assertGreater(outcomes['masked'], 0)
        self.assertGreater(outcomes['kept'], 0)

    def test_unquoted_value_holding_a_backtick_is_masked_whole(self):
        # The unquoted branch stopped at the first backtick, reported a mask and
        # left the tail of the value in the note (on main since 3.16.0); a setx
        # span split at a comma the same way.
        store = runtime.MemoryStore(self.state, self.vault)
        for text, expected in (
                ("DB_PASSWORD=Xy7#kL9a`mN2$pQ4!", "[REDACTED]"),
                ("export DB_PASSWORD=Xy7#kL9a``mN2$pQ4! ve devam", "export [REDACTED] ve devam"),
                ("- `OPENAI_API_KEY=Xy7#kL9a`mN2$pQ4!`", "- `[REDACTED]`"),
                ("API_KEY: Xy7#kL9a`'mN2$pQ4! sonra", "[REDACTED] sonra"),
                ("setx API_KEY `Xy7kL9am,N2pQ4zR8`", "[REDACTED]"),
                ("setx API_KEY `Xy7kL9am,N2pQ4zR8`, sonra", "[REDACTED], sonra"),
                ("setx API_KEY Xy7kL9am`N2pQ4zR8", "[REDACTED]"),
                ("DB_PASSWORD=`Xy7#kL9a,mN2$pQ4!`", "[REDACTED]")):
            with self.subTest(text=text):
                result, count = redact(text, self.state)
                self.assertEqual((result, count), (expected, 1))
                for part in ('mN2$pQ4', 'N2pQ4zR8'):
                    self.assertNotIn(part, result)
                with self.assertRaises(ValueError):
                    advisor._safe(store, {'statement': text})
        # A backtick run that a space, the end, trailing punctuation or a Turkish
        # suffix follows closes the inline code around the assignment and stays
        # outside, as in 3.17.0.
        for text, expected in (
                (f"- `OPENAI_API_KEY={VALUE}`", "- `[REDACTED]`"),
                (f"`API_KEY={VALUE}`, `TOKEN=x`", "`[REDACTED]`, `TOKEN=x`"),
                ("`API_KEY=abcdefghij`'yi ayarla", "`[REDACTED]`'yi ayarla"),
                ("`API_KEY=abcdefghij`’nin değeri", "`[REDACTED]`’nin değeri"),
                (f"`setx API_KEY {VALUE}` komutu", "`[REDACTED]` komutu")):
            with self.subTest(text=text):
                self.assertEqual(redact(text, self.state), (expected, 1))

    def test_long_suffix_and_typographic_marks_after_inline_code_match_3_17_0(self):
        # A Turkish suffix longer than eight letters, or a typographic quote,
        # ellipsis or dash after the closing backtick, read as part of the
        # value: the outer backtick and the prose went into the mask. The
        # expected text is the 3.17.0 output.
        for text, expected in (
                ("`API_KEY=Xy7kL9amN2pQ`'larındaki satır", "`[REDACTED]`'larındaki satır"),
                ("- `SECRET_KEY=Xy7kL9amN2pQ`'ındakileri sil", "- `[REDACTED]`'ındakileri sil"),
                ("`API_KEY=abcdefghij`'ndakilerden geç", "`[REDACTED]`'ndakilerden geç"),
                ("`API_KEY=abcdefghij`'lerindeki ayarla", "`[REDACTED]`'lerindeki ayarla"),
                ('- `OPENAI_API_KEY=Xy7kL9amN2pQ`’larınızdan.', '- `[REDACTED]`’larınızdan.'),
                ("`API_KEY=abcdefghij`'lerinizdekilerden geç", "`[REDACTED]`'lerinizdekilerden geç"),
                ('“`API_KEY=abcdefghij`” dedi', '“`[REDACTED]`” dedi'),
                ('`API_KEY=Xy7kL9amN2pQ`’', '`[REDACTED]`’'),
                ("`API_KEY=Xy7kL9amN2pQ`'yi…", "`[REDACTED]`'yi…"),
                ("`API_KEY=Xy7kL9amN2pQ`'yi—", "`[REDACTED]`'yi—"),
                ('`API_KEY=Xy7kL9amN2pQ`’yi”', '`[REDACTED]`’yi”'),
                ('`API_KEY=Xy7kL9amN2pQ`» dedi', '`[REDACTED]`» dedi'),
                ('`API_KEY=Xy7kL9amN2pQ`… sonra', '`[REDACTED]`… sonra'),
                ('‘`TOKEN=Xy7kL9amN2pQ`’ yaz', '‘`[REDACTED]`’ yaz')):
            with self.subTest(text=text):
                self.assertEqual(redact(text, self.state), (expected, 1))
        # A value opened by backticks keeps the same marks outside the mask.
        self.assertEqual(redact('API_KEY=`Xy7kL9amN2pQ`” dedi', self.state), ('[REDACTED]” dedi', 1))

    def test_short_span_before_a_line_break_stays_text_and_passes_jev(self):
        # Jev scans the JSON form, where a line break is the two characters \n.
        # A short span read through its closing backtick reached the end of the
        # JSON string, so Jev refused a note that redact() kept as written.
        store = runtime.MemoryStore(self.state, self.vault)
        for text in ('token: `kisa`\ndevam', 'Password: `abc`\nok', 'API key: `.env`\nbitti',
                     'Not: secret: `yok`\n#etiket', 'token: `kisa`\r\n`kod`',
                     'Token: `abc`\n`Xy7kL9amN2`', 'API key: ```js\nconst x=1\n```',
                     'API key: ``ab`c``\nsonra'):
            with self.subTest(text=text):
                self.assertEqual(redact(text, self.state), (text, 0))
                advisor._safe(store, {'statement': text})
        # A long value before the line break is still masked and refused.
        for text, expected in (('token: `Xy7kL9amN2`\ndevam', '[REDACTED]\ndevam'),
                               ('token: `Xy7kL9amN2\ndevam', '[REDACTED]\ndevam')):
            with self.subTest(text=text):
                self.assertEqual(redact(text, self.state), (expected, 1))
                with self.assertRaises(ValueError):
                    advisor._safe(store, {'statement': text})

    def test_comma_semicolon_and_closing_quote_end_a_value_as_in_3_17_0(self):
        # A comma or a semicolon ends an unquoted value and a closing quote ends
        # a quoted one, so the next setting in compact JSON, a connection string
        # or a key=value list stays readable. A value holding one of these
        # characters has to be quoted or put in backticks (known limit).
        for text, expected in (
                ('{"password":"abcdefghij","host":"db.example.com","port":5432}',
                 '{[REDACTED]:5432}'),
                ("Server=x;Password=abcdefgh;Database=foo;", "Server=x;[REDACTED];Database=foo;"),
                ("token=abcdefgh,mode=fast", "[REDACTED],mode=fast"),
                ("DB_PASSWORD=Xy7#kL9a,mN2$pQ4!", "[REDACTED],mN2$pQ4!"),
                ("API_KEY: Xy7#kL9a;mN2$pQ4!.", "[REDACTED];mN2$pQ4!."),
                ("setx API_KEY Xy7kL9am;N2pQ4zR8, sonra", "[REDACTED];N2pQ4zR8, sonra"),
                ('password="Xy7#kL9a"mN2$pQ4!', "[REDACTED]mN2$pQ4!"),
                ("setx API_KEY 'Xy7kL9am'N2pQ4zR8", "[REDACTED]N2pQ4zR8"),
                ('DB_PASSWORD="Xy7#kL9a,mN2$pQ4!"', "[REDACTED]")):
            with self.subTest(text=text):
                self.assertEqual(redact(text, self.state), (expected, 1))

    def test_unquoted_spans_are_masked_whole_or_left_as_written(self):
        # Same class check as the backtick grid, for values no backtick opens:
        # the prefixes, a value holding 0-3 backticks, commas or semicolons in a
        # row, a wrapper (none, closing backticks, one backtick, quotes) and the
        # text after it. Either the input is unchanged and Jev accepts it, or no
        # 6-character piece of the value is left and Jev rejects it. A comma or
        # a semicolon ends an unquoted value as in 3.17.0: there the result is
        # the head of the value redacted on its own, followed by the rest of
        # the text as written.
        store = runtime.MemoryStore(self.state, self.vault)
        values = (('Xy7#kL9a', 'mN2$pQ4!'), ('Xy7#k', 'L9amN2$pQ4!'),
                  ('Xy7#kL9amN2', '$pQ4!z'), ('Xy7#​kL9a', 'mN2$pQ4!'))
        wrappers = (('', ''), ('', '`'), ('', '``'), ('', '```'), ('`', '`'), ('`', ''),
                    ('"', '"'), ("'", "'"))
        inners = [''.join(run) for size in range(4) for run in itertools.product('`,;', repeat=size)]
        afters = ('', ' ve devam', '.', ', sonra', "'yi kullan", 'değerini',
                  '\nsonraki satır', '\r\nsonraki satır', ' `kod` sonra', '\nsonra',
                  '\r\nsonra', '” dedi', "'lerindekilerden geç", '…')
        failures, outcomes = [], {'masked': 0, 'kept': 0, 'split': 0}
        for prefix, (opening, closing), inner, (head, tail), after in itertools.product(
                ('DB_PASSWORD=', 'export DB_PASSWORD=', 'API_KEY: ', 'setx API_KEY ',
                 '- `OPENAI_API_KEY='), wrappers, inners, values, afters):
            value = head + inner + tail
            text = prefix + opening + value + closing + after
            result, count = redact(text, self.state)
            try:
                advisor._safe(store, {'statement': text})
                accepted = True
            except ValueError:
                accepted = False
            cut = min((value.index(mark) for mark in ',;' if mark in value), default=None)
            if not opening and cut is not None:
                outcomes['split'] += 1
                start = prefix + value[:cut]
                expected = redact(start, self.state)
                ok = ((result, count) == (expected[0] + text[len(start):], expected[1])
                      and accepted == (count == 0))
            elif result == text:
                outcomes['kept'] += 1
                ok = count == 0 and accepted
            else:
                outcomes['masked'] += 1
                pieces = {plain[i:i + 6] for plain in (value, value.replace('​', ''))
                          for i in range(len(plain) - 5)}
                ok = (count >= 1 and '[REDACTED]' in result and not accepted
                      and not any(piece in result for piece in pieces))
            if not ok:
                failures.append((text, result, count, accepted))
        self.assertEqual(failures[:10], [])
        self.assertEqual(sum(outcomes.values()), 5 * 8 * 40 * 4 * 14)
        self.assertGreater(outcomes['masked'], 0)
        self.assertGreater(outcomes['kept'], 0)
        self.assertGreater(outcomes['split'], 0)

    def test_setx_values_that_3_17_0_masked_stay_masked(self):
        # 3.17.0 read a setx or set -x value that starts with a backtick, or
        # holds one in its first eight characters, as part of the value. The
        # span and unquoted rules left such a value as written when it did not
        # close before a space or the line end (PowerShell "setx DB_PASSWORD
        # `$Xy7kL9am2 /M"), so it reached the note, memory.sqlite3 and Jev. The
        # text and the 3.17.0 count were produced by the 3.17.0 source.
        store = runtime.MemoryStore(self.state, self.vault)
        for text, value, count_3_17_0 in SETX_3_17_0:
            self.assertEqual(count_3_17_0, len(SETX_3_17_0_VALUE.findall(text)))
            with self.subTest(text=text):
                result, count = redact(text, self.state)
                self.assertGreaterEqual(count, 1)
                for piece in re.findall(r'[A-Za-z0-9]{6,}', value):
                    self.assertNotIn(piece, result)
                with self.assertRaises(ValueError):
                    advisor._safe(store, {'statement': text})
        for text, expected in (('setx DB_PASSWORD `$Xy7kL9am2 /M', '[REDACTED] /M'),
                               ('setx API_KEY `$Xy7kL9am2 && echo ok', '[REDACTED] && echo ok'),
                               ('setx API_KEY `Xy7kL9amN2pQ4zR8, sonra', '[REDACTED], sonra'),
                               ('setx API_KEY `Xy7kL9am,N2pQ4 x', '[REDACTED] x')):
            with self.subTest(text=text):
                self.assertEqual(redact(text, self.state), (expected, 1))

    def test_setx_spans_masked_by_3_17_0_are_masked_whole(self):
        # Class check for setx and set -x with backticks: an opening run of 0-6
        # backticks, a value holding 0-3 in a row, a closing run of 0-6 and the
        # text after it. Wherever the 3.17.0 setx rule (SETX_3_17_0_VALUE)
        # masked, the value is masked and no 6-character piece of it is left.
        values = (('Xy7kL9am', 'N2pQ4zR8'), ('$Xy7k', 'L9amN2pQ'), ('kisa', 'mN2pQ4zR8'),
                  ('Xy7kL9a', ''), ('"Xy7kL9am', '"'), ('Xy7#kL9', 'a,mN2$pQ4!'), ('Ab1cde', ''))
        afters = ('', ' /M', ' && echo ok', ', sonra', "'yi kullan", '"', '\nsonra', '” dedi')
        failures, masked = [], 0
        for prefix, opening, inner, (head, tail), closing, after in itertools.product(
                ('setx API_KEY ', 'setx /m DB_PASSWORD ', 'set -x API_KEY ', '`setx API_KEY '),
                range(7), range(4), values, range(7), afters):
            value = head + '`' * inner + tail
            text = prefix + '`' * opening + value + '`' * closing + after
            if not SETX_3_17_0_VALUE.search(text):
                continue
            masked += 1
            result, count = redact(text, self.state)
            if count < 1 or any(piece in result for piece in re.findall(r'[A-Za-z0-9]{6,}', value)):
                failures.append((text, result, count))
        self.assertEqual(failures[:10], [])
        self.assertGreater(masked, 20000)

    def test_variable_name_in_backticks_after_a_secret_label_stays_text(self):
        # Docs name the variable that holds a credential; masking the name hid
        # nothing secret and made Jev's _safe skip every query that found the note.
        store = runtime.MemoryStore(self.state, self.vault)
        for text in ("API key: `OPENAI_API_KEY` ortam değişkeninden okunur.",
                     "Token: `GITHUB_TOKEN` kullan", "password: `DB_PASSWORD` alanına yaz",
                     "Token:\n`GITHUB_TOKEN`", "Token: ``GITHUB_TOKEN``",
                     "Kurulum: Token: `GITHUB_TOKEN` ortam değişkenini ayarla."):
            with self.subTest(text=text):
                self.assertEqual(redact(text, self.state), (text, 0))
                advisor._safe(store, {'statement': text})
        # Mixed case, lower case, no underscore or an unclosed span is still a value.
        for text, expected in ((f"API_KEY=`{VALUE.upper()}`", "[REDACTED]"),
                               ("API_KEY=`Abcd1234_EFGH5678`", "[REDACTED]"),
                               ("API_KEY=`abcd1234_efgh5678`", "[REDACTED]"),
                               ("TOKEN=`GITHUB_TOKEN_VALUE", "[REDACTED]")):
            with self.subTest(text=text):
                self.assertEqual(redact(text, self.state), (expected, 1))

    def test_pem_body_is_masked_without_trailer_and_does_not_swallow_prose(self):
        begin = "-----BEGIN PRIVATE KEY-----"
        body = "QUJD" * 16  # Synthetic base64, no private key.
        escaped = json.dumps(f'{begin}\n{body}\n-----END PRIVATE KEY-----')
        self.assertEqual(redact(escaped, self.state), ('"[REDACTED]"', 1))
        text = f"{begin}\r\n{body}\r\n{begin}\r\n{body}\r\nLabel: retained words"
        self.assertEqual(redact(text, self.state),
                         ("[REDACTED]\r\n[REDACTED]\r\nLabel: retained words", 2))

    def test_turkish_letters_in_a_prefix_never_hide_the_value(self):
        # Unicode IGNORECASE lets [a-z] match İ and ı. A name boundary that did so
        # left the whole value of these names visible while MÜŞTERI_API_KEY masked.
        for name in ('KİŞİSEL_API_KEY', 'GİZLİ_TOKEN', 'ŞİFRE_PASSWORD', 'MÜŞTERİ_SECRET',
                     'MÜŞTERI_API_KEY', 'çalışma_alanı_token', 'ÖZEL_PASSWD'):
            for text in (f'{name}={VALUE}', f'export {name}="{VALUE}"', f'"{name}": "{VALUE}"'):
                with self.subTest(text=text):
                    filtered, count = redact(text, self.state)
                    self.assertEqual(count, 1)
                    self.assertNotIn(VALUE, filtered)
                    self.assertTrue(filtered.endswith('[REDACTED]'), filtered)
                    kept = filtered[:-len('[REDACTED]')]
                    self.assertTrue(text.startswith(kept), filtered)

    def test_tokenizer_special_token_attributes_are_not_credentials(self):
        # Prefix support must not turn common model code into a secret: a masked
        # note loses the snippet and _safe skips Jev for every query that finds it.
        store = runtime.MemoryStore(self.state, self.vault)
        for text in ('tokenizer.pad_token = tokenizer.eos_token',
                     'tokenizer.bos_token = "<|begin_of_text|>"',
                     "tokenizer.add_special_tokens({'mask_token': '<mask_token>'})",
                     '{"sep_token": "<|separator|>", "cls_token": "<|classify|>"}',
                     'cls_token=tokenizer.cls_token', 'self.unk_token = tokenizer.unk_token'):
            with self.subTest(text=text):
                self.assertEqual(redact(text, self.state), (text, 0))
                advisor._safe(store, {'excerpt': text})
        for name in ('SPLUNK_TOKEN', 'PAD_TOKEN_SECRET', 'MY_EOS_TOKEN_PASSWORD'):
            with self.subTest(name=name):
                self.assertEqual(redact(f'{name}={VALUE}', self.state), ('[REDACTED]', 1))

    def test_secret_and_private_key_names_redact(self):
        for name in ('AWS_SECRET_ACCESS_KEY', 'SECRET_KEY', 'DJANGO_SECRET_KEY', 'secretKey',
                     'aws_secret_access_key', 'PRIVATE_KEY', 'WALLET_PRIVATE_KEY'):
            for text in (f'{name}={VALUE}', f'export {name}="{VALUE}"', f'"{name}": "{VALUE}"'):
                with self.subTest(text=text):
                    self.assertEqual(redact(text, self.state)[1], 1)
                    self.assertNotIn(VALUE, redact(text, self.state)[0])
        self.assertEqual(redact(f'https://example.com/?secret_key={VALUE}&format=md', self.state),
                         ('https://example.com/?[REDACTED]&format=md', 1))
        for text in (f'PUBLIC_KEY={VALUE}', 'SSH_PRIVATE_KEY_PATH=/home/user/.ssh/id_ed25519',
                     f'SECRET_KEY_ID={VALUE}', f'FOREIGN_KEY={VALUE}', 'SECRET_KEY=short'):
            with self.subTest(text=text):
                self.assertEqual(redact(text, self.state), (text, 0))

    def test_subscript_and_command_forms_redact(self):
        for text, expected in (
                (f'os.environ["TYPESAFE_API_KEY"] = "{VALUE}"', 'os.environ[[REDACTED]'),
                (f"os.environ['OPENAI_API_KEY']='{VALUE}'", 'os.environ[[REDACTED]'),
                (f'process.env["MY_SECRET"] = "{VALUE}";', 'process.env[[REDACTED];'),
                (f'setx TYPESAFE_API_KEY "{VALUE}"', '[REDACTED]'),
                (f'setx /M TYPESAFE_API_KEY {VALUE}', '[REDACTED]'),
                (f'SETX OPENAI_API_KEY "{VALUE}" /M', '[REDACTED] /M'),
                (f'[Environment]::SetEnvironmentVariable("TYPESAFE_API_KEY", "{VALUE}", "User")',
                 '[Environment]::[REDACTED], "User")'),
                (f"[System.Environment]::SetEnvironmentVariable('DB_PASSWORD','{VALUE}','Machine')",
                 '[System.Environment]::[REDACTED])'),
                (f'set -gx TYPESAFE_API_KEY "{VALUE}"', '[REDACTED]'),
                (f'set -Ux MY_SECRET {VALUE}', '[REDACTED]')):
            with self.subTest(text=text):
                self.assertEqual(redact(text, self.state), (expected, 1))
        # A space also separates prose words: unquoted command values need a digit.
        for text in ('setx TYPESAFE_API_KEY komutunu kullan', 'set -x GITHUB_TOKEN değişkenini oku',
                     'setx TYPESAFE_API_KEY ""', 'setx PATH "%PATH%;C:\\Tools\\bin"',
                     'setx TYPESAFE_BASE_URL https://example.com/v1', 'reset -x MY_TOKEN abcdefgh12',
                     'set -x\nGITHUB_TOKEN abcdefgh12', f'[token]: https://example.com/{VALUE}',
                     '[Environment]::GetEnvironmentVariable("TYPESAFE_API_KEY", "User")',
                     'x["pad_token"] = tokenizer.eos_token'):
            with self.subTest(text=text):
                self.assertEqual(redact(text, self.state), (text, 0))

    def test_prefixed_query_assignment_preserves_other_parameters(self):
        for name in NAMES:
            text = f'https://example.com/?{name}={VALUE}&format=markdown'
            with self.subTest(name=name):
                self.assertEqual(redact(text, self.state),
                                 ('https://example.com/?[REDACTED]&format=markdown', 1))

    def test_jev_safe_rejects_prefixed_keys_and_values_after_json_escaping(self):
        store = runtime.MemoryStore(self.state, self.vault)
        for name in NAMES:
            assignment = f'export {name}="{VALUE}"'
            for value in (assignment, {'excerpt': assignment}, {name: VALUE},
                          {'statement': json.dumps({'text': assignment})}):
                with self.subTest(name=name, value=value):
                    with self.assertRaisesRegex(ValueError, '^advisor_sensitive_input$'):
                        advisor._safe(store, value)

    def test_installed_note_create_keeps_prefixed_secrets_out_of_note_and_index(self):
        env = isolated_env(self.root / 'home')
        installed = install(self.vault, self.state, env)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        cli = self.vault / 'beyin.py'
        enabled = run_python(cli, ['preferences', '--secret-filter', 'on'], self.vault, env)
        self.assertEqual(enabled.returncode, 0, enabled.stderr)
        body = '\n'.join(f'export {name}="{VALUE}"' for name in NAMES)
        created = run_python(cli, ['note-create', '--json'], self.vault, env,
                             {'source': 'notes/env-assignments.md', 'text': body})
        self.assertEqual(created.returncode, 0, created.stderr)
        result = json.loads(created.stdout)
        self.assertEqual(result['redacted'], len(NAMES))
        stored = (self.vault / result['source']).read_text(encoding='utf-8')
        self.assertEqual(stored.count('[REDACTED]'), len(NAMES))
        self.assertNotIn(VALUE, stored)
        synced = run_python(cli, ['sync'], self.vault, env)
        self.assertEqual(synced.returncode, 0, synced.stderr)
        for database in self.state.rglob('*.sqlite3*'):
            self.assertNotIn(VALUE.encode(), database.read_bytes(), database.name)

    def test_installed_note_create_masks_maintainer_forms_and_keeps_tokenizer_code(self):
        env = isolated_env(self.root / 'home')
        installed = install(self.vault, self.state, env)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        cli = self.vault / 'beyin.py'
        enabled = run_python(cli, ['preferences', '--secret-filter', 'on'], self.vault, env)
        self.assertEqual(enabled.returncode, 0, enabled.stderr)
        code = 'tokenizer.pad_token = tokenizer.eos_token'
        body = '\n'.join((f'setx TYPESAFE_API_KEY "{VALUE}"', f'os.environ["OPENAI_API_KEY"] = "{VALUE}"',
                          f'KİŞİSEL_API_KEY={VALUE}', f'AWS_SECRET_ACCESS_KEY={VALUE}', code))
        created = run_python(cli, ['note-create', '--json'], self.vault, env,
                             {'source': 'notes/env-forms.md', 'text': body})
        self.assertEqual(created.returncode, 0, created.stderr)
        result = json.loads(created.stdout)
        self.assertEqual(result['redacted'], 4)
        stored = (self.vault / result['source']).read_text(encoding='utf-8')
        self.assertNotIn(VALUE, stored)
        self.assertIn('os.environ[[REDACTED]', stored)
        self.assertIn('KİŞİ[REDACTED]', stored)
        self.assertIn(code, stored)
        synced = run_python(cli, ['sync'], self.vault, env)
        self.assertEqual(synced.returncode, 0, synced.stderr)
        for database in self.state.rglob('*.sqlite3*'):
            self.assertNotIn(VALUE.encode(), database.read_bytes(), database.name)

    def test_auto_context_blocks_prefixed_assignments_before_transport(self):
        # The public control must actually reach the injected transport. No real
        # HTTP call is possible, even while reproducing the pre-fix leak.
        for name in (*NAMES, 'PUBLIC_API_KEY_NAME'):
            for location in ('prompt', 'note'):
                with self.subTest(name=name, location=location), tempfile.TemporaryDirectory(
                        dir=self.root) as directory, patch.dict(
                            os.environ, {'TYPESAFE_API_KEY': 'synthetic-key'}, clear=True):
                    root = Path(directory)
                    vault = root / 'vault'
                    vault.mkdir()
                    store = runtime.MemoryStore(root / 'state', vault)
                    assignment = f'export {name}="{VALUE}"'
                    text = 'Quartz deploy site instructions.'
                    query = QUERY
                    if location == 'note':
                        text += '\n' + assignment
                    else:
                        query += '\n' + assignment
                    (vault / 'deploy.md').write_text(text, encoding='utf-8')
                    store.ingest(dict(id='deploy', title='deploy', text=text, source='deploy.md'))
                    client.set_mode(store.state_dir, 'on', enable=['auto_context'])
                    calls = []

                    def transport(url, body, key, timeout):
                        calls.append(body)
                        answers = {'topical': {'type': 'noul', 'noul': 0.9}}
                        answers.update({ident: {'type': 'noul', 'noul': 0.9}
                                        for ident in body['state']['notes']})
                        return {'answers': answers}

                    local = store.context_for('claude', query, strict=True, budget_chars=6000)
                    self.assertEqual([record['id'] for record in local['records']], ['deploy'])
                    result = advisor.auto_context(store, 'claude', query, local, budget_chars=6000,
                                                  transport=transport)
                    if name == 'PUBLIC_API_KEY_NAME':
                        self.assertEqual(len(calls), 1)
                        self.assertIn(VALUE, json.dumps(calls))
                    else:
                        self.assertFalse(calls, 'Sensitive assignment reached the offline transport')
                        self.assertEqual(result, local)
                        log = (store.state_dir / 'jev-calls.jsonl').read_text(encoding='utf-8')
                        rows = [json.loads(line) for line in log.splitlines()]
                        self.assertEqual([row['outcome'] for row in rows], ['skipped_sensitive'])
                        self.assertNotIn(VALUE, log)


if __name__ == '__main__':
    unittest.main()
