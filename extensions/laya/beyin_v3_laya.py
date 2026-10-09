"""Optional local Laya backend for the advisor. Standard library only.

Laya (https://github.com/NandhaKishorM/laya, Apache-2.0) is an open-weights System One
decision model. Its `laya-serve` server speaks the same POST /v1/systemone protocol as Jev.
Beyin never imports its Python package or torch: the server is a separate local process,
reached only over a loopback address.

Laya encodes every question as its own row and keeps only the first 512 (english) or 1024
(multilingual) tokens of that row, cutting the rest without an error. A Jev body that keys
several candidates inside one state would hide the later ones. This adapter therefore:

- splits each Jev-shaped body into requests of one candidate and one question (plan),
- checks every request against a per-checkpoint size cap before the first call,
- discards any answer whose row reached the token limit, since it may have been cut,
- pins one checkpoint and rejects answers routed to another,
- reports choice confidence as max(probabilities), Laya's calibrated answer_confidence,
- merges the answers back into the exact Jev response shape for the unchanged validators.

Nothing here writes files or reads credentials; the client passes LAYA_API_KEY if set.
"""
import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request

CONTRACT = 'laya-adapter-v1'
# The checkpoints this adapter pins. typed-decisions is left out on purpose: it is fine-tuned
# on unrelated workflows, and a server that has not loaded it downloads 843 MB on first use.
CHECKPOINTS = ('multilingual', 'english')
KNOWN_CHECKPOINTS = CHECKPOINTS + ('typed-decisions',)
# 8765 rather than laya-serve's own 8000 default, a common development server port: if Laya is
# down, another local app on that port must not receive note text. Start the server with
# LAYA_HOST=127.0.0.1 LAYA_PORT=8765.
DEFAULTS = dict(base_url='http://127.0.0.1:8765', model='multilingual', timeout=8.0)
# Literal addresses only: localhost is refused so a hosts file cannot point it elsewhere.
LOOPBACK = ('127.0.0.1', '::1')
# Tokens per question row (max_len in each checkpoint's rl_agent_config.json).
MAX_TOKENS = dict(multilingual=1024, english=512)
# Size caps for one request state, in size_units() of its JSON. Measured 2026-09-24 with the
# checkpoint tokenizers (laya 0.3.20, snapshot 55cf4c4). The templates below take 39 to 102
# tokens of instruction and options, which leaves at least 918 (multilingual) or 403 (english)
# tokens for the state. On 600 to 2,400 character JSON samples of Turkish docs, ASCII Turkish,
# English prose, 493 real Markdown notes and Python code, tokens per unit peaked at 0.52
# (multilingual) and 0.50 (english); real auto_context note requests used at most 47% of the
# room. Dense adversarial text (base64, punctuation runs, timestamps) reached 0.61 and 0.70, and
# longer wording shrinks the room: both are caught after the call, because a row that reaches
# MAX_TOKENS is discarded, never judged. Re-measure after lengthening QUESTIONS.
STATE_UNITS = dict(multilingual=1700, english=760)
# Per-claim source context for answer_check and memory_assessment, in characters, so one long
# source degrades only its own claim (source_context_incomplete) instead of the whole batch.
# The claim, quotes and JSON escaping take the rest of STATE_UNITS.
CONTEXT_CHARS = dict(multilingual=1200, english=400)
# auto_context note requests carry at least this much of the prompt, or the note is too large.
MIN_PROMPT_UNITS = 60
MAX_REQUESTS = 48
RESPONSE_LIMIT = 1000000

# The only wording Laya sees, in ONE table so a benchmark can replace it. Short and question
# first: Laya fits the instruction and every option into 192 (english) or 256 (multilingual)
# tokens. Measured probes (2026-09-24) separated Turkish negation only with English
# statement-form yes/no rows, so noul rows are English statements. Option texts (criteria)
# stay exactly Jev's, so validation and thresholds read the same keys. Each request carries
# one candidate and one question, so no instruction needs to address a JSON path.
QUESTIONS = {
    'topical': ('noul', 'The request names a concrete subject that stored notes could help with. '
                        'It is not a greeting, thanks or a short confirmation.'),
    'note': ('noul', 'Opening the note would really help answer the request. Sharing a word is not enough.'),
    'retrieval': ('score', 'How directly does the candidate support the query?'),
    'retrieval_part': ('score', 'How directly does the candidate support the query part?'),
    'evidence_review': ('score', 'Do the quotes in the candidate support the whole claim in the query, as the check asks?'),
    'memory_review': ('score', 'How does the candidate record relate to the anchor record in the query, as the check asks?'),
    'relation': ('choice', 'How does the evidence relate to the claim? Use only the evidence.'),
    'support': ('choice', 'How does the evidence relate to the proposal claim?'),
    'commitment': ('choice', 'What commitment to the proposal claim does the speaker express in the evidence?'),
    'kind': ('choice', 'What kind of information is the proposal claim, in its evidence context?'),
    'prior': ('choice', 'How does the proposal claim, as qualified by the evidence, relate to the prior record?'),
}
# Cache fingerprint part: new wording or caps never reuse answers cached under the old ones.
REVISION = hashlib.sha256(json.dumps([CONTRACT, QUESTIONS, STATE_UNITS, MIN_PROMPT_UNITS],
                                     sort_keys=True).encode()).hexdigest()[:16]


