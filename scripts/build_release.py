"""Run from any directory; no .env or local assets are packaged."""
import ast
import hashlib
from pathlib import Path
import sys
import zipfile

root = Path(__file__).resolve().parents[1]
addon = root / 'crp_glb_importer'
tree = ast.parse((addon / '__init__.py').read_text(encoding='utf-8'))
info = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='bl_info' for t in n.targets))
version = '.'.join(map(str,info['version']))
if len(sys.argv)>1 and sys.argv[1].removeprefix('v') != version:
    raise SystemExit('Tag must match bl_info version '+version)
archive = root / 'CRP-AssetBridge.zip'
with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
    for p in sorted(addon.rglob('*.py')):
        if '__pycache__' in p.parts:
            continue
        compile(p.read_bytes(),str(p),'exec')
        z.write(p,p.relative_to(root).as_posix())
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
(root / 'CRP-AssetBridge.zip.sha256').write_text(digest+'  CRP-AssetBridge.zip\n',encoding='ascii')
print('Built',version,archive.name)
