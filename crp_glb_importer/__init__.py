bl_info = {
    "name": "CRP-AssetBridge",
    "author": "Aman",
    "version": (3, 7, 0),
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
import urllib.error
import urllib.parse
import urllib.request

import bpy
from bpy.props import BoolProperty, CollectionProperty, IntProperty, PointerProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup, UIList


AUTH_URL = "https://authentication.dev.learningpadedu.com/user/login"
REFRESH_URL = "https://authentication.dev.learningpadedu.com/user/refresh/access/token"
MODELS_URL = "https://content.dev.learningpadedu.com/v2/crp/models"
USER_AGENT = "CRP-AssetBridge/3.7.0"


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


class CRPSettings(PropertyGroup):
    username: StringProperty(name="Username", options={'SKIP_SAVE'})
    password: StringProperty(name="Password", subtype='PASSWORD', options={'SKIP_SAVE'})
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
        default="",
    )
    token: StringProperty(name="Access Token", subtype='PASSWORD', options={'SKIP_SAVE'})
    refresh_token: StringProperty(name="Refresh Token", subtype='PASSWORD', options={'SKIP_SAVE'})
    logged_in: BoolProperty(name="Authenticated", default=False, options={'SKIP_SAVE'})
    auto_refresh: BoolProperty(name="Auto Refresh Token", default=True,
                              description="Automatically renew expired access tokens and retry searches")
    import_search_text: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    enter_separate_enabled: BoolProperty(
        name="Enter to Separate Objects", default=False,
        description="Enable Enter to clear selected objects' parents and move them to the collection shown below",
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
            settings.status = "Imported: {0}".format(filename)
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


class CRP_OT_CollectImportedGLB(Operator):
    bl_idname = "crp.collect_imported_glb"
    bl_label = "Separate Selected Objects"
    bl_description = "Clear selected objects' parents, keep their world transforms, and move them to mesh-<search text>"
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
        context.scene.crp_settings.separate_collection_name = collection.name
        self.report({'INFO'}, 'Separated {0} objects into {1} (transforms kept)'.format(len(selected), collection.name))
        return {'FINISHED'}


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
        auto.label(text="One-click workflow", icon='PLAY')
        auto.operator("crp.auto_workflow", icon='PLAY')
        separate = layout.box()
        separate.prop(settings, 'enter_separate_enabled')
        controls = separate.column()
        controls.enabled = settings.enter_separate_enabled
        controls.prop(settings, 'separate_collection_name')
        controls.label(text="Selected objects: keep transforms")
        controls.operator('crp.collect_imported_glb', icon='OUTLINER_COLLECTION')


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
    CRP_PT_MainPanel,
)


_addon_keymaps = []


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.crp_settings = PointerProperty(type=CRPSettings)
    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig:
        keymap = keyconfig.keymaps.new(name='3D View', space_type='VIEW_3D')
        for key in ('RET', 'NUMPAD_ENTER'):
            item = keymap.keymap_items.new('crp.collect_imported_glb', key, 'PRESS')
            _addon_keymaps.append((keymap, item))


def unregister():
    for keymap, item in _addon_keymaps:
        keymap.keymap_items.remove(item)
    _addon_keymaps.clear()
    if hasattr(bpy.types.Scene, "crp_settings"):
        del bpy.types.Scene.crp_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