class AnswersInvalid(ValueError):
    """Only a fixed diagnostic code, never response content."""
    def __init__(self, issue):
        super().__init__('answers_invalid')
        self.issue = issue


def validate(block):
    """The `laya` block of jev.json, with defaults applied. Any unknown key is invalid."""
    if not isinstance(block, dict) or set(block) - set(DEFAULTS):
        raise ValueError('config_invalid')
    config = dict(DEFAULTS, **block)
    try:
        endpoint(config['base_url'])
    except ValueError:
        raise ValueError('config_invalid') from None
    timeout = config['timeout']
    if (config['model'] not in CHECKPOINTS or type(timeout) not in (int, float)
            or not math.isfinite(timeout) or not 0 < timeout <= 10):
        raise ValueError('config_invalid')
    return config


def _root(base_url):
    if not isinstance(base_url, str):
        raise ValueError('endpoint_invalid')
    parsed = urllib.parse.urlsplit(base_url)
    if (parsed.scheme not in ('http', 'https') or parsed.hostname not in LOOPBACK or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path.rstrip('/') not in ('', '/v1')):
        raise ValueError('endpoint_invalid')
    try:
        parsed.port
    except ValueError:
        raise ValueError('endpoint_invalid') from None
    return parsed.scheme + '://' + parsed.netloc


def endpoint(base_url):
    """The inference URL on a literal loopback address; TYPESAFE_BASE_URL never applies."""
    return _root(base_url) + '/v1/systemone'


def size_units(text):
    """Conservative size of serialized text, in the units STATE_UNITS is measured in.

    One per character, plus one per digit, plus the UTF-8 length of every character above
    U+024F: digits, CJK and emoji cost the Laya tokenizers far more tokens per character
    than Turkish or English prose. Turkish letters stay one unit.
    """
    return len(text) + sum(1 for c in text if c.isdigit()) + sum(len(c.encode('utf-8')) for c in text if ord(c) > 0x24F)


def _units(value):
    return size_units(json.dumps(value, ensure_ascii=False))


def _share(text, budget):
    """Longest prefix of the prompt whose serialized size fits the budget. The only trimming."""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if _units(text[:middle]) - 2 <= budget:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def plan(purpose, body, model):
    """Split one Jev-shaped body into (request, outer question id) pairs. No I/O.

    Raises before any request: budget_exceeded when there would be more than MAX_REQUESTS,
    laya_state_too_large when any state is over its cap. Text is never cut to fit, except
    the auto_context prompt share, which is already a bounded head of the prompt.
    """
    if model not in CHECKPOINTS:
        raise ValueError('config_invalid')
    state, questions, cap = body['state'], body['questions'], STATE_UNITS[model]
    requests = []

    def ask(sub_state, template, outer, criteria=None):
        kind, text = QUESTIONS[template]
        question = dict(type=kind, instructions=text)
        if criteria is not None:
            question['criteria'] = criteria
        requests.append((dict(model=model, state=sub_state, questions=dict(q=question)), outer))

    if purpose == 'auto_context':
        prompt = state['request']
        ask(dict(request=_share(prompt, cap - _units(dict(request='')))), 'topical', 'topical')
        for key, note in state['notes'].items():
            room = cap - _units(dict(request='', note=note))
            if room < MIN_PROMPT_UNITS:
                raise ValueError('laya_state_too_large')
            ask(dict(request=_share(prompt, room), note=note), 'note', key)
    elif purpose == 'answer_check':
        for key, item in state['items'].items():
            ask(dict(claim=item['claim'], evidence=item['evidence']), 'relation', key, questions[key]['criteria'])
    elif purpose == 'memory_assessment':
        base = dict(proposal=state['proposal'], evidence=state['evidence'])
        for name in ('support', 'commitment', 'kind'):
            ask(base, name, name, questions[name]['criteria'])
        for ident, prior in state['prior'].items():
            ask(dict(base, prior=prior), 'prior', 'relation_' + ident, questions['relation_' + ident]['criteria'])
    else:
        # retrieval, memory_review, evidence_review: one request per (facet, candidate).
        query, facets = state['query'], state['facets']
        whole = facets == [query]
        for i, card in enumerate(state['candidates']):
            candidate = {k: card[k] for k in ('title', 'statement', 'scope', 'domains') if k in card}
            for j, facet in enumerate(facets):
                outer = 'f%d_c%d' % (j, i)
                if purpose != 'retrieval':
                    ask(dict(query=query, check=facet, candidate=candidate), purpose, outer, questions[outer]['criteria'])
                elif whole:
                    ask(dict(query=query, candidate=candidate), 'retrieval', outer, questions[outer]['criteria'])
                else:
                    ask(dict(query=query, part=facet, candidate=candidate), 'retrieval_part', outer, questions[outer]['criteria'])
    if sorted(outer for _, outer in requests) != sorted(questions):
        raise ValueError('payload_invalid')
    if len(requests) > MAX_REQUESTS:
        raise ValueError('budget_exceeded')
    if any(_units(sub['state']) > cap for sub, _ in requests):
        raise ValueError('laya_state_too_large')
    return requests


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('redirect_rejected')


