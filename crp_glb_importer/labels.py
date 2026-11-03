bl_info = {
    "name": "LabelCHKAman - Visualise Labels",
    "author": "Aman",
    "version": (1, 4, 2),
    "blender": (4, 0, 0),
    "location": "3D Viewport > Sidebar > LabelCHK",
    "description": "Draw rounded viewport labels from description and animdata custom properties",
    "category": "3D View",
}

import ast
import math
import re

import bpy
from bpy.app.handlers import persistent
import blf
import gpu
from bpy_extras import view3d_utils
from bpy.props import (
    BoolProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup
from gpu_extras.batch import batch_for_shader


_draw_handle = None
_timeline_handle = None
_bar_hits = {}
_bar_preview = {}
_keymaps = []
_selected_bars = set()


def _visibility_curve(obj):
    animation = obj.animation_data
    if not animation or not animation.action:
        return None
    action = animation.action
    curves = list(getattr(action, 'fcurves', []))
    slot = getattr(animation, 'action_slot', None)
    for layer in getattr(action, 'layers', []):
        for strip in layer.strips:
            if slot and hasattr(strip, 'channelbag'):
                bag = strip.channelbag(slot)
                if bag:
                    curves.extend(bag.fcurves)
    return next((c for c in curves if c.data_path == 'label_chk_visible'), None)


def _enabled(context):
    addon = context.preferences.addons.get(__package__)
    return bool(addon and addon.preferences.label_visualizer_enabled and context.scene and context.scene.crp_label_settings.enabled)


def _draw_timeline_labels():
    context = bpy.context
    if not context.area or context.area.ui_type != 'TIMELINE':
        return
    region = context.region
    if not region or region.type != 'WINDOW':
        return
    settings = context.scene.crp_label_settings
    hits = []
    _bar_hits[region.as_pointer()] = hits
    if not _enabled(context):
        return
    left = region.view2d.region_to_view(0, 0)[0]
    right = region.view2d.region_to_view(region.width, 0)[0]
    old_blend = gpu.state.blend_get()
    gpu.state.blend_set('ALPHA')
    try:
        row = 0
        for obj in sorted(context.scene.objects, key=lambda item: ((_find_label_properties(item) or (item.name, ''))[0].casefold(), item.name)):
            if obj.get('_label_chk_removed', False):
                continue
            if not obj.name.lower().startswith(settings.label_prefix.lower()):
                continue
            values = _find_label_properties(obj)
            if not values:
                continue
            y = 30 + row * 30
            if y + 24 > region.height - 20:
                break
            row += 1
            ranges = []
            if obj.label_chk_use_keys:
                curve = _visibility_curve(obj)
                if curve:
                    boundaries = sorted({float(k.co.x) for k in curve.keyframe_points})
                    for a, b in zip(boundaries, boundaries[1:]):
                        if curve.evaluate((a + b) / 2) >= 0.5:
                            ranges.append((a, b))
                elif obj.label_chk_visible:
                    ranges = [(left, right)]
            elif not settings.use_animdata or not values[1].strip():
                ranges = [(left, right)]
            else:
                for part in re.split(r'[,;]+', values[1]):
                    match = re.fullmatch(r'\s*(-?\d+)(?:\s*-\s*(-?\d+))?\s*', part)
                    if match:
                        a = int(match[1])
                        b = int(match[2]) if match[2] else a
                        ranges.append((min(a, b), max(a, b) + 1))
            ranges = _bar_preview.get(obj.name, ranges)
            for index, (a, b) in enumerate(ranges):
                if b <= left or a >= right:
                    continue
                x1 = region.view2d.view_to_region(max(a, left), 0, clip=False)[0]
                x2 = region.view2d.view_to_region(min(b, right), 0, clip=False)[0]
                color = (0.3, 0.95, 0.4, 1.0) if (obj.name, index) in _selected_bars else (0.05, 0.65, 0.19, 0.8)
                _draw_fill(x1, y, x2, y + 24, color, 3, 4)
                hits.append((x1, x2, y, obj.name, index, list(ranges)))
                for edge in (x1, x2 - 3):
                    _draw_fill(edge, y + 4, edge + 3, y + 20,
                               (0.65, 1.0, 0.7, 1.0), 1, 2)
                blf.size(0, 11)
                blf.color(0, 1, 1, 1, 1)
                blf.position(0, x1 + 4, y + 7, 0)
                blf.draw(0, values[0])
    finally:
        gpu.state.blend_set(old_blend)


@persistent
def _redraw_viewports(*_args):
    wm = bpy.context.window_manager
    if wm is None:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type in {'VIEW_3D', 'DOPESHEET_EDITOR'}:
                area.tag_redraw()


def _set_description(obj, text):
    if obj.get('description') is not None:
        obj['description'] = text
        return
    for name in obj.keys():
        if name == '_RNA_UI':
            continue
        original = obj.get(name)
        value = _convert_value(original)
        if hasattr(value, 'get') and value.get('description') is not None:
            value['description'] = text
            if isinstance(original, str):
                obj[name] = repr(value)
            return
    obj['description'] = text


def _delete_label_and_dot(context, obj):
    # Exact suffix pairing only: label-001 -> dot-001.
    # Do not remove other dots, children, or shared mesh datablocks.
    prefix = context.scene.crp_label_settings.label_prefix
    dot = None
    if prefix and obj.name.lower().startswith(prefix.lower()):
        suffix = obj.name[len(prefix):]
        if suffix:
            dot = context.scene.objects.get('dot-' + suffix)
    if dot is not None and dot != obj:
        bpy.data.objects.remove(dot, do_unlink=True)
    bpy.data.objects.remove(obj, do_unlink=True)


class CRP_LABELCHKAMAN_OT_edit_text(Operator):
    bl_idname = 'screen.crp_label_chk_edit_text'
    bl_label = 'Edit Label Text'
    bl_options = {'REGISTER', 'UNDO'}

    object_name: StringProperty(options={'HIDDEN'})
    text: StringProperty(name='Label Text')
    remove_label: BoolProperty(name='Remove Label', default=False,
                              description='Delete this label object and its matching dot object')

    def invoke(self, context, event):
        if not _enabled(context) or context.area.ui_type != 'TIMELINE' or context.region.type != 'WINDOW':
            return {'PASS_THROUGH'}
        for x1, x2, y, name, index, ranges in reversed(_bar_hits.get(context.region.as_pointer(), [])):
            if x1 <= event.mouse_region_x <= x2 and y <= event.mouse_region_y <= y + 24:
                obj = context.scene.objects.get(name)
                values = _find_label_properties(obj) if obj else None
                if values:
                    self.object_name = name
                    self.text = values[0]
                    self.remove_label = False
                    return context.window_manager.invoke_props_dialog(self, width=320)
        return {'PASS_THROUGH'}

    def draw(self, context):
        row = self.layout.row()
        row.enabled = not self.remove_label
        row.prop(self, 'text')
        self.layout.prop(self, 'remove_label', toggle=True, icon='TRASH')
        if self.remove_label:
            self.layout.label(text='Deletes label + matching dot. Ctrl+Z undoes.')

    def execute(self, context):
        obj = context.scene.objects.get(self.object_name)
        if obj is None:
            self.report({'WARNING'}, 'Label object no longer exists')
            return {'CANCELLED'}
        if self.remove_label:
            name = obj.name
            _delete_label_and_dot(context, obj)
            _bar_preview.pop(name, None)
            _bar_hits.clear()
            self.report({'INFO'}, f'Deleted label object: {name}')
        else:
            _set_description(obj, self.text)
        _redraw_viewports()
        return {'FINISHED'}


class CRP_LABELCHKAMAN_OT_delete_selected(Operator):
    bl_idname = 'screen.crp_label_chk_delete_selected'
    bl_label = 'Delete Selected Label Objects'
    bl_options = {'UNDO'}

    def invoke(self, context, event):
        if (context.area.ui_type != 'TIMELINE'
                or not _enabled(context)
                or not _selected_bars):
            return {'PASS_THROUGH'}
        return self.execute(context)

    def execute(self, context):
        count = 0
        for name in {name for name, _ in _selected_bars}:
            obj = context.scene.objects.get(name)
            if obj is not None and _find_label_properties(obj) is not None:
                _delete_label_and_dot(context, obj)
                count += 1
        _selected_bars.clear()
        _bar_hits.clear()
        _bar_preview.clear()
        _redraw_viewports()
        self.report({'INFO'}, f'Deleted {count} label objects')
        return {'FINISHED'}


class CRP_LABELCHKAMAN_OT_drag_bar(Operator):
    bl_idname = 'screen.crp_label_chk_drag_bar'
    bl_label = 'Move Label Bar'
    bl_options = {'UNDO', 'BLOCKING'}

    def invoke(self, context, event):
        if not _enabled(context) or context.area.ui_type != 'TIMELINE' or context.region.type != 'WINDOW':
            return {'PASS_THROUGH'}
        for x1, x2, y, name, index, ranges in reversed(_bar_hits.get(context.region.as_pointer(), [])):
            if x1 <= event.mouse_region_x <= x2 and y <= event.mouse_region_y <= y + 24:
                target = (name, index)
                if event.shift:
                    if target in _selected_bars:
                        _selected_bars.remove(target)
                    else:
                        _selected_bars.add(target)
                    context.area.tag_redraw()
                    return {'FINISHED'}
                if target not in _selected_bars:
                    _selected_bars.clear()
                    _selected_bars.add(target)
                self.group_original = {}
                self.group_indices = {}
                for _, _, _, other_name, other_index, other_ranges in _bar_hits.get(context.region.as_pointer(), []):
                    if (other_name, other_index) in _selected_bars:
                        self.group_original[other_name] = list(other_ranges)
                        self.group_indices.setdefault(other_name, set()).add(other_index)
                self.object_name = name
                self.index = index
                self.original = ranges
                self.start = context.region.view2d.region_to_view(event.mouse_region_x, 0)[0]
                self.mode = 'MOVE'
                if event.mouse_region_x - x1 < min(7, (x2 - x1) / 3):
                    self.mode = 'START'
                elif x2 - event.mouse_region_x < min(7, (x2 - x1) / 3):
                    self.mode = 'END'
                if len(_selected_bars) > 1:
                    self.mode = 'MOVE'
                context.window_manager.modal_handler_add(self)
                context.area.header_text_set('Drag label timing | Release: confirm | Esc: cancel')
                return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}

    def modal(self, context, event):
        if event.type in {'ESC', 'RIGHTMOUSE'}:
            self.finish(context)
            return {'CANCELLED'}
        if event.type == 'MOUSEMOVE':
            delta = round(context.region.view2d.region_to_view(event.mouse_region_x, 0)[0] - self.start)
            a, b = self.original[self.index]
            if self.mode == 'MOVE':
                a, b = a + delta, b + delta
            elif self.mode == 'START':
                a = min(a + delta, b - 1)
            else:
                b = max(b + delta, a + 1)
            ranges = list(self.original)
            ranges[self.index] = (a, b)
            _bar_preview[self.object_name] = ranges
            if self.mode == 'MOVE':
                for name, original in self.group_original.items():
                    updated = list(original)
                    for index in self.group_indices[name]:
                        start, end = original[index]
                        updated[index] = (start + delta, end + delta)
                    _bar_preview[name] = updated
            context.area.header_text_set(f'Label: {int(a)}–{int(b)-1} | Release: confirm | Esc: cancel')
            context.area.tag_redraw()
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            for name, ranges in list(_bar_preview.items()):
                obj = context.scene.objects.get(name)
                if obj is None:
                    continue
                # Rebuild only this label visibility channel; transforms are untouched.
                merged = []
                for a, b in sorted(ranges):
                    if merged and a <= merged[-1][1]:
                        merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
                    else:
                        merged.append((a, b))
                curve = _visibility_curve(obj)
                if curve:
                    # Removal reallocates the collection: fetch a fresh reference
                    # each time rather than retaining stale RNA wrappers.
                    while len(curve.keyframe_points):
                        curve.keyframe_points.remove(curve.keyframe_points[-1], fast=True)
                    curve.update()
                obj.label_chk_visible = False
                obj.keyframe_insert(data_path='label_chk_visible', frame=merged[0][0]-1, group='Label Visibility')
                for a, b in merged:
                    obj.label_chk_visible = True
                    obj.keyframe_insert(data_path='label_chk_visible', frame=a, group='Label Visibility')
                    obj.label_chk_visible = False
                    obj.keyframe_insert(data_path='label_chk_visible', frame=b, group='Label Visibility')
                curve = _visibility_curve(obj)
                if curve:
                    for key in curve.keyframe_points:
                        key.interpolation = 'CONSTANT'
                    curve.update()
                obj.label_chk_use_keys = True
                context.scene.frame_set(context.scene.frame_current)
            changed = bool(_bar_preview)
            self.finish(context)
            if changed:
                _selected_bars.clear()
            return {'FINISHED'}
        return {'RUNNING_MODAL'}

    def finish(self, context):
        _bar_preview.clear()
        context.area.header_text_set(None)
        _redraw_viewports()


