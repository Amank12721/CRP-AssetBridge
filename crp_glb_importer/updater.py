"""GitHub release updater; no CRP credentials are sent to GitHub."""
import ast
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
import bpy
from bpy.props import BoolProperty

REPO = 'Amank12721/CRP-AssetBridge'
ASSET = 'CRP-AssetBridge.zip'
MODULE = 'crp_glb_importer'
state = {'status': '', 'busy': False, 'release': None, 'restart': False}
_events = queue.Queue()
_enabled = False
_version = (0, 0, 0)

def download(url, limit):
    request = urllib.request.Request(url, headers={'User-Agent': 'CRP-AssetBridge-Updater', 'Accept': 'application/vnd.github+json' if 'api.github.com/' in url else 'application/octet-stream'})
    with urllib.request.urlopen(request, timeout=45) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Release asset exceeds size limit')
    return data

def release_info():
    payload = json.loads(download('https://api.github.com/repos/' + REPO + '/releases/latest', 2*1024*1024))
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', payload.get('tag_name', ''))
    if not match or payload.get('draft') or payload.get('prerelease'):
        raise ValueError('Latest release must have a stable vX.Y.Z tag')
    version = tuple(map(int, match.groups()))
    assets = {a['name']: a['browser_download_url'] for a in payload.get('assets', [])}
    if version <= _version:
        return None
    for name in (ASSET, ASSET + '.sha256'):
        if name not in assets:
            raise ValueError('Release is missing ' + name)
        if not assets[name].startswith('https://github.com/' + REPO + '/releases/download/'):
            raise ValueError('Unexpected release asset URL')
    return {'version': version, 'zip': assets[ASSET], 'checksum': assets[ASSET+'.sha256']}

def validate_archive(raw, checksum, version, blender_version):
    digest = checksum.decode('ascii').strip().split()[0]
    if not re.fullmatch('[a-fA-F0-9]{64}', digest) or hashlib.sha256(raw).hexdigest() != digest.lower():
        raise ValueError('Release checksum verification failed')
    files = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        total = 0
        for entry in archive.infolist():
            p = PurePosixPath(entry.filename)
            if p.is_absolute() or '..' in p.parts or '\\' in entry.filename or ':' in entry.filename or not p.parts or p.parts[0] != MODULE:
                raise ValueError('Unsafe archive path')
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Archive symlinks are not supported')
            if entry.is_dir():
                continue
            if p.suffix.lower() not in ('.py', '.md', '.txt', '.json', '.png', '.svg') or p.name.startswith('.'):
                raise ValueError('Unexpected file in release')
            total += entry.file_size
            if total > 50*1024*1024:
                raise ValueError('Unpacked release exceeds size limit')
            relative = str(PurePosixPath(*p.parts[1:]))
            if relative in files:
                raise ValueError('Duplicate release entry')
            files[relative] = archive.read(entry)
    tree = ast.parse(files['__init__.py'].decode('utf-8-sig'))
    info = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'bl_info' for t in n.targets))
    if tuple(info['version']) != tuple(version):
        raise ValueError('Release tag and add-on version do not match')
    if tuple(info.get('blender', (3,6,0))) > tuple(blender_version):
        raise ValueError('This release requires a newer Blender version')
    for name, content in files.items():
        if name.endswith('.py'):
            compile(content, name, 'exec')
    return files

def install_release(release, target, blender_version):
    raw = download(release['zip'], 25*1024*1024)
    checksum = download(release['checksum'], 4096)
    files = validate_archive(raw, checksum, release['version'], blender_version)
    target = Path(target).resolve()
    backup_root = target.parent / 'crp_assetbridge_update_backups'
    backup_root.mkdir(exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix='previous-', dir=backup_root)) / MODULE
    stage = Path(tempfile.mkdtemp(prefix='crp-update-', dir=target.parent))
    try:
        for name, content in files.items():
            dest = stage / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
        os.replace(target, backup)
        try:
            os.replace(stage, target)
        except Exception:
            os.replace(backup, target)
            raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return str(backup)

