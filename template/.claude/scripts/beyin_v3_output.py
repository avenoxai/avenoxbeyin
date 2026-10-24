"""Machine JSON has a byte encoding; caller-owned human streams keep theirs."""
import json
import sys


def write_json(value, *, stream=None, indent=2):
    """Emit one UTF-8 JSON document without reconfiguring caller streams.

    Text-only capture objects receive the same Unicode JSON for embedded callers;
    actual CLI pipes use their binary buffer, independent of terminal encoding.
    """
    stream = sys.stdout if stream is None else stream
    text = json.dumps(value, ensure_ascii=True, indent=indent) + '\n'
    buffer = getattr(stream, 'buffer', None)
    if buffer is None:
        stream.write(text)
    else:
        stream.flush()
        buffer.write(text.encode('utf-8'))
        buffer.flush()
