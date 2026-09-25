"""User-owned component exclusions for local install and update. No models."""
import json
import os
from pathlib import Path
import tempfile

EXCLUDABLE_COMPONENTS = (
    'agents_block', 'adapters', 'adapters/hermes', 'adapters/omp', 'adapters/opencode',
    'harnesses/antigravity', 'launchers', 'skills', 'skills/beyin',
    'skills/beyin-doktor', 'skills/beyin-guncelle'
)


def validate_exclusions(items):
    if not isinstance(items, (list, tuple, set)) or not all(isinstance(x, str) for x in items):
        raise ValueError('excluded_components must be a list of strings')
    normalized = [x.replace('\\', '/') for x in items]
    unknown = sorted(set(normalized) - set(EXCLUDABLE_COMPONENTS))
    if unknown:
        raise ValueError('Unknown or core component ' + ', '.join(unknown) +
                         '; excludable: ' + ', '.join(EXCLUDABLE_COMPONENTS))
    return sorted(set(normalized))


def exclusions_path(vault):
    path = Path(vault) / '.beyin-exclusions.json'
    if path.is_symlink():
        raise ValueError('Exclusions must be a regular vault-local file')
    return path


def read_exclusions(vault):
    path = exclusions_path(vault)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        raise ValueError('Invalid exclusions file format; expected JSON')
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get('excluded_components', [])
    else:
        raise ValueError('Invalid exclusions file format; expected list or dict')
    return validate_exclusions(items)


def save_exclusions(vault, items):
    path = exclusions_path(vault)
    valid = validate_exclusions(items)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps({'excluded_components': valid}, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    fd, temporary = tempfile.mkstemp(prefix='.beyin-exclusions-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return valid