def _convert_value(value):
    if isinstance(value, str):
        try:
            parsed = ast.literal_eval(value.strip())
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, SyntaxError):
            pass
    return value


def _find_label_properties(obj):
    description = obj.get("description")
    animdata = obj.get("animdata", "")
    if description is not None:
        return str(description), str(animdata or "")

    for property_name in obj.keys():
        if property_name == "_RNA_UI":
            continue
        value = _convert_value(obj.get(property_name))
        if isinstance(value, dict) or hasattr(value, "get"):
            description = value.get("description")
            if description is not None:
                return str(description), str(value.get("animdata", "") or "")
    return None


def _frame_is_visible(animdata, current_frame):
    if not animdata or not str(animdata).strip():
        return True

    valid_data = False
    for section in re.split(r"[,;]+", str(animdata)):
        section = section.strip()
        range_match = re.fullmatch(r"(-?\d+)\s*-\s*(-?\d+)", section)
        if range_match:
            valid_data = True
            start, end = int(range_match.group(1)), int(range_match.group(2))
            start, end = min(start, end), max(start, end)
            if start <= current_frame <= end:
                return True
            continue
        if re.fullmatch(r"-?\d+", section):
            valid_data = True
            if current_frame == int(section):
                return True

    return not valid_data


def _rounded_points(x1, y1, x2, y2, radius, segments):
    radius = max(0.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    corners = (
        (x2 - radius, y2 - radius, 0),
        (x1 + radius, y2 - radius, 90),
        (x1 + radius, y1 + radius, 180),
        (x2 - radius, y1 + radius, 270),
    )
    points = []
    for cx, cy, start_angle in corners:
        for index in range(segments + 1):
            angle = math.radians(start_angle + (index / segments) * 90)
            points.append((cx + math.cos(angle) * radius,
                           cy + math.sin(angle) * radius))
    return points


def _draw_fill(x1, y1, x2, y2, color, radius, segments):
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    outer = _rounded_points(x1, y1, x2, y2, radius, segments)
    vertices = [((x1 + x2) / 2, (y1 + y2) / 2)] + outer
    count = len(outer)
    indices = [(0, i + 1, ((i + 1) % count) + 1) for i in range(count)]
    batch = batch_for_shader(shader, "TRIS", {"pos": vertices}, indices=indices)
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _draw_border(x1, y1, x2, y2, color, radius, segments, width):
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    points = _rounded_points(x1, y1, x2, y2, radius, segments)
    points.append(points[0])
    batch = batch_for_shader(shader, "LINE_STRIP", {"pos": points})
    gpu.state.line_width_set(width)
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)
    gpu.state.line_width_set(1.0)


