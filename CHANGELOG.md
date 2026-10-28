# Changelog

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
