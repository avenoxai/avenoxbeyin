"""A <!-- beyin:pin --> quoted as code pins nothing (#890, #924). Class test: every place a
rule can carry the marker crossed with every code context, a generated grammar for the reason
drop (code is opaque to REASON), plus the 3.18.0 parity rule for rule text that cannot hold
code. Synthetic rules only."""
import importlib.util
import os
from pathlib import Path
import random
import sys
import unittest

ROOT = Path(os.environ.get('BEYIN_TEST_REPO', Path(__file__).resolve().parents[1]))
MARKER = '<!-- beyin:pin -->'


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


companion = load('beyin_v3_issue924_companion', 'template/.claude/scripts/beyin_v3_companion.py')

# Where a marker can sit on one line of a list item: (template, pins). The marker is code only
# inside a closed inline code span; an unclosed or mismatched backtick run is a literal.
INLINE = (
    ('{m}', True),
    ('`{m}`', False),
    ('``{m}``', False),
    ('```{m}```', False),
    ('` {m} `', False),
    ('`` ` {m} ``', False),
    ('`a` {m}', True),
    ('{m} `b`', True),
    ('`a`{m}`b`', True),
    ('`x {m}', True),
    ('``{m}`', True),
    # The reason keyword itself quoted in the span, before or after the marker.
    ('`**neden:** x {m}`', False),
    ('``a **neden:** {m}``', False),
    ('`{m} **neden:** x`', False),
    ('` **neden:** x ` {m} `', True),
)
# The line the inline context lands on: rule line (start, middle, end), its own continuation
# line, after a reason, on the reason's continuation line, before a reason.
HOSTS = (
    '- Kural metni {c}\n',
    '- {c} kural metni\n',
    '- **kural:** Pin yazımı {c} sonra.\n',
    '- Kural metni\n  devam {c}\n',
    '- **kural:** Kural. **neden:** gerekçe {c}\n',
    '- **kural:** Kural. **neden:** gerekçe\n  devam {c}\n',
    '- **kural:** Kural {c} **neden:** gerekçe\n',
)


def blocks(indent):
    """Fenced contexts for a marker on its own line, at any indent a list item can use."""
    p = ' ' * indent
    return (
        ((f'{p}```', f'{p}{{m}}', f'{p}```'), False),
        ((f'{p}~~~', f'{p}{{m}}', f'{p}~~~'), False),
        ((f'{p}```md', f'{p}{{m}}', f'{p}```'), False),
        # A shorter run does not close a longer fence; nor does the other fence character.
        ((f'{p}````', f'{p}{{m}}', f'{p}```', f'{p}````'), False),
        ((f'{p}~~~', f'{p}{{m}}', f'{p}```', f'{p}~~~'), False),
        # An unclosed fence runs to the end of the item.
        ((f'{p}```', f'{p}{{m}}'), False),
        # After a closed fence the marker is live again.
        ((f'{p}```', f'{p}x', f'{p}```', f'{p}{{m}}'), True),
        # A backtick in the info string means this line is not a fence.
        ((f'{p}``` a`b', f'{p}{{m}}'), True),
    )


BLOCK_HOSTS = ('- Kural metni:\n{b}\n', '- **kural:** Kural. **neden:** örnek:\n{b}\n')


def grid():
    for template, pins in INLINE:
        for host in HOSTS:
            yield host.format(c=template.format(m=MARKER)), pins
    for indent in (0, 2, 3, 4, 6, 8):
        for lines, pins in blocks(indent):
            for host in BLOCK_HOSTS:
                yield host.format(b='\n'.join(lines).format(m=MARKER)), pins


WORDS = ('Kural', 'x', 'gerekçe', '`', '``', '`a`', '``b``', '**neden:**', '**why:**', '**kural:**',
         MARKER, MARKER, '` ' + MARKER + ' `')
FENCE_LINES = ('```', '~~~', '````', '~~~~', '```md', '``` a`b')


