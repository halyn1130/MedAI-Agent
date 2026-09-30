"""Read a frozen dataset and reject files changed since the manifest was made."""
import hashlib
import json
from pathlib import Path

DEFAULT_MANIFEST = Path(__file__).parent / 'data/frozen_manifest.json'


def load_company(manifest_path, company_id):
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text())
    entry = manifest['companies'][company_id]
    path = (manifest_path.parent / entry['path']).resolve()
    if not path.is_relative_to(manifest_path.parent):
        raise ValueError('Dataset path is outside the frozen dataset directory')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != entry['sha256']:
        raise ValueError('Frozen dataset has changed; restore the original input')
    state = json.loads(raw)
    if state['company_profile']['company_id'] != company_id:
        raise ValueError('Dataset company ID mismatch')
    return state, entry['sha256']