def _draw_labels():
    context = bpy.context
    region = context.region
    rv3d = context.region_data
    scene = context.scene
    if region is None or rv3d is None or scene is None:
        return

    settings = scene.crp_label_settings
    if not _enabled(context):
        return

    gpu.state.blend_set("ALPHA")
    try:
        for obj in scene.objects:
            if obj.get('_label_chk_removed', False):
                continue
            if not obj.name.lower().startswith(settings.label_prefix.lower()):
                continue
            if settings.selected_only and not obj.select_get():
                continue
            if obj.hide_get() or obj.hide_viewport:
                continue

            values = _find_label_properties(obj)
            if values is None:
                continue
            description, animdata = values
            description = description.strip()
            animdata = animdata.strip()
            if not description:
                continue
            if obj.label_chk_use_keys and not obj.label_chk_visible:
                continue
            if not obj.label_chk_use_keys and settings.use_animdata and not _frame_is_visible(animdata, scene.frame_current):
                continue

            text = f"{description} | {animdata}" if settings.show_animdata and animdata else description
            screen = view3d_utils.location_3d_to_region_2d(
                region, rv3d, obj.matrix_world.translation)
            if screen is None:
                continue

            try:
                blf.size(0, settings.font_size)
            except TypeError:
                blf.size(0, settings.font_size, 72)

            text_width, text_height = blf.dimensions(0, text)
            text_x = screen.x - text_width / 2 + settings.offset_x
            text_y = screen.y - text_height / 2 + settings.offset_y
            x1 = text_x - settings.padding_x
            y1 = text_y - settings.padding_y
            x2 = text_x + text_width + settings.padding_x
            y2 = text_y + text_height + settings.padding_y

            if settings.show_background:
                _draw_fill(x1, y1, x2, y2, settings.background_color,
                           settings.corner_radius, settings.round_segments)
            if settings.show_border:
                _draw_border(x1, y1, x2, y2, settings.border_color,
                             settings.corner_radius, settings.round_segments,
                             settings.border_width)

            blf.color(0, *settings.text_color)
            blf.position(0, text_x, text_y, 0)
            blf.draw(0, text)
    finally:
        gpu.state.line_width_set(1.0)
        gpu.state.blend_set("NONE")


