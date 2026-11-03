## 3.10.3

- Bundle LabelCHKAman 1.4.2 viewport labels and timeline editing.
- Enable Label Visualizer by default; expose its settings only in AssetBridge Preferences.
- No separate LabelCHK sidebar panel; preserve existing label visibility animation paths.

## 3.10.1

- Add a visible "Update test successful - v3.10.1" panel label to verify the release updater.

## 3.10.0

- Add automatic startup update checks and Check for Updates / Install Update buttons.
- Verify GitHub release ZIP checksums, paths, version and Blender compatibility.
- Keep previous add-on backup, roll back failed replacement, and require restart after updates.
- Add tag-triggered GitHub release workflow and packaging script.

# Changelog

## 3.9.1
- Optional Save Camera Animation JSON checkbox fetches the selected model's dynamicCameraFile signed URL into the saved blend folder, without changing scene cameras.
- Tested with 32 camera keyframes at 24 FPS, missing camera metadata, default-off behavior, and unique export names in Blender 5.2.3.

## 3.9.0
- Added separate default-off audio and label JSON checkboxes for the selected GLB import.
- Audio files are stored in CRP_Audio beside the blend and added as Sequencer sound strips at the current frame. Additional language alternatives are muted.
- Label JSON is saved beside the blend. Save the blend before enabling optional extras.

## 3.8.1
- Auto-load .env on startup and file load; persist only its absolute path in Blender's user configuration.
- Keep credentials and tokens in runtime memory, purge legacy stored authentication fields on load/save, and clean up timers on disable.
- Export complete selected collections and root-level objects as GLBs; restore selection and active object even on failure.
- Sanitize filenames, prevent directory traversal, and use numbered suffixes rather than overwrite existing exports.
- Resolve the custom library selected in an open Asset Browser, or the sole configured library; require manual selection when ambiguous.
- Validate manual paths without silent fallback. Rebuilt ZIP and tested in Blender 5.2.3 LTS.

## 3.8.0
- Enter preparation renames selected objects using the editable collection name.
- Clears selected object/data animation, including shape keys, while protecting shared data.
- Centers the selected group at world origin while preserving relative layout, rotation and scale.
- Verified these changes in Blender 5.2.3 LTS.

## 3.7.0
- Renamed the Blender add-on and sidebar tab to CRP-AssetBridge.
- Added Aman as author and the visible Created by Aman credit.
- Standardized configuration on .env with CRP_USERNAME, CRP_PASSWORD, CRP_ROLE.
- Improved credential parsing for quoted values, comments, export, and legacy passwords containing equals signs.

## 3.6.0
- Added an Enter-to-separate checkbox, disabled by default.
- Added an editable destination collection name, populated when the checkbox is enabled.

## 3.5.2
- Separate all selected objects while preserving world transforms.

## 3.5.1
- Clear the selected object's parent and move it to its own collection.

## 3.5.0
- Added automatic token refresh on/off control and imported-object tracking.

## 3.4.1
- Double-click a candidate GLB filename to import it.

## 3.4.0
- Refresh expired access tokens and retry catalog requests once.
- Added manual refresh.

## 3.3.1
- Display server-side search candidates without incorrectly filtering missing audio metadata from list responses.
