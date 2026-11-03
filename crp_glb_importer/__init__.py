bl_info = {
    "name": "CRP-AssetBridge",
    "author": "Aman",
    "version": (3, 10, 3),
    "blender": (3, 6, 0),
    "location": "3D Viewport > Sidebar > CRP-AssetBridge",
    "description": "Find, import and organize CRP educational GLB assets. Created by Aman.",
    "category": "Import-Export",
}

import json
import hashlib
import os
import tempfile
import time
import uuid
import re
from bpy.app.handlers import persistent
import urllib.error
import urllib.parse
import urllib.request

import bpy
from bpy.props import BoolProperty, CollectionProperty, IntProperty, PointerProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup, UIList
from mathutils import Vector
from . import updater, labels


AUTH_URL = "https://authentication.dev.learningpadedu.com/user/login"
REFRESH_URL = "https://authentication.dev.learningpadedu.com/user/refresh/access/token"
MODELS_URL = "https://content.dev.learningpadedu.com/v2/crp/models"
USER_AGENT = "CRP-AssetBridge/3.10.3"


class CRPHTTPError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__("HTTP {0}: CRP request failed".format(status))


def _request_json(url, *, method="GET", headers=None, form=None, json_body=None, timeout=30):
    request_headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    request_headers.update(headers or {})
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if form is not None:
        body = urllib.parse.urlencode(form).encode("utf-8")
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise CRPHTTPError(exc.code) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("Network error: {0}".format(exc.reason)) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Server returned an invalid JSON response") from exc


def _find_value(value, wanted_keys):
    """Find a named value in common nested API response shapes."""
    if isinstance(value, dict):
        for key in wanted_keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for child in value.values():
            found = _find_value(child, wanted_keys)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_value(child, wanted_keys)
            if found:
                return found
    return ""


def _auth_headers(token):
    return {"Authorization": "Bearer " + token}


def _clear_auth(settings):
    settings.token = ""
    settings.refresh_token = ""
    settings.logged_in = False


def _refresh_access_token(settings):
    if not settings.refresh_token:
        raise RuntimeError("No refresh token available. Log in again.")
    try:
        _, payload = _request_json(REFRESH_URL, method="POST", json_body={},
                                   headers=_auth_headers(settings.refresh_token))
    except CRPHTTPError as exc:
        if exc.status in (401, 403):
            _clear_auth(settings)
            raise RuntimeError("Refresh token expired or rejected. Log in again.") from exc
        raise
    token = _find_value(payload, ("access_token", "accessToken"))
    if not token:
        raise RuntimeError("Refresh response did not include an access token")
    settings.token = token
    rotated = _find_value(payload, ("refresh_token", "refreshToken"))
    if rotated:
        settings.refresh_token = rotated
    settings.logged_in = True
    settings.status = "Access token refreshed"


def _catalog_request(settings, url, timeout=45):
    try:
        return _request_json(url, headers=_auth_headers(settings.token), timeout=timeout)
    except CRPHTTPError as exc:
        if exc.status != 401:
            raise
        if not settings.auto_refresh:
            settings.logged_in = False
            settings.token = ""
            raise RuntimeError("Access token expired. Auto-refresh is off; refresh manually or log in.") from exc
        if not settings.refresh_token:
            _clear_auth(settings)
            raise RuntimeError("Session expired. Log in again.") from exc
    _refresh_access_token(settings)
    try:
        return _request_json(url, headers=_auth_headers(settings.token), timeout=timeout)
    except CRPHTTPError as exc:
        if exc.status == 401:
            _clear_auth(settings)
            raise RuntimeError("Session rejected after refresh. Log in again.") from exc
        raise


def _safe_glb_name(name):
    name = os.path.basename((name or "model.glb").strip())
    if not name.lower().endswith(".glb"):
        name += ".glb"
    return name


class CRPAudioResult(PropertyGroup):
    filename: StringProperty(name="GLB Filename")
    search_text: StringProperty(options={'SKIP_SAVE'})
    model_json: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})


def _separation_toggle_update(settings, context):
    if settings.enter_separate_enabled:
        obj = context.active_object
        label = (obj.get('crp_search_text') if obj else None) or settings.import_search_text or settings.audio_query or settings.glb_filename or 'Imported GLB'
        settings.separate_collection_name = 'mesh-' + (' '.join(str(label).split()) or 'Imported GLB')



# Sensitive values live only in Python memory, never in Scene ID properties.
_session = {}
_env_path = None

def _runtime_get(key, default=""):
    return lambda self: _session.get(key, default)

def _runtime_set(key):
    return lambda self, value: _session.__setitem__(key, value)

def _config_path():
    return os.path.join(bpy.utils.user_resource('CONFIG', create=True), 'crp_assetbridge.json')

def _get_env_path(self):
    global _env_path
    if _env_path is None:
        try:
            with open(_config_path(), encoding='utf-8') as handle:
                _env_path = str(json.load(handle).get('env_file', ''))
        except (OSError, ValueError, TypeError):
            _env_path = ''
    return _env_path

def _set_env_path(self, value):
    global _env_path
    path = os.path.abspath(bpy.path.abspath(value)) if value.strip() else ''
    destination = _config_path()
    temporary = destination + '.tmp'
    try:
        with open(temporary, 'w', encoding='utf-8') as handle:
            json.dump({'env_file': path}, handle)
        os.replace(temporary, destination)
        _env_path = path
    except OSError:
        self.status = 'Could not save .env path; check Blender config permissions'
        raise

def _purge_legacy_secrets():
    for scene in getattr(bpy.data, 'scenes', ()):
        settings = scene.crp_settings
        for key in ('username', 'password', 'token', 'refresh_token', 'logged_in', 'env_file'):
            if key in settings:
                del settings[key]

