#!/usr/bin/env python3
"""Build all greyBot distribution components from one explicit source allowlist."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parent.parent


def sources():
    paths = [ROOT / 'control' / 'Dockerfile', ROOT / '.dockerignore',
             ROOT / 'assets' / 'greyBot-avatar-v3.png']
    paths += list((ROOT / 'control').glob('requirements*.txt'))
    paths += list((ROOT / 'control').glob('compose*.yaml'))
    paths += list((ROOT / 'control' / 'greybot_control').glob('*.py'))
    paths += [p for p in (ROOT / 'control' / 'greybot_control' / 'static').iterdir()
              if p.suffix in {'.html', '.css', '.js'}]
    for name in ('discord-mcp', 'discord-local-log'):
        base = ROOT / 'integrations' / name
        paths += [p for p in base.rglob('*') if p.is_file()
                  and not any(part.startswith('.') or part == '__pycache__'
                              for part in p.relative_to(base).parts)
                  and (p.suffix in {'.py', '.md', '.ts', '.tsx', '.plist', '.command'}
                       or p.name == 'LICENSE')]
    return sorted(set(paths))


def main():
    subprocess.run(['bash', str(ROOT / 'scripts' / 'build-lambda.sh')], check=True)
    out = ROOT / 'build' / 'release'
    out.mkdir(parents=True, exist_ok=True)
    bundle = out / 'greybot-sources.tar.gz'
    selected = sources()
    with tarfile.open(bundle, 'w:gz') as archive:
        for path in selected:
            archive.add(path, arcname=str(path.relative_to(ROOT)), recursive=False)
    shutil.make_archive(str(out / 'greybot-lambda'), 'zip', ROOT / 'build' / 'lambda')
    manifest = [{'path': str(p.relative_to(ROOT)),
                 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                for p in selected]
    (out / 'source-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    artifacts = [bundle, out / 'greybot-lambda.zip', out / 'source-manifest.json']
    (out / 'SHA256SUMS').write_text(''.join(
        f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n' for p in artifacts))
    print(f'Release built: {out}')


if __name__ == '__main__':
    main()