def _add_draw_handler():
    global _draw_handle
    if _draw_handle is None:
        _draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_labels, (), "WINDOW", "POST_PIXEL")


def _remove_draw_handler():
    global _draw_handle
    if _draw_handle is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, "WINDOW")
        except Exception:
            pass
        _draw_handle = None


def _setting_updated(_self, _context):
    _redraw_viewports()


class CRP_LABELCHKAMAN_Settings(PropertyGroup):
    enabled: BoolProperty(name="Show Labels", default=True, update=_setting_updated)
    show_advanced: BoolProperty(name="Advanced Controls", default=False)
    label_prefix: StringProperty(name="Object Prefix", default="label-", update=_setting_updated)
    selected_only: BoolProperty(name="Selected Only", default=False, update=_setting_updated)
    use_animdata: BoolProperty(name="Use Animdata Visibility", default=True, update=_setting_updated)
    show_animdata: BoolProperty(name="Show Animdata in Text", default=False, update=_setting_updated)

    font_size: IntProperty(name="Font Size", default=18, min=6, max=100, update=_setting_updated)
    offset_x: FloatProperty(name="Offset X", default=0.0, update=_setting_updated)
    offset_y: FloatProperty(name="Offset Y", default=1.0, update=_setting_updated)
    text_color: FloatVectorProperty(name="Text", subtype='COLOR', size=4,
                                    default=(1.0, 1.0, 1.0, 1.0), min=0.0, max=1.0,
                                    update=_setting_updated)

    show_background: BoolProperty(name="Background", default=True, update=_setting_updated)
    background_color: FloatVectorProperty(name="Background", subtype='COLOR', size=4,
                                          default=(0.02, 0.02, 0.02, 0.85), min=0.0, max=1.0,
                                          update=_setting_updated)
    padding_x: FloatProperty(name="Padding X", default=10.0, min=0.0, max=100.0, update=_setting_updated)
    padding_y: FloatProperty(name="Padding Y", default=6.0, min=0.0, max=100.0, update=_setting_updated)
    corner_radius: FloatProperty(name="Corner Radius", default=5.0, min=0.0, max=50.0, update=_setting_updated)
    round_segments: IntProperty(name="Corner Smoothness", default=5, min=1, max=16, update=_setting_updated)

    show_border: BoolProperty(name="Border", default=True, update=_setting_updated)
    border_width: FloatProperty(name="Border Width", default=1.5, min=1.0, max=10.0, update=_setting_updated)
    border_color: FloatVectorProperty(name="Border", subtype='COLOR', size=4,
                                      default=(1.0, 0.55, 0.05, 1.0), min=0.0, max=1.0,
                                      update=_setting_updated)