@persistent
def _on_load(_):
    _session.clear()
    _purge_legacy_secrets()
    if not bpy.app.timers.is_registered(_auto_load_env):
        bpy.app.timers.register(_auto_load_env, first_interval=0.5)

@persistent
def _before_save(_):
    _purge_legacy_secrets()


class CRPSettings(PropertyGroup):
    fetch_camera: BoolProperty(name="Save Camera Animation JSON", default=False,
        description="Fetch selected model camera animation JSON beside the saved blend; does not alter scene cameras")
    fetch_audio: BoolProperty(name="Import Audio to Timeline", default=False,
        description="Download selected model audio and add sound strips at the current frame; save the blend first")
    fetch_labels: BoolProperty(name="Save Label JSON beside .blend", default=False,
        description="Save the selected model's labelData beside the saved blend file")
    username: StringProperty(name="Username", options={'SKIP_SAVE'}, get=_runtime_get('username'), set=_runtime_set('username'))
    password: StringProperty(name="Password", subtype='PASSWORD', options={'SKIP_SAVE'}, get=_runtime_get('password'), set=_runtime_set('password'))
    role: StringProperty(
        name="Role",
        description="Role expected by the CRP login API (normally internal)",
        default="internal",
        options={'SKIP_SAVE'},
    )
    env_file: StringProperty(
        name=".env File",
        subtype='FILE_PATH',
        description="Select .env containing CRP_USERNAME, CRP_PASSWORD and CRP_ROLE",
        default="", get=_get_env_path, set=_set_env_path,
    )  # Saved independently of .blend files; contains only the path
    token: StringProperty(name="Access Token", subtype='PASSWORD', options={'SKIP_SAVE'}, get=_runtime_get('token'), set=_runtime_set('token'))
    refresh_token: StringProperty(name="Refresh Token", subtype='PASSWORD', options={'SKIP_SAVE'}, get=_runtime_get('refresh_token'), set=_runtime_set('refresh_token'))
    logged_in: BoolProperty(name="Authenticated", default=False, options={'SKIP_SAVE'}, get=_runtime_get('logged_in', False), set=_runtime_set('logged_in'))
    auto_refresh: BoolProperty(name="Auto Refresh Token", default=True,
                              description="Automatically renew expired access tokens and retry searches")
    import_search_text: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    enter_separate_enabled: BoolProperty(
        name="Enter to Separate Objects", default=False,
        description="Enable Enter to detach, rename, clear animation and center selected objects in the collection below",
        update=_separation_toggle_update,
    )
    separate_collection_name: StringProperty(
        name="Collection Name", default="mesh-Imported GLB",
        description="Name to use when separating selected objects; edit before pressing Enter",
    )
    glb_filename: StringProperty(
        name="GLB Filename",
        description="Exact filename, including or omitting the .glb extension",
        default="",
    )
    audio_query: StringProperty(
        name="Audio Script Search",
        description="Search words in CRP meta_data.audio[].audio_script (1–100 characters)",
        default="",
    )
    audio_results: CollectionProperty(type=CRPAudioResult, options={'SKIP_SAVE'})
    audio_result_index: IntProperty(default=0, options={'SKIP_SAVE'})
    result_json: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    status: StringProperty(name="Status", default="Not authenticated", options={'SKIP_SAVE'})
    download_directory: StringProperty(
        name="Download Folder",
        subtype='DIR_PATH',
        description="Leave blank to use Blender's temporary folder",
        default="",
    )
    asset_library_path: StringProperty(
        name="Asset Library Path",
        subtype='DIR_PATH',
        description="Path to Asset Library folder for GLB export. Leave blank to auto-detect",
        default="",
    )
    auto_find_asset_lib: BoolProperty(
        name="Auto-find Asset Library",
        default=True,
        description="Use the custom library selected in an open Asset Browser, or the only configured library",
    )


class CRP_OT_Login(Operator):
    bl_idname = "crp.login"
    bl_label = "Get Token"
    bl_description = "Log in to CRP and obtain the access token inside Blender"

    def execute(self, context):
        settings = context.scene.crp_settings
        if not settings.username.strip() or not settings.password:
            self.report({'ERROR'}, "Enter both username and password")
            return {'CANCELLED'}

        settings.status = "Authenticating..."
        try:
            password_value = settings.password
            if not (len(password_value) == 64 and all(ch in "0123456789abcdefABCDEF" for ch in password_value)):
                password_value = hashlib.sha256(password_value.encode("utf-8")).hexdigest()
            _, payload = _request_json(
                AUTH_URL,
                method="POST",
                form={
                    "username": settings.username.strip(),
                    "password": password_value,
                    "role": settings.role.strip() or "internal",
                },
            )
            token = _find_value(payload, ("access_token", "accessToken", "token"))
            if not token:
                raise RuntimeError("Login succeeded but no access token was present in the response")
            settings.token = token
            settings.refresh_token = _find_value(payload, ("refresh_token", "refreshToken"))
            settings.password = ""
            settings.logged_in = True
            settings.result_json = ""
            settings.status = "Authenticated"
            self.report({'INFO'}, "CRP token received")
            return {'FINISHED'}
        except Exception as exc:
            _clear_auth(settings)
            settings.status = "Login failed"
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


