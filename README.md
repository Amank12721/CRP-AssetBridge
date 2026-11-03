# CRP-AssetBridge

**Created by Aman.** Blender add-on for finding educational GLB assets in CRP, downloading them, and importing them into the scene. Current version: **3.8.1**. Its Blender name and sidebar tab are **CRP-AssetBridge**. The Python module remains `crp_glb_importer` so existing installations upgrade in place.

## Install

1. In Blender, open **Edit → Preferences → Add-ons → Install from Disk**.
2. Select `CRP-AssetBridge.zip` from this repository and enable **CRP-AssetBridge**.
3. Open **3D Viewport → N sidebar → CRP-AssetBridge**.
4. Select the local `.env` file in **.env File**. Click **Load .env**, then **Get Token**. Use role `internal`. If Blender's file picker hides dotfiles, enable Show Hidden or paste the full `.env` path.

Tested in Blender 4.4.3. No additional Python packages or browser are required.

## Credentials

`.env` is a local credentials file, excluded from Git and the installation ZIP. A configured local copy is supplied on this machine. Other machines should copy `.env.example` to `.env`, then fill in their credentials.

```dotenv
CRP_USERNAME="YOUR_CRP_USERNAME"
CRP_PASSWORD="YOUR_CRP_PASSWORD"
CRP_ROLE="internal"
```

The parser supports blank lines, comments, quoted values and an optional `export` prefix. In double-quoted values, escape a literal backslash as `\\` and a quote as `\"`. Existing two-line ENV.txt files remain readable for compatibility. The add-on hashes plaintext passwords with SHA-256 before login. Credentials and tokens are not saved into `.blend` files; the selected `.env` path may be saved.

Do not force-add `.env` to Git or include it in releases.

## Find and import

### Saved credentials path

Selecting .env saves its absolute path in `crp_assetbridge.json` under Blender's user configuration folder, independently of scene files and Save Preferences. Credentials auto-load on startup and after opening a file, without making a login request. Passwords and tokens are kept only in runtime memory. Select your .env once after upgrading; old scene paths are not trusted automatically. Old .blend files/backups may still contain credentials written by earlier versions: this update cleans loaded authentication fields on the next save, but cannot sanitize other files or backups automatically.

### Export to Asset Library

Select a member of a collection to export the entire collection (including nested members), or select root-level objects to export them individually. In an Outliner operator context, selected collections are also supported. Auto-find uses the custom library selected in an open Asset Browser, or the only configured library. If multiple libraries are ambiguous, select one in the Asset Browser or turn auto-find off and choose an existing folder manually. The resolved path appears in the panel.

GLBs use sanitized collection/object names. Existing files are preserved with numbered suffixes. Selection and active object are restored. Excluded collections must be enabled before export; objects linked to multiple collections require an unambiguous selection. Export writes GLB files into the library folder; it does not create Blender-native Asset Browser entries or catalogs.

- **Find Exact GLB:** enter a filename, with or without `.glb`. Exact matching follows API pagination to the end.
- **Search Description / Audio:** enter a word such as `Beaker`. CRP searches filenames, model names, and audio scripts. The add-on lists unique candidate GLB names from up to 20 pages of 100 records.
- Click a candidate filename once to select it; double-click to download and import it. **Select GLB → Download & Import** is also available.
- **Auto: Login, Find & Import** uses the current session or logs in, then finds and imports the exact GLB.

## Token refresh

**Auto Refresh Token** controls automatic renewal. When enabled, an HTTP 401 during search triggers a refresh-token request, then retries the failed request once. When disabled, refresh manually or log in again. The **Refresh Token** button remains available when a refresh token exists. A rejected refresh token requires a new login.

## Separate objects into a collection

For newly imported assets tracked by this add-on:

1. In Object Mode, select the objects to separate; keep a CRP-imported object active.
2. Enable **Enter to Separate Objects** in the CRP panel. It defaults to OFF.
3. **Collection Name** fills from the active object's original search, for example `mesh-Beaker`. Edit it if desired.
4. With the pointer in the 3D Viewport, press Enter or numpad Enter. Selected objects are detached, renamed to the entered name (Blender adds suffixes such as .001 for multiple objects), and moved into the named collection. Object and object-data animation, NLA and drivers are cleared; shared animated data is copied to protect unselected objects. The selected group's combined bounding-box center moves to world origin (0, 0, 0), preserving relative layout, rotation and scale. Unselected objects keep their collection memberships. Use Undo to reverse the operation.

If another add-on also binds Enter, disable its conflicting shortcut. On this machine, the Model Extractor `crpx.confirm_key` shortcut was disabled in Blender preferences; that machine-specific preference is not bundled in this repository.

## Repository files

- `crp_glb_importer/__init__.py`: add-on source.
- `CRP-AssetBridge.zip`: installable ZIP containing only the add-on source.
- `.env.example`: credential format example with placeholders.
- `.env`: ignored local credentials; not part of Git or the ZIP.
- `CHANGELOG.md`: recent feature history.


## Updates (3.10.0)

Use Check for Updates in the sidebar. Install Update appears when a newer public GitHub release is available. Startup checks can be disabled in add-on preferences. Save your work and restart Blender after installation. See [UPDATES.md](UPDATES.md) for publishing releases.


## Label Visualizer (3.10.3)

LabelCHKAman by Aman is included and enabled by default. Controls are in Edit > Preferences > Add-ons > CRP-AssetBridge > Label Visualizer Settings. Enable Advanced Controls for appearance and timing tools. No extra sidebar panel is registered. Matching label- objects with description/animdata properties show viewport text and editable timeline bars. This does not automatically convert downloaded label JSON into scene objects. Disable any separately installed LabelCHKAman copy to avoid duplicate handlers.