class CRP_LABELCHKAMAN_OT_add_properties(Operator):
    bl_idname = "object.crp_label_add_properties"
    bl_label = "Add Label Properties"
    bl_description = "Add description and animdata properties to selected objects"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if not context.selected_objects:
            self.report({'WARNING'}, "Select at least one label object")
            return {'CANCELLED'}
        for obj in context.selected_objects:
            if "description" not in obj:
                obj["description"] = obj.name
            if "animdata" not in obj:
                obj["animdata"] = "1-20"
        _redraw_viewports()
        self.report({'INFO'}, "Label properties added")
        return {'FINISHED'}


class CRP_LABELCHKAMAN_OT_convert(Operator):
    bl_idname = "object.crp_label_chk_convert"
    bl_label = "Animdata to Keyframes (Selected)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        count = 0
        for obj in context.selected_objects:
            if obj.label_chk_use_keys:
                continue  # Preserve already edited timing.
            if not obj.name.lower().startswith(context.scene.crp_label_settings.label_prefix.lower()):
                continue
            values = _find_label_properties(obj)
            if not values:
                continue
            ranges = []
            for part in re.split(r'[,;]+', values[1]):
                match = re.fullmatch(r'\s*(-?\d+)(?:\s*-\s*(-?\d+))?\s*', part)
                if not match:
                    ranges = []
                    break
                a = int(match[1])
                b = int(match[2]) if match[2] else a
                ranges.append((min(a, b), max(a, b)))
            if not ranges:
                continue
            merged = []
            for a, b in sorted(ranges):
                if merged and a <= merged[-1][1] + 1:
                    merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
                else:
                    merged.append((a, b))
            obj.label_chk_visible = False
            obj.keyframe_insert(data_path='label_chk_visible', frame=merged[0][0] - 1, group='Label Visibility')
            for a, b in merged:
                obj.label_chk_visible = True
                obj.keyframe_insert(data_path='label_chk_visible', frame=a, group='Label Visibility')
                obj.label_chk_visible = False
                obj.keyframe_insert(data_path='label_chk_visible', frame=b + 1, group='Label Visibility')
            obj.label_chk_use_keys = True
            count += 1
        context.scene.frame_set(context.scene.frame_current)
        self.report({'INFO'}, f'{count} labels converted; existing keyframe labels skipped')
        return {'FINISHED'}