def _read_env_credentials(path):
    with open(path, 'r', encoding='utf-8-sig') as handle:
        lines = [line.strip() for line in handle if line.strip() and not line.lstrip().startswith('#')]
    values, plain = {}, []
    known = {'username', 'user', 'crp_username', 'password', 'pass', 'crp_password', 'role', 'crp_role'}
    for line in lines:
        candidate = line[7:].lstrip() if line.startswith('export ') else line
        key, separator, value = candidate.partition('=')
        if separator and key.strip().lower() in known:
            value = value.strip()
            if value.startswith('"'):
                try:
                    value, end = json.JSONDecoder().raw_decode(value)
                    trailing = candidate.partition('=')[2].strip()[end:].strip()
                    if not isinstance(value, str) or (trailing and not trailing.startswith('#')):
                        raise ValueError()
                except ValueError:
                    raise RuntimeError('Invalid quoted value in .env; check quotes and escapes') from None
            elif value.startswith("'"):
                end = value.find("'", 1)
                if end < 0 or (value[end+1:].strip() and not value[end+1:].strip().startswith('#')):
                    raise RuntimeError('Invalid single-quoted value in .env')
                value = value[1:end]
            else:
                value = value.split(' #', 1)[0].rstrip()
            values[key.strip().lower()] = value
        else:
            plain.append(line)
    username = values.get('crp_username') or values.get('username') or values.get('user')
    password = values.get('crp_password') or values.get('password') or values.get('pass')
    if not values and len(plain) == 2:
        username, password = plain
    if not username or not password:
        raise RuntimeError('.env must contain CRP_USERNAME and CRP_PASSWORD')
    return username, password, values.get('crp_role') or values.get('role') or 'internal'


class CRP_OT_LoadEnv(Operator):
    bl_idname = "crp.load_env"
    bl_label = "Load .env"
    bl_description = "Load CRP_USERNAME, CRP_PASSWORD and CRP_ROLE from the selected .env file"

    def execute(self, context):
        settings = context.scene.crp_settings
        path = bpy.path.abspath(settings.env_file).strip()
        if not path or not os.path.isfile(path):
            self.report({'ERROR'}, "Choose a valid .env file")
            return {'CANCELLED'}
        try:
            username, password, role = _read_env_credentials(path)

            settings.username = username
            settings.password = password
            if role:
                settings.role = role
            settings.status = ".env credentials loaded"
            self.report({'INFO'}, ".env credentials loaded")
            return {'FINISHED'}
        except Exception as exc:
            settings.status = "ENV import failed"
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

class CRP_OT_UseToken(Operator):
    bl_idname = "crp.use_token"
    bl_label = "Use Existing Token"
    bl_description = "Use the token entered above without performing a login"

    def execute(self, context):
        settings = context.scene.crp_settings
        token = settings.token.strip()
        if not token:
            self.report({'ERROR'}, "Enter an access token")
            return {'CANCELLED'}
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        settings.token = token
        settings.logged_in = True
        settings.status = "Token ready"
        return {'FINISHED'}


class CRP_OT_RefreshToken(Operator):
    bl_idname = "crp.refresh_token"
    bl_label = "Refresh Token"
    bl_description = "Get a new access token using the current refresh token"

    def execute(self, context):
        try:
            _refresh_access_token(context.scene.crp_settings)
            self.report({'INFO'}, "Access token refreshed")
            return {'FINISHED'}
        except Exception as exc:
            context.scene.crp_settings.status = str(exc)
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class CRP_OT_Logout(Operator):
    bl_idname = "crp.logout"
    bl_label = "Clear Session"

    def execute(self, context):
        settings = context.scene.crp_settings
        settings.password = ""
        settings.token = ""
        settings.refresh_token = ""
        settings.result_json = ""
        settings.logged_in = False
        settings.status = "Session cleared"
        return {'FINISHED'}


class CRP_OT_Search(Operator):
    bl_idname = "crp.search_glb"
    bl_label = "Find Exact GLB"
    bl_description = "Search the CRP catalog for this exact GLB filename"

    def execute(self, context):
        settings = context.scene.crp_settings
        if not settings.logged_in or not settings.token:
            self.report({'ERROR'}, "Get or enter a token first")
            return {'CANCELLED'}
        target = _safe_glb_name(settings.glb_filename)
        if target == "model.glb" and not settings.glb_filename.strip():
            self.report({'ERROR'}, "Enter a GLB filename")
            return {'CANCELLED'}

        wm = context.window_manager
        wm.progress_begin(0, 1)
        settings.result_json = ""
        exact_without_url = None
        try:
            page = 1
            while True:
                settings.status = "Searching matching results, page {0}".format(page)
                query = urllib.parse.urlencode({"search": target, "page": page, "limit": 100})
                _, payload = _catalog_request(
                    settings,
                    MODELS_URL + "?" + query,
                    timeout=45,
                )
                models = payload.get("data", []) if isinstance(payload, dict) else []
                if isinstance(models, dict):
                    models = models.get("models", models.get("items", []))
                for model in models if isinstance(models, list) else []:
                    current = str(model.get("file_name", ""))
                    if current.casefold() == target.casefold():
                        if model.get("download_url") or model.get("signed_url"):
                            settings.result_json = json.dumps(model)
                            settings.import_search_text = settings.glb_filename.strip()
                            settings.status = "Found on page {0}".format(page)
                            self.report({'INFO'}, "Found {0}".format(current))
                            return {'FINISHED'}
                        exact_without_url = current

                pagination = payload.get("pagination", {}) if isinstance(payload, dict) else {}
                total_pages = pagination.get("total_pages")
                if isinstance(total_pages, int) and not isinstance(total_pages, bool):
                    wm.progress_update(min(page / max(total_pages, 1), 1))
                    if page >= total_pages:
                        break
                elif pagination.get("has_next") is False:
                    break
                if not models:
                    break
                if not pagination and len(models) == 100:
                    raise RuntimeError("CRP did not return pagination; search cannot continue reliably")
                page += 1

            if exact_without_url:
                settings.status = "Matched filename, but no download URL"
                self.report({'WARNING'}, "CRP has {0}, but its records have no download URL".format(target))
                return {'CANCELLED'}
            settings.status = "No exact match found"
            self.report({'WARNING'}, "No exact filename match for {0} in CRP search results".format(target))
            return {'CANCELLED'}
        except Exception as exc:
            if "HTTP 401" in str(exc):
                settings.logged_in = False
                settings.token = ""
                settings.status = "Token expired or rejected"
            else:
                settings.status = "Search failed"
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        finally:
            wm.progress_end()