def generated_item(rng, indents, fences):
    """One list item from a small grammar of rule lines, reason keywords, markers and code."""
    def words(most):
        return ' '.join(rng.choice(WORDS) for _ in range(rng.randint(1, most)))

    lines = ['- R ' + words(6)]
    for _ in range(rng.randint(0, 7)):
        kind = rng.random()
        if kind < .3:
            lines.append(rng.choice(('  ', '    ')) + words(5))
        elif kind < .55:
            lines.append(rng.choice(indents) + rng.choice(fences))
        elif kind < .65:
            lines.append('')
        elif kind < .75:
            lines.append(rng.choice(('  ', '    ')) + '- ' + words(4))
        elif kind < .8:
            lines.append('# başlık')
        else:
            lines.append(rng.choice(indents) + rng.choice((MARKER, '**neden:** ' + MARKER, '**neden:** y',
                                                           '`' + MARKER + '`')))
    return '\n'.join(lines) + '\n'


def reference_without_reasons(text):
    """3.18.0's without_reasons for text that cannot hold code: a reason keeps its first marker."""
    def drop(match):
        found = companion.PINNED_RULE.search(match[0])
        return ' ' + found[0] if found else ''
    stripped, count = companion.REASON.subn(drop, text)
    if not count:
        return text
    return stripped.rstrip('\n') + f'\n[{count} rule reasons (**neden:**) omitted to fit the opening; read source]\n'