class CRP_LABELCHKAMAN_OT_timeline(Operator):
    bl_idname = 'screen.crp_label_chk_timeline'
    bl_label = 'Open Label Dope Sheet'

    def execute(self, context):
        area = next((a for a in context.screen.areas if a.type == 'DOPESHEET_EDITOR'), None)
        if area is None:
            self.report({'WARNING'}, 'Change an editor to Dope Sheet, then click again')
            return {'CANCELLED'}
        area.type = 'DOPESHEET_EDITOR'
        space = area.spaces.active
        space.mode = 'DOPESHEET'
        space.dopesheet.show_only_selected = False
        space.dopesheet.filter_text = context.scene.crp_label_settings.label_prefix
        area.tag_redraw()
        return {'FINISHED'}


def draw_preferences(layout, context):
    settings = context.scene.crp_label_settings

    layout.prop(settings, "enabled")
    layout.prop(
        settings,
        "show_advanced",
        icon='DISCLOSURE_TRI_DOWN' if settings.show_advanced else 'DISCLOSURE_TRI_RIGHT',
    )

    if settings.show_advanced:
        box = layout.box()
        box.label(text='Label Timeline')
        box.operator('object.crp_label_chk_convert')
        box.operator('screen.crp_label_chk_timeline')
        obj = context.active_object
        if obj:
            box.prop(obj, 'label_chk_use_keys')
            if obj.label_chk_use_keys:
                box.prop(obj, 'label_chk_visible')
                box.label(text='Hover Visible, press I to keyframe')
        layout.operator(CRP_LABELCHKAMAN_OT_add_properties.bl_idname, icon='ADD')

        box = layout.box()
        box.label(text="Objects & Animation")
        box.prop(settings, "label_prefix")
        box.prop(settings, "selected_only")
        box.prop(settings, "use_animdata")
        box.prop(settings, "show_animdata")

        box = layout.box()
        box.label(text="Text")
        box.prop(settings, "font_size")
        row = box.row(align=True)
        row.prop(settings, "offset_x")
        row.prop(settings, "offset_y")
        box.prop(settings, "text_color")

        box = layout.box()
        box.prop(settings, "show_background")
        if settings.show_background:
            box.prop(settings, "background_color")
            row = box.row(align=True)
            row.prop(settings, "padding_x")
            row.prop(settings, "padding_y")
            box.prop(settings, "corner_radius")
            box.prop(settings, "round_segments")

        box = layout.box()
        box.prop(settings, "show_border")
        if settings.show_border:
            box.prop(settings, "border_width")
            box.prop(settings, "border_color")