_last_result_click = (None, 0.0)


def _result_double_clicked(key, now, interval, explicit_double=False):
    global _last_result_click
    previous_key, previous_time = _last_result_click
    double = explicit_double or (key == previous_key and 0 <= now - previous_time <= interval)
    _last_result_click = (None, 0.0) if double else (key, now)
    return double


class CRP_OT_ResultClick(Operator):
    bl_idname = "crp.result_click"
    bl_label = "Select / Import GLB"
    bl_description = "Click to select this GLB; double-click to download and import it"
    bl_options = {'UNDO'}

    index: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def invoke(self, context, event):
        settings = context.scene.crp_settings
        if not 0 <= self.index < len(settings.audio_results):
            return {'CANCELLED'}
        settings.audio_result_index = self.index
        bpy.ops.crp.select_audio_result()
        key = (context.scene.as_pointer(), self.index, settings.audio_results[self.index].filename)
        interval = getattr(context.preferences.inputs, 'mouse_double_click_time', 350) / 1000.0
        if event.type == 'LEFTMOUSE' and _result_double_clicked(
                key, time.monotonic(), interval, event.value == 'DOUBLE_CLICK'):
            return self.execute(context)
        return {'FINISHED'}

    def execute(self, context):
        settings = context.scene.crp_settings
        if not 0 <= self.index < len(settings.audio_results):
            self.report({'ERROR'}, "Select a GLB from the list")
            return {'CANCELLED'}
        settings.audio_result_index = self.index
        if 'FINISHED' not in bpy.ops.crp.select_audio_result():
            return {'CANCELLED'}
        return bpy.ops.crp.download_import()