def _opener():
    # No proxy of any kind, even when HTTP_PROXY, ALL_PROXY or system settings name one.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def post(url, body, key, timeout):
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    request = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                                     headers=headers, method='POST')
    try:
        with _opener().open(request, timeout=timeout) as response:
            raw = response.read(RESPONSE_LIMIT + 1)
    except urllib.error.HTTPError as exc:
        exc.close()  # the caller maps only the status code; the body is never read
        raise
    if len(raw) > RESPONSE_LIMIT:
        raise ValueError('response_too_large')
    return json.loads(raw)


def _answer(raw, model):
    if not isinstance(raw, dict):
        raise AnswersInvalid('answers_not_object')
    routing = raw.get('routing')
    if not isinstance(routing, dict) or routing.get('model') != model:
        raise ValueError('laya_checkpoint_mismatch')
    usage = raw.get('usage')
    used = usage.get('input_tokens') if isinstance(usage, dict) else None
    # One question per request, so input_tokens is that row's length. A row at the limit may
    # have lost the end of its state: never judge on it.
    if type(used) is not int or not 0 <= used < MAX_TOKENS[model]:
        raise ValueError('laya_state_truncated')
    answers = raw.get('answers')
    if not isinstance(answers, dict):
        raise AnswersInvalid('answers_not_object')
    if set(answers) != {'q'}:
        raise AnswersInvalid('answer_keys_mismatch')
    answer = answers['q']
    if isinstance(answer, dict) and answer.get('type') == 'choice':
        answer = dict(answer)
        values = answer.get('probabilities')
        # Laya's own `confidence` on a choice is normalized entropy, a different scale; its
        # calibrated answer_confidence is max(p). Recompute it rather than trust either field.
        if (isinstance(values, dict) and values and
                all(type(v) in (int, float) and math.isfinite(v) for v in values.values())):
            answer['confidence'] = max(values.values())
    return answer, used


def call(url, body, key, timeout, *, requests, model, sent, guard=None, send=None):
    """Send the planned requests one at a time inside one deadline; merge into the Jev shape.

    laya-serve runs one inference at a time, so parallel requests would only queue. Any failure
    discards every answer. `sent` records each wire request for telemetry; `guard` stops the
    fan-out when the configuration changes. The outer `body` itself is never sent.
    """
    deadline = time.monotonic() + timeout
    send = send or post
    answers, input_tokens = {}, 0
    for sub, outer in requests:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('deadline_exceeded')
        if guard is not None and not guard():
            raise ValueError('configuration_changed')
        sent.append(outer)
        answers[outer], used = _answer(send(url, sub, key, remaining), model)
        input_tokens += used
    return dict(model='laya:' + model, answers=answers, usage=dict(input_tokens=input_tokens, output_tokens=0))


def health(base_url, timeout=1.0):
    """One GET /health on the configured loopback server. Returns only bounded fields."""
    started = time.monotonic()
    result = dict(reachable=False)
    try:
        request = urllib.request.Request(_root(base_url) + '/health', method='GET')
        with _opener().open(request, timeout=timeout) as response:
            raw = response.read(4097)
        result['reachable'] = True
        data = json.loads(raw) if len(raw) <= 4096 else None
        if not isinstance(data, dict):
            raise ValueError('response_invalid')
    except urllib.error.HTTPError as exc:
        exc.close()
        result.update(reachable=True, code='http_error',
                      http_status=exc.code if type(exc.code) is int and 100 <= exc.code <= 599 else None)
        return result
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(exc, ValueError) and str(exc) == 'endpoint_invalid':
            result['code'] = 'endpoint_invalid'
        elif isinstance(reason, ConnectionRefusedError):
            result['code'] = 'provider_unreachable'
        elif isinstance(reason, TimeoutError):
            result['code'] = 'deadline_exceeded'
        else:
            result['code'] = 'response_invalid' if result['reachable'] else 'request_failed'
        return result
    status, device, loaded = data.get('status'), data.get('device'), data.get('loaded')
    result.update(status=status if isinstance(status, str) and re.fullmatch(r'[a-z_]{1,16}', status) else None,
                  device=device if isinstance(device, str) and re.fullmatch(r'[a-z0-9:_-]{1,32}', device) else None,
                  loaded=sorted({name for name in loaded if name in KNOWN_CHECKPOINTS}) if isinstance(loaded, list) else [],
                  latency_ms=round((time.monotonic() - started) * 1000, 3))
    return result