classes = (
    CRP_LABELCHKAMAN_OT_delete_selected,
    CRP_LABELCHKAMAN_OT_edit_text,
    CRP_LABELCHKAMAN_OT_drag_bar,
    CRP_LABELCHKAMAN_Settings,
    CRP_LABELCHKAMAN_OT_add_properties,
    CRP_LABELCHKAMAN_OT_convert,
    CRP_LABELCHKAMAN_OT_timeline,
)


def register():
    global _timeline_handle
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.crp_label_settings = PointerProperty(type=CRP_LABELCHKAMAN_Settings)
    bpy.types.Object.label_chk_use_keys = BoolProperty(name='Use Label Keyframes', default=False, update=_setting_updated)
    bpy.types.Object.label_chk_visible = BoolProperty(name='Label Visible', default=True, update=_setting_updated)
    _add_draw_handler()
    _timeline_handle = bpy.types.SpaceDopeSheetEditor.draw_handler_add(
        _draw_timeline_labels, (), 'WINDOW', 'POST_PIXEL')
    config = bpy.context.window_manager.keyconfigs.addon
    if config:
        km = config.keymaps.new(name='Dopesheet', space_type='DOPESHEET_EDITOR')
        item = km.keymap_items.new('screen.crp_label_chk_delete_selected', 'DEL', 'PRESS', head=True)
        _keymaps.append((km, item))
        item = km.keymap_items.new('screen.crp_label_chk_drag_bar', 'LEFTMOUSE', 'PRESS', head=True)
        _keymaps.append((km, item))
        item = km.keymap_items.new('screen.crp_label_chk_drag_bar', 'LEFTMOUSE', 'PRESS', shift=True, head=True)
        _keymaps.append((km, item))
        item = km.keymap_items.new('screen.crp_label_chk_edit_text', 'LEFTMOUSE', 'DOUBLE_CLICK', head=True)
        _keymaps.append((km, item))
    if _redraw_viewports not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(_redraw_viewports)
    _redraw_viewports()


def unregister():
    for km, item in _keymaps:
        km.keymap_items.remove(item)
    _keymaps.clear()
    _selected_bars.clear()
    _bar_hits.clear()
    _bar_preview.clear()
    global _timeline_handle
    if _timeline_handle is not None:
        bpy.types.SpaceDopeSheetEditor.draw_handler_remove(_timeline_handle, 'WINDOW')
        _timeline_handle = None
    _remove_draw_handler()
    if _redraw_viewports in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(_redraw_viewports)
    if hasattr(bpy.types.Scene, "crp_label_settings"):
        del bpy.types.Scene.crp_label_settings
    del bpy.types.Object.label_chk_use_keys
    del bpy.types.Object.label_chk_visible
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()