class CRP_UL_AudioResults(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.operator_context = 'INVOKE_DEFAULT'
        button = layout.operator('crp.result_click', text=item.filename, icon='FILE_3D', emboss=False)
        button.index = index


class CRP_OT_SearchAudio(Operator):
    bl_idname = "crp.search_audio"
    bl_label = "Search Description / Audio"
    bl_description = "List GLB candidates returned by CRP search across filename, model name, and audio script (up to 20 pages)"

    def execute(self, context):
        settings = context.scene.crp_settings
        query_text = settings.audio_query.strip()
        if not settings.logged_in or not settings.token:
            self.report({'ERROR'}, "Get a token first")
            return {'CANCELLED'}
        if not 1 <= len(query_text) <= 100:
            self.report({'ERROR'}, "Enter 1–100 characters of search text")
            return {'CANCELLED'}

        settings.audio_results.clear()
        settings.result_json = ""
        seen = set()
        try:
            for page in range(1, 21):
                settings.status = "Searching audio scripts, page {0}/20".format(page)
                query = urllib.parse.urlencode({"search": query_text, "page": page, "limit": 100})
                _, payload = _catalog_request(settings, MODELS_URL + "?" + query)
                models = payload.get("data", []) if isinstance(payload, dict) else []
                if isinstance(models, dict):
                    models = models.get("models", models.get("items", []))
                if not isinstance(models, list):
                    raise RuntimeError("Unexpected model list from CRP")
                for model in models:
                    if not isinstance(model, dict):
                        continue
                    filename = str(model.get("file_name") or "").strip()
                    if not filename.lower().endswith(".glb") or filename.casefold() in seen:
                        continue
                    seen.add(filename.casefold())
                    item = settings.audio_results.add()
                    item.filename = filename
                    item.search_text = query_text
                    item.model_json = json.dumps(model)

                pagination = payload.get("pagination", {}) if isinstance(payload, dict) else {}
                if not isinstance(pagination, dict):
                    pagination = {}
                total_pages = pagination.get("total_pages")
                if (not models or pagination.get("has_next") is False or
                        (isinstance(total_pages, int) and page >= total_pages)):
                    break
            settings.audio_result_index = 0
            count = len(settings.audio_results)
            settings.status = "Found {0} candidate GLB names (up to 20 pages)".format(count)
            self.report({'INFO'}, settings.status)
            return {'FINISHED'}
        except Exception as exc:
            settings.status = "Audio search failed"
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class CRP_OT_SelectAudioResult(Operator):
    bl_idname = "crp.select_audio_result"
    bl_label = "Select GLB"

    def execute(self, context):
        settings = context.scene.crp_settings
        index = settings.audio_result_index
        if index < 0 or index >= len(settings.audio_results):
            self.report({'ERROR'}, "Select a GLB from the list")
            return {'CANCELLED'}
        item = settings.audio_results[index]
        settings.glb_filename = item.filename
        settings.result_json = item.model_json
        settings.import_search_text = item.search_text or item.filename
        settings.status = "Selected: " + item.filename
        return {'FINISHED'}


def _model_details(settings, model_id):
    for attempt in range(2):
        try:
            _, payload = _request_json(MODELS_URL + '/get', method='POST',
                headers=_auth_headers(settings.token), json_body={'model_id': model_id})
            data = payload.get('data')
            if not isinstance(data, dict):
                raise RuntimeError('Model detail response has no data')
            if data.get('model_id') != model_id:
                raise RuntimeError('Model detail ID does not match selected GLB')
            return data
        except CRPHTTPError as exc:
            if exc.status != 401 or attempt or not settings.auto_refresh or not settings.refresh_token:
                raise
            _refresh_access_token(settings)


def _import_model_extras(context, settings, model):
    if not (settings.fetch_audio or settings.fetch_labels or settings.fetch_camera):
        return [], []
    details = _model_details(settings, model['model_id'])
    metadata = details.get('meta_data') or {}
    folder = os.path.dirname(bpy.data.filepath)
    stem = _export_filename(os.path.splitext(model.get('file_name') or 'model')[0])
    messages, warnings = [], []
    if settings.fetch_camera:
        camera = metadata.get('dynamicCameraFile')
        url = camera.get('signed_url') if isinstance(camera, dict) else None
        if not url:
            warnings.append('No camera animation download URL for selected GLB')
        else:
            try:
                if urllib.parse.urlsplit(url).scheme != 'https':
                    raise ValueError('Camera download requires HTTPS')
                request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
                with urllib.request.urlopen(request, timeout=60) as response:
                    raw = response.read(20 * 1024 * 1024 + 1)
                if len(raw) > 20 * 1024 * 1024:
                    raise ValueError('Camera JSON exceeds 20 MB limit')
                document = json.loads(raw.decode('utf-8-sig'))
                if not isinstance(document, (dict, list)):
                    raise ValueError('Invalid camera JSON structure')
                path = os.path.join(folder, stem + '_camera_' + uuid.uuid4().hex[:8] + '.json')
                with open(path, 'x', encoding='utf-8') as handle:
                    json.dump(document, handle, ensure_ascii=False, indent=2)
                messages.append('Camera animation JSON saved: ' + os.path.basename(path))
            except Exception:
                warnings.append('Camera JSON fetch failed; check session and folder permissions')
    if settings.fetch_labels:
        labels = metadata.get('labelData')
        if isinstance(labels, list) and labels:
            # Unique suffix preserves earlier exports; never store signed URLs or tokens.
            path = os.path.join(folder, stem + '_labels_' + uuid.uuid4().hex[:8] + '.json')
            with open(path, 'x', encoding='utf-8') as handle:
                json.dump({'model_id': model['model_id'], 'file_name': model.get('file_name'),
                           'labelData': labels}, handle, ensure_ascii=False, indent=2)
            messages.append('Label JSON saved: ' + os.path.basename(path))
        else:
            warnings.append('No label data for selected GLB')
    if settings.fetch_audio:
        audios = metadata.get('audio') or []
        if not isinstance(audios, list):
            audios = []
        usable = [a for a in audios if isinstance(a, dict) and a.get('signed_url')]
        if not usable:
            warnings.append('No audio download URL for selected GLB')
        for index, audio in enumerate(usable):
            path = None
            try:
                url = audio['signed_url']
                if urllib.parse.urlsplit(url).scheme != 'https':
                    raise RuntimeError('Audio requires HTTPS')
                suffix = os.path.splitext(urllib.parse.urlsplit(url).path)[1].lower()
                if suffix not in {'.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac'}:
                    raise RuntimeError('Unsupported audio format')
                audio_folder = os.path.join(folder, 'CRP_Audio')
                os.makedirs(audio_folder, exist_ok=True)
                name = stem + '_' + _export_filename(str(audio.get('lang', 'audio'))) + '_' + uuid.uuid4().hex[:8]
                path = os.path.join(audio_folder, name + suffix)
                with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': USER_AGENT}), timeout=60) as response, open(path, 'xb') as handle:
                    count = 0
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        count += len(chunk)
                        if count > 100 * 1024 * 1024:
                            raise RuntimeError('Audio exceeds 100 MB limit')
                        handle.write(chunk)
                editor = context.scene.sequence_editor_create()
                strips = getattr(editor, 'strips', None)
                if strips is None:
                    strips = editor.sequences
                channel = max((strip.channel for strip in strips), default=0) + 1
                strip = strips.new_sound(name=stem + ' [' + str(audio.get('lang', index + 1)) + ']',
                    filepath=path, channel=channel, frame_start=context.scene.frame_current)
                strip.sound.filepath = bpy.path.relpath(path)
                strip['crp_model_id'] = model['model_id']
                # Multiple languages are alternatives, not simultaneous narration.
                strip.mute = index > 0
                messages.append('Audio added: ' + str(audio.get('lang', index + 1)))
            except Exception:
                if path and os.path.isfile(path):
                    os.remove(path)
                warnings.append('Audio could not be downloaded/added; retry import with a fresh session')
    return messages, warnings


class CRP_OT_DownloadImport(Operator):
    bl_idname = "crp.download_import"
    bl_label = "Download & Import"
    bl_description = "Download the selected GLB, validate it, and import it into the scene"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.scene.crp_settings
        if not settings.result_json:
            self.report({'ERROR'}, "Search for a model first")
            return {'CANCELLED'}
        if (settings.fetch_audio or settings.fetch_labels or settings.fetch_camera) and not bpy.data.filepath:
            self.report({'ERROR'}, "Save the .blend file first, or disable the audio/JSON ticks")
            return {'CANCELLED'}
        try:
            model = json.loads(settings.result_json)
            url = model.get("download_url") or model.get("signed_url")
            if not url:
                raise RuntimeError("The model record has no download URL")
            filename = _safe_glb_name(model.get("file_name"))
            destination_dir = bpy.path.abspath(settings.download_directory).strip()
            if not destination_dir:
                destination_dir = tempfile.gettempdir()
            os.makedirs(destination_dir, exist_ok=True)
            destination = os.path.join(destination_dir, filename)

            settings.status = "Downloading {0}".format(filename)
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=120) as response, open(destination, "wb") as output:
                    header = response.read(4)
                    if header != b"glTF":
                        raise RuntimeError("Downloaded data is not a valid binary GLB")
                    output.write(header)
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
            except Exception:
                if os.path.exists(destination):
                    os.remove(destination)
                raise

            settings.status = "Importing {0}".format(filename)
            previous_objects = set(bpy.data.objects)
            outcome = bpy.ops.import_scene.gltf(filepath=destination)
            if 'FINISHED' not in outcome:
                raise RuntimeError("Blender did not finish importing the GLB")
            import_id = uuid.uuid4().hex
            search_text = settings.import_search_text or filename
            for obj in set(bpy.data.objects) - previous_objects:
                obj['crp_import_id'] = import_id
                obj['crp_search_text'] = search_text
            warnings = []
            try:
                messages, warnings = _import_model_extras(context, settings, model)
                for message in messages:
                    self.report({'INFO'}, message)
            except Exception:
                warnings = ['GLB imported, but optional audio/JSON fetch failed; check session and folder permissions']
            for warning in warnings:
                self.report({'WARNING'}, warning)
            settings.status = "Imported: {0}".format(filename)
            if warnings:
                settings.status += ' (audio/JSON warning: ' + warnings[0] + ')'
            self.report({'INFO'}, "Imported {0}".format(filename))
            return {'FINISHED'}
        except Exception as exc:
            settings.status = "Import failed"
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class CRP_OT_AutoWorkflow(Operator):
    bl_idname = "crp.auto_workflow"
    bl_label = "Auto: Login, Find & Import"
    bl_description = "Use the current session or log in, automatically refresh expired access, and import the exact GLB"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.scene.crp_settings
        try:
            if not settings.glb_filename.strip():
                self.report({'ERROR'}, "Enter the exact GLB filename")
                return {'CANCELLED'}

            if not settings.logged_in or not settings.token:
                if settings.refresh_token and settings.auto_refresh:
                    _refresh_access_token(settings)
                else:
                    if settings.env_file and 'FINISHED' not in bpy.ops.crp.load_env():
                        return {'CANCELLED'}
                    if 'FINISHED' not in bpy.ops.crp.login():
                        return {'CANCELLED'}
            if 'FINISHED' not in bpy.ops.crp.search_glb():
                return {'CANCELLED'}
            if 'FINISHED' not in bpy.ops.crp.download_import():
                return {'CANCELLED'}
            self.report({'INFO'}, "Automatic CRP import completed")
            return {'FINISHED'}
        except Exception as exc:
            settings.status = "Automatic workflow failed"
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