def start(kind):
    if state['busy'] or state['restart']:
        return
    state['busy'] = True
    state['status'] = 'Checking GitHub...' if kind == 'check' else 'Downloading and verifying update...'
    release = state['release']
    target = str(Path(__file__).parent)
    blender_version = tuple(bpy.app.version)
    def worker():
        try:
            result = release_info() if kind == 'check' else install_release(release, target, blender_version)
            _events.put((kind, result, None))
        except urllib.error.HTTPError as exc:
            message = 'No public release found (or repository is private)' if exc.code == 404 else 'GitHub HTTP '+str(exc.code)+'; try again later'
            _events.put((kind, None, message))
        except Exception as exc:
            _events.put((kind, None, str(exc)))
    threading.Thread(target=worker, daemon=True).start()

def poll():
    if not _enabled:
        return None
    while not _events.empty():
        kind, result, error = _events.get()
        state['busy'] = False
        if error:
            state['status'] = error
        elif kind == 'check':
            state['release'] = result
            state['status'] = 'Update available: '+'.'.join(map(str,result['version'])) if result else 'You have the latest release'
        else:
            state['restart'] = True
            state['status'] = 'Installed. Save your work and restart Blender.'
        wm = bpy.context.window_manager
        if wm:
            for window in wm.windows:
                for area in window.screen.areas:
                    area.tag_redraw()
    return 0.5

def startup_check():
    addon = bpy.context.preferences.addons.get(__package__)
    if _enabled and addon and addon.preferences.check_updates_on_startup:
        start('check')

class CRP_UpdatePreferences(bpy.types.AddonPreferences):
    bl_idname = __package__
    label_visualizer_enabled: BoolProperty(name='Enable Label Visualizer', default=True)
    show_label_options: BoolProperty(name='Label Visualizer Settings', default=False)
    check_updates_on_startup: BoolProperty(name='Check for updates when Blender starts', default=True)
    def draw(self, context):
        from . import labels
        box = self.layout.box()
        box.prop(self, 'label_visualizer_enabled')
        box.prop(self, 'show_label_options', icon='TRIA_DOWN' if self.show_label_options else 'TRIA_RIGHT', emboss=False)
        if self.show_label_options and context.scene:
            controls = box.column()
            controls.enabled = self.label_visualizer_enabled
            labels.draw_preferences(controls, context)
        self.layout.prop(self, 'check_updates_on_startup')
        draw(self.layout)

class CRP_OT_CheckUpdate(bpy.types.Operator):
    bl_idname = 'crp.check_update'
    bl_label = 'Check for Updates'
    def execute(self, context):
        start('check')
        return {'FINISHED'}

class CRP_OT_InstallUpdate(bpy.types.Operator):
    bl_idname = 'crp.install_update'
    bl_label = 'Install Update'
    bl_description = 'Install verified GitHub release; retain backup and restart Blender afterward'
    @classmethod
    def poll(cls, context):
        return bool(state['release']) and not state['busy'] and not state['restart']
    def execute(self, context):
        start('install')
        return {'FINISHED'}

def draw(layout):
    box = layout.box()
    box.label(text='AssetBridge '+'.'.join(map(str,_version))+' Updates')
    row = box.row()
    row.enabled = not state['busy'] and not state['restart']
    row.operator('crp.check_update', icon='FILE_REFRESH')
    if state['release']:
        row.operator('crp.install_update', icon='IMPORT')
    if state['status']:
        box.label(text=state['status'])

CLASSES = (CRP_UpdatePreferences, CRP_OT_CheckUpdate, CRP_OT_InstallUpdate)
def register(version):
    global _enabled, _version
    _enabled, _version = True, tuple(version)
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    if not bpy.app.background:
        bpy.app.timers.register(poll, first_interval=0.5, persistent=True)
        bpy.app.timers.register(startup_check, first_interval=5)

def unregister():
    global _enabled
    _enabled = False
    for callback in (poll, startup_check):
        if bpy.app.timers.is_registered(callback):
            bpy.app.timers.unregister(callback)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