class PinCodeContextTest(unittest.TestCase):
    def test_every_marker_place_crossed_with_every_code_context(self):
        # Invariant: an item is pinned exactly when a marker stands outside code, and dropping
        # the reasons never changes that (a quoted marker never turns into a live one).
        cases = list(grid())
        self.assertEqual(len(cases), len(INLINE) * len(HOSTS) + 6 * 8 * len(BLOCK_HOSTS))
        for text, pins in cases:
            for newline in ('\n', '\r\n'):
                item = text.replace('\n', newline)
                with self.subTest(item=item):
                    self.assertEqual(companion._is_pinned(item), pins)
                    stripped = companion.without_reasons(item)
                    self.assertEqual(companion._is_pinned(stripped), pins, stripped)
                    if '**neden:**' in item:
                        self.assertNotIn('gerekçe', stripped)

    def test_reason_drop_keeps_every_pin_in_a_generated_grammar(self):
        # Invariant for the reason drop (#924): REASON reads code as opaque, so dropping the
        # reasons never cuts a code span or a fence in two. A rule that pins keeps pinning and
        # a rule that only quotes the marker never starts. The grammar mixes reason keywords,
        # markers and backticks with fences at any indent, blank lines, sub-items and headings.
        rng = random.Random(9240)
        for _ in range(3000):
            item = generated_item(rng, ('', '  ', '    ', '      '), FENCE_LINES)
            for newline in ('\n', '\r\n'):
                text = item.replace('\n', newline)
                with self.subTest(item=text):
                    self.assertEqual(companion._is_pinned(companion.without_reasons(text)),
                                     companion._is_pinned(text))

    def test_reason_drop_keeps_rule_boundaries_and_pins_of_every_rule(self):
        # The same invariant over whole rule files: rule_starts finds the same rules after the
        # drop and each one keeps its pin. Fences here are the ones rule_starts also knows
        # (indent 0-3, bare closing line), so the two readings of code agree.
        rng = random.Random(9241)

        def rules(text):
            starts = companion.rule_starts(text)
            return [text[a:b] for a, b in zip(starts, starts[1:] + [len(text)])]

        for _ in range(1500):
            text = '# Kurallar\n\n' + ''.join(generated_item(rng, ('', '  ', '   '), ('```', '~~~', '````', '~~~~'))
                                              for _ in range(rng.randint(2, 5)))
            if rng.random() < .2:
                text = text.replace('\n', '\r\n')
            with self.subTest(text=text):
                before, after = rules(text), rules(companion.without_reasons(text))
                self.assertEqual(len(after), len(before))
                self.assertEqual([companion._is_pinned(item) for item in after],
                                 [companion._is_pinned(item) for item in before])

    def test_quoted_reason_is_kept_and_real_reason_is_dropped(self):
        # A `**neden:**` quoted as code is no reason: the code stays whole, the real reason goes.
        cases = (
            ('- Biçim: `**neden:** ... ' + MARKER + '` ile yazılır.\n',
             '- Biçim: `**neden:** ... ' + MARKER + '` ile yazılır.\n'),
            ('- Biçim: `- kural ' + MARKER + ' **neden:** gerekçe` diye. **neden:** asıl gerekçe\n',
             '- Biçim: `- kural ' + MARKER + ' **neden:** gerekçe` diye.\n'
             '[1 rule reasons (**neden:**) omitted to fit the opening; read source]\n'),
            # A reason with a fenced example list goes whole, fence included.
            ('- Kural. **neden:** örnek:\n  ```\n  - a\n\n  - b\n  ```\n- Sonraki ' + MARKER + '\n',
             '- Kural.\n- Sonraki ' + MARKER + '\n'
             '[1 rule reasons (**neden:**) omitted to fit the opening; read source]\n'),
            # A blank line still ends a reason before a fence that is not part of it.
            ('- Kural. **neden:** kısa.\n\n  ```\n  make test\n  ```\n',
             '- Kural.\n\n  ```\n  make test\n  ```\n'
             '[1 rule reasons (**neden:**) omitted to fit the opening; read source]\n'),
            # Right after a fence's closing line the kept marker takes a line of its own.
            ('- Kural:\n  ```\n  make test\n  ```\n  **neden:** CI ister. ' + MARKER + '\n',
             '- Kural:\n  ```\n  make test\n  ```\n  ' + MARKER + '\n'
             '[1 rule reasons (**neden:**) omitted to fit the opening; read source]\n'),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(companion.without_reasons(text), expected)
                self.assertEqual(companion._is_pinned(expected), companion._is_pinned(text))

    def test_text_that_cannot_hold_code_pins_as_in_3_18_0(self):
        # Parity rule: without a backtick or a tilde there is no code, so the pin check and the
        # reason drop give exactly what 3.18.0 gave.
        rng = random.Random(924)
        pieces = ('Kural metni', '**kural:** Ölçülü yaz', '**neden:** çünkü İstanbul', MARKER,
                  '<!--beyin:PIN-->', 'ıİşğüçö', '#', '1.', '-', '**why:**', 'devam satırı', 'x' * 30)
        for _ in range(400):
            lines = ['# Kurallar', '']
            for _ in range(rng.randint(1, 12)):
                lines.append(rng.choice(('- ', '* ', '1. ')) +
                             ' '.join(rng.choice(pieces) for _ in range(rng.randint(1, 6))))
                lines += ['  ' + rng.choice(pieces) for _ in range(rng.randint(0, 2))]
            text = '\n'.join(lines) + '\n'
            if rng.random() < .2:
                text = text.replace('\n', '\r\n')
            with self.subTest(text=text):
                self.assertEqual(companion._is_pinned(text), bool(companion.PINNED_RULE.search(text)))
                self.assertEqual(companion.without_reasons(text), reference_without_reasons(text))

    def test_quoted_marker_takes_no_pin_room_in_the_opening(self):
        # End to end through ends(): the rule that only quotes the syntax clips like any other,
        # the rule with a live marker is shown, and pinned_omitted stays empty.
        rules = ''.join(f'- R{n:03d} kural metni burada durur, kısa ve tek satır.\n' for n in range(1, 61))
        quoted = '- R020 kural metni burada durur, kısa ve tek satır.\n'
        live = '- R040 kural metni burada durur, kısa ve tek satır.\n'
        text = '# Kurallar\n\n' + rules.replace(
            quoted, quoted.rstrip('\n') + ' Örnek: `' + MARKER + '`\n').replace(
            live, live.rstrip('\n') + ' ' + MARKER + '\n')
        report = {}
        shown = companion.clip(text, 1200, both=True, tail=True, rules_report=report)
        self.assertLessEqual(len(shown), 1200)
        self.assertIn('- R040 ', shown)
        self.assertNotIn('- R020 ', shown)
        self.assertNotIn('pinned_omitted', report)


if __name__ == '__main__':
    unittest.main()