def _selection_center(objects):
    points = []
    for obj in objects:
        if obj.type in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}:
            points.extend(obj.matrix_world @ Vector(corner) for corner in obj.bound_box)
    if not points:
        points = [obj.matrix_world.translation.copy() for obj in objects]
    return Vector(tuple((min(p[i] for p in points) + max(p[i] for p in points)) * .5 for i in range(3)))


class CRP_OT_CollectImportedGLB(Operator):
    bl_idname = "crp.collect_imported_glb"
    bl_label = "Separate Selected Objects"
    bl_description = "Detach selected objects, rename them, clear their animation and center the group at world origin"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (context.mode == 'OBJECT' and obj is not None and
                obj in context.selected_objects and bool(obj.get('crp_import_id')) and
                context.scene.crp_settings.enter_separate_enabled)

    def execute(self, context):
        active = context.active_object
        name = context.scene.crp_settings.separate_collection_name.strip()
        if not name:
            self.report({'ERROR'}, "Enter a collection name first")
            return {'CANCELLED'}
        selected = list(context.selected_objects)
        transforms = {obj: obj.matrix_world.copy() for obj in selected}
        # Keep unselected children in place when their selected parents are centered.
        outside_children = {child: child.matrix_world.copy() for obj in selected
                            for child in obj.children if child not in selected}
        detached_id = active.get('crp_detached_id') or uuid.uuid4().hex
        collection = next((c for c in context.scene.collection.children_recursive
                           if c.get('crp_detached_id') == detached_id
                           and c.name == name
                           and set(c.objects) == set(selected)), None)
        if collection is None:
            detached_id = uuid.uuid4().hex
            collection = bpy.data.collections.new(name)
            collection['crp_detached_id'] = detached_id
            context.scene.collection.children.link(collection)
        for obj in selected:
            obj.animation_data_clear()
            data = getattr(obj, 'data', None)
            if data is not None:
                keys = getattr(data, 'shape_keys', None)
                if getattr(data, 'animation_data', None) or (keys and keys.animation_data):
                    if data.users > 1:
                        obj.data = data.copy()
                        data = obj.data
                    data.animation_data_clear()
                    keys = getattr(data, 'shape_keys', None)
                    if keys:
                        keys.animation_data_clear()
            obj.parent = None
            obj.matrix_parent_inverse.identity()
        for obj in selected:
            obj.matrix_world = transforms[obj]
            obj['crp_detached_id'] = detached_id
            if collection not in obj.users_collection:
                collection.objects.link(obj)
            for old_collection in list(obj.users_collection):
                if old_collection != collection:
                    old_collection.objects.unlink(obj)
        context.view_layer.update()
        center = _selection_center(selected)
        for obj in selected:
            matrix = obj.matrix_world.copy()
            matrix.translation -= center
            obj.matrix_world = matrix
            if obj.name != name:
                obj.name = name
        context.view_layer.update()
        for child, matrix in outside_children.items():
            child.matrix_world = matrix
        context.view_layer.update()
        context.scene.crp_settings.separate_collection_name = collection.name
        self.report({'INFO'}, 'Prepared {0} objects in {1}: renamed, animation cleared, centered'.format(len(selected), collection.name))
        return {'FINISHED'}


def _get_asset_library_path(settings):
    if not settings.auto_find_asset_lib:
        path = bpy.path.abspath(settings.asset_library_path).strip()
        return path if path and os.path.isdir(path) else None
    libraries = bpy.context.preferences.filepaths.asset_libraries
    valid = {lib.name: bpy.path.abspath(lib.path) for lib in libraries
             if lib.path and os.path.isdir(bpy.path.abspath(lib.path))}
    # Resolve an explicitly selected custom library in an open Asset Browser.
    active = set()
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'FILE_BROWSER':
                params = getattr(area.spaces.active, 'params', None)
                reference = getattr(params, 'asset_library_reference', '')
                if reference in valid:
                    active.add(valid[reference])
    if len(active) == 1:
        return active.pop()
    if not active and len(valid) == 1:
        return next(iter(valid.values()))
    return None  # Multiple libraries are ambiguous; never silently pick the first.


