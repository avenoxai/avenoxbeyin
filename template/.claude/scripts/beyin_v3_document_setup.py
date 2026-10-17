"""Document prerequisite report for the existing agent-led system setup flow.

Never downloads, installs, elevates, edits PATH or runs a package manager. The
active agent executes the concrete package actions within the user's existing
system authority, then reruns the installed content probe. OS packages remain
owned by that package manager, outside the vault install/rollback journal.
"""
import hashlib
import platform
import shutil
import sys

from beyin_v3_parser_probe import document_capabilities
from beyin_v3_workspaces import canonical_bytes


def _system_actions(*, missing_tools=('pdftoppm', 'tesseract'), missing_languages=True):
    steps = []
    if sys.platform == 'darwin':
        manager = 'brew'
        packages = (['poppler'] if 'pdftoppm' in missing_tools else []) + (['tesseract'] if 'tesseract' in missing_tools else []) + (['tesseract-lang'] if missing_languages else [])
        args, admin = ['install'], False
    elif sys.platform == 'win32':
        path = shutil.which('winget')
        if not path:
            return [], 'existing_windows_package_manager_unavailable'
        packages = (['oschwartz10612.Poppler'] if 'pdftoppm' in missing_tools else []) + (['UB-Mannheim.TesseractOCR'] if 'tesseract' in missing_tools else [])
        for package in packages:
            steps.append({'argv': [path, 'install', '--exact', '--id', package, '--source', 'winget'],
                          'system_authority_required': True})
        return steps, 'verify_tesseract_tur_eng_data_and_PATH_in_new_process; https://tesseract-ocr.github.io/tessdoc/Installation.html'
    elif sys.platform.startswith('linux'):
        try:
            distro = platform.freedesktop_os_release()
        except OSError:
            distro = {}
        ids = set((distro.get('ID', '') + ' ' + distro.get('ID_LIKE', '')).split())
        if ids & {'debian', 'ubuntu'}:
            manager = 'apt-get'
            packages = (['poppler-utils'] if 'pdftoppm' in missing_tools else []) + (['tesseract-ocr'] if 'tesseract' in missing_tools else []) + (['tesseract-ocr-eng', 'tesseract-ocr-tur'] if missing_languages else [])
            args = ['install']
        elif ids & {'arch', 'manjaro'}:
            manager = 'pacman'
            packages = (['poppler'] if 'pdftoppm' in missing_tools else []) + (['tesseract'] if 'tesseract' in missing_tools else []) + (['tesseract-data-eng', 'tesseract-data-tur'] if missing_languages else [])
            args = ['-S', '--needed']
        elif ids & {'fedora', 'rhel', 'centos'}:
            manager = 'dnf'
            packages = (['poppler-utils'] if 'pdftoppm' in missing_tools else []) + (['tesseract'] if 'tesseract' in missing_tools else []) + (['tesseract-langpack-eng', 'tesseract-langpack-tur'] if missing_languages else [])
            args = ['install']
        else:
            return [], 'identify_distribution_packages_for_pdftoppm_tesseract_tur_eng'
        admin = True
    else:
        return [], 'unsupported_system_package_setup'
    path = shutil.which(manager)
    if not path:
        return [], 'existing_system_package_manager_unavailable'
    steps.append({'argv': [path, *args, *packages], 'system_authority_required': admin})
    return steps, 'execute_with_existing_system_authority_then_repeat_installed_probe'


def document_setup(vault):
    """Read-only installed capability report plus concrete system package actions.

    Native presence is never substituted for the actual OCR content/location
    probe. No absolute tool path or package version is a permanent capability.
    """
    capabilities = document_capabilities(vault)
    states = {c['name']: c['state'] for c in capabilities}
    ready = all(states.get(name) == 'ready' for name in ('pdf_text', 'docx_text', 'ocr_tr_en'))
    actions, next_action = [], 'continue_workspace_plan_and_daily_verification'
    if not ready:
        ocr = next((c for c in capabilities if c['name'] == 'ocr_tr_en'), {})
        if states.get('pdf_text') != 'ready' or 'ocr_probe_asset' in ocr.get('detail', ''):
            next_action = 'repair_managed_parser_assets_with_existing_installer_then_repeat_probe'
        elif ocr.get('state') == 'missing_dependency':
            missing = tuple(name for name in ('pdftoppm', 'tesseract') if not shutil.which(name))
            actions, next_action = _system_actions(missing_tools=missing, missing_languages=True)
        else:
            next_action = 'inspect_document_probe_failure_before_retrying; no_ready_claim'
    for action in actions:
        action.update(owner='system_package_manager', may_use_network=True,
                      rollback='system_package_manager; never_uninstall_preexisting_packages_via_vault_revert')
        action['action_sha256'] = hashlib.sha256(canonical_bytes(action)).hexdigest()
    return {'status': 'ready' if ready else 'dependencies_required' if actions else 'needs_action',
            'capabilities': capabilities, 'system_actions': actions, 'next_action': next_action,
            'offline': 'installed_tools_or_system_package_cache_required; no_portable_native_bundle',
            'workspace_health': 'verification_pending'}
