#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
docker run --rm --network none --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$root/quest,target=/src" --entrypoint bash \
  sawyer-quest-builder:openxr-1.1.36 -lc 'sha256sum -c vendor/SHA256SUMS && bash build-in-container'
cp "$root/quest/build/sawyer-quest-input-120hz.apk" "$root/quest/sawyer-quest-input-120hz.apk"
python3 - "$root/quest" <<'PY'
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1])
path = root / 'apk.json'
manifest = json.loads(path.read_text())
manifest['sha256'] = hashlib.sha256((root / manifest['apk']).read_bytes()).hexdigest()
path.write_text(json.dumps(manifest, indent=2) + '\n')
PY