def _export_filename(name):
    stem = re.sub(r'[<>:"/\\\\|?*\x00-\x1f]', '_', name).strip(' .')[:150] or 'Asset'
    if stem.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM%d' % i for i in range(1, 10)), *('LPT%d' % i for i in range(1, 10))}:
        stem = '_' + stem
    return stem


def _reserve_export_path(directory, name):
    root = os.path.realpath(directory)
    stem = _export_filename(name)
    index = 0
    while True:
        suffix = '' if index == 0 else '_%03d' % index
        path = os.path.join(root, stem + suffix + '.glb')
        if os.path.commonpath([root, os.path.realpath(path)]) != root:
            raise RuntimeError('Export filename escapes the library folder')
        try:
            with open(path, 'xb'):
                pass
            return path
        except FileExistsError:
            index += 1


def _export_groups(context):
    selected_ids = getattr(context, 'selected_ids', ())
    collections = {item for item in selected_ids if isinstance(item, bpy.types.Collection)}
    scene_collections = set(context.scene.collection.children_recursive)
    standalone = []
    for obj in context.selected_objects:
        owners = set(obj.users_collection) & scene_collections
        if len(owners) > 1:
            raise RuntimeError('Object belongs to multiple collections: ' + obj.name)
        if owners:
            collections.update(owners)
        else:
            standalone.append(obj)
    # A selected ancestor already includes its nested collections.
    collections = {c for c in collections if not any(c in parent.children_recursive for parent in collections if parent != c)}
    covered = {obj for c in collections for obj in c.all_objects}
    return ([(c.name, list(c.all_objects)) for c in sorted(collections, key=lambda c: c.name)]
            + [(obj.name, [obj]) for obj in standalone if obj not in covered])


class CRP_OT_ExportToAssetLibrary(Operator):
    bl_idname = "crp.export_to_asset_library"
    bl_label = "Export to Asset Library"
    bl_description = "Export complete collections and root-level objects as GLBs; existing files are never overwritten"

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and (bool(context.selected_objects) or
            any(isinstance(item, bpy.types.Collection) for item in getattr(context, 'selected_ids', ())))

    def execute(self, context):
        settings = context.scene.crp_settings
        directory = _get_asset_library_path(settings)
        if not directory:
            self.report({'ERROR'}, "Choose a valid manual path, or select one custom library in an Asset Browser")
            return {'CANCELLED'}
        original = list(context.selected_objects)
        active = context.view_layer.objects.active
        states = {}
        count = 0
        try:
            groups = _export_groups(context)
            if not groups:
                raise RuntimeError('No collections or objects selected')
            for name, objects in groups:
                if not objects:
                    raise RuntimeError('Empty collection: ' + name)
                for obj in objects:
                    if obj.name not in context.view_layer.objects:
                        raise RuntimeError('Enable excluded collection before exporting: ' + obj.name)
                    states[obj] = (obj.hide_get(), obj.hide_viewport, obj.hide_select)
            for obj in states:
                obj.hide_viewport = False
                obj.hide_select = False
                obj.hide_set(False)
            for name, objects in groups:
                for obj in context.view_layer.objects:
                    obj.select_set(False)
                for obj in objects:
                    obj.select_set(True)
                if set(context.selected_objects) != set(objects):
                    raise RuntimeError('Collection contains unavailable objects; enable its visibility first')
                context.view_layer.objects.active = objects[0]
                path = _reserve_export_path(directory, name)
                try:
                    result = bpy.ops.export_scene.gltf(
                        filepath=path, use_selection=True, export_format='GLB',
                        export_materials='EXPORT', export_cameras=False, export_lights=False)
                    if 'FINISHED' not in result:
                        raise RuntimeError('Blender cancelled GLB export')
                    with open(path, 'rb') as handle:
                        if handle.read(4) != b'glTF':
                            raise RuntimeError('Exporter did not produce a valid GLB')
                except Exception:
                    os.remove(path)  # Only the new file exclusively reserved by this operation.
                    raise
                count += 1
            settings.status = 'Exported %d GLB(s) to %s' % (count, directory)
            self.report({'INFO'}, settings.status)
            return {'FINISHED'}
        except Exception as exc:
            settings.status = 'Export stopped after %d file(s): %s' % (count, exc)
            self.report({'ERROR'}, settings.status)
            return {'CANCELLED'}
        finally:
            for obj in context.view_layer.objects:
                obj.select_set(False)
            for obj, state in states.items():
                obj.hide_set(state[0])
                obj.hide_viewport, obj.hide_select = state[1:]
            for obj in original:
                obj.select_set(True)
            context.view_layer.objects.active = active


