"""A <!-- beyin:pin --> quoted as code pins nothing (#890, #924). Class test: every place a
rule can carry the marker crossed with every code context, plus the 3.18.0 parity rule for
rule text that cannot hold code. Synthetic rules only."""
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
)
# The line the inline context lands on: rule line (start, end), its own continuation line,
# after a reason, on the reason's continuation line, before a reason.
HOSTS = (
    '- Kural metni {c}\n',
    '- {c} kural metni\n',
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