class CRP_PT_MainPanel(Panel):
    bl_label = "CRP-AssetBridge"
    bl_idname = "CRP_PT_glb_importer"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'CRP-AssetBridge'

    def draw(self, context):
        layout = self.layout
        settings = context.scene.crp_settings

        layout.label(text="Created by Aman")
        layout.label(text="Update test successful - v3.10.1", icon='CHECKMARK')
        auth = layout.box()
        auth.label(text="1. Authentication", icon='LOCKED' if not settings.logged_in else 'UNLOCKED')
        auth.prop(settings, 'auto_refresh')
        if not settings.logged_in:
            auth.prop(settings, "env_file")
            auth.operator("crp.load_env", icon='FILE_FOLDER')
            auth.separator()
            auth.prop(settings, "username")
            auth.prop(settings, "password")
            auth.prop(settings, "role")
            auth.operator("crp.login", icon='KEY_HLT')
            auth.separator()
            auth.label(text="Optional fallback token:")
            auth.prop(settings, "token", text="Token")
            auth.operator("crp.use_token", icon='CHECKMARK')
        else:
            auth.label(text="Token is ready", icon='CHECKMARK')
            auth.operator("crp.logout", icon='X')
        if settings.refresh_token:
            auth.operator("crp.refresh_token", icon='FILE_REFRESH')

        search = layout.box()
        search.enabled = settings.logged_in
        search.label(text="2. Find Model", icon='VIEWZOOM')
        search.prop(settings, "glb_filename")
        search.operator("crp.search_glb", icon='ZOOM_IN')

        audio_search = layout.box()
        audio_search.enabled = settings.logged_in
        audio_search.label(text="Search description / audio text", icon='SOUND')
        audio_search.prop(settings, "audio_query")
        audio_search.operator("crp.search_audio", icon='VIEWZOOM')
        if settings.audio_results:
            audio_search.label(text="Candidate GLB files: {0}".format(len(settings.audio_results)))
            audio_search.label(text="Double-click a filename to import")
            audio_search.template_list("CRP_UL_AudioResults", "", settings,
                                       "audio_results", settings, "audio_result_index", rows=6)
            audio_search.operator("crp.select_audio_result", icon='CHECKMARK')

        if settings.result_json:
            try:
                model = json.loads(settings.result_json)
                result = layout.box()
                result.label(text="3. Result", icon='MESH_DATA')
                result.label(text=str(model.get("model_name", "Unnamed model"))[:60])
                result.label(text=str(model.get("file_name", "unknown.glb"))[:60], icon='FILE_3D')
                result.prop(settings, "download_directory")
                result.operator("crp.download_import", icon='IMPORT')
            except Exception:
                pass

        status = layout.box()
        status.label(text=settings.status[:100], icon='INFO')

        auto = layout.box()
        auto.label(text="Optional extras for selected GLB")
        auto.prop(settings, 'fetch_audio')
        auto.prop(settings, 'fetch_labels')
        auto.prop(settings, 'fetch_camera')
        if settings.fetch_audio or settings.fetch_labels or settings.fetch_camera:
            auto.label(text="Save .blend first; audio starts at current frame")
        auto.label(text="One-click workflow", icon='PLAY')
        auto.operator("crp.auto_workflow", icon='PLAY')
        
        separate = layout.box()
        separate.prop(settings, 'enter_separate_enabled')
        controls = separate.column()
        controls.enabled = settings.enter_separate_enabled
        controls.prop(settings, 'separate_collection_name')
        controls.label(text="Rename, clear animation, center group")
        controls.operator('crp.collect_imported_glb', icon='OUTLINER_COLLECTION')
        
        export_box = layout.box()
        export_box.label(text="Export to Asset Library", icon='ASSET_MANAGER')
        export_box.prop(settings, 'auto_find_asset_lib')
        if settings.auto_find_asset_lib:
            # Show which path is auto-detected
            asset_path = _get_asset_library_path(settings)
            if asset_path:
                export_box.label(text=f"Using: {asset_path}", icon='FOLDER_REDIRECT')
            else:
                export_box.label(text="Choose one library in Asset Browser or use manual", icon='ERROR')
        else:
            export_box.prop(settings, 'asset_library_path')
        export_box.operator('crp.export_to_asset_library', icon='EXPORT')
        export_box.label(text="Exports whole collections; existing files get a suffix", icon='INFO')


CLASSES = (
    CRPAudioResult,
    CRPSettings,
    CRP_OT_LoadEnv,
    CRP_OT_Login,
    CRP_OT_UseToken,
    CRP_OT_RefreshToken,
    CRP_OT_Logout,
    CRP_OT_Search,
    CRP_OT_ResultClick,
    CRP_UL_AudioResults,
    CRP_OT_SearchAudio,
    CRP_OT_SelectAudioResult,
    CRP_OT_DownloadImport,
    CRP_OT_AutoWorkflow,
    CRP_OT_CollectImportedGLB,
    CRP_OT_ExportToAssetLibrary,
    CRP_PT_MainPanel,
)


_addon_keymaps = []


def _auto_load_env():
    """Automatically load .env credentials on startup if a path is saved."""
    try:
        import bpy
        if not hasattr(bpy.context, 'scene') or bpy.context.scene is None:
            return
        settings = bpy.context.scene.crp_settings
        _purge_legacy_secrets()
        if not settings.env_file or settings.logged_in:
            return
        path = bpy.path.abspath(settings.env_file).strip()
        if path and os.path.isfile(path):
            username, password, role = _read_env_credentials(path)
            settings.username = username
            settings.password = password
            if role:
                settings.role = role
            settings.status = ".env auto-loaded from saved path"
    except Exception:
        if bpy.context.scene:
            bpy.context.scene.crp_settings.status = ".env auto-load failed; check the saved path and file format"


def register():
    labels.register()
    updater.register(bl_info["version"])
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.crp_settings = PointerProperty(type=CRPSettings)
    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig:
        keymap = keyconfig.keymaps.new(name='3D View', space_type='VIEW_3D')
        for key in ('RET', 'NUMPAD_ENTER'):
            item = keymap.keymap_items.new('crp.collect_imported_glb', key, 'PRESS')
            _addon_keymaps.append((keymap, item))
    _purge_legacy_secrets()
    bpy.app.handlers.load_post.append(_on_load)
    bpy.app.handlers.save_pre.append(_before_save)
    # Auto-load .env after a short delay to ensure context is ready
    bpy.app.timers.register(_auto_load_env, first_interval=0.5)


def unregister():
    updater.unregister()
    labels.unregister()
    if bpy.app.timers.is_registered(_auto_load_env):
        bpy.app.timers.unregister(_auto_load_env)
    for handlers, callback in ((bpy.app.handlers.load_post, _on_load), (bpy.app.handlers.save_pre, _before_save)):
        if callback in handlers:
            handlers.remove(callback)
    _session.clear()
    for keymap, item in _addon_keymaps:
        keymap.keymap_items.remove(item)
    _addon_keymaps.clear()
    if hasattr(bpy.types.Scene, "crp_settings"):
        del bpy.types.Scene.crp_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
