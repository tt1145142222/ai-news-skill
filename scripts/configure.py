"""Generate local absolute paths from the public profile; no downloads or login."""
from pathlib import Path
import argparse
import json
import sys

SKILL = Path(__file__).resolve().parents[1]


def make_profile(project_root, delivery_root=None, fallback_root=None):
    root = Path(project_root).expanduser().resolve()
    config = json.loads((SKILL / 'assets/profile.example.json').read_text(encoding='utf-8'))
    executable = 'Scripts/python.exe' if sys.platform == 'win32' else 'bin/python'
    config.update(runtime_root=str(root), output_root=str(root / 'output'),
                  fallback_root=str(Path(fallback_root).expanduser().resolve() if fallback_root else root / 'output-fallback'),
                  python='tools/venv/' + executable,
                  font_serif=str(root / config['font_sans']))
    config['voice']['python'] = 'tools/qwen-tts-venv/' + executable
    config['delivery']['root'] = str(Path(delivery_root).expanduser().resolve() if delivery_root else root / 'deliveries')
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', required=True, help='Your local runtime, models and work root')
    parser.add_argument('--delivery-root', help='Optional separate finished-output root')
    parser.add_argument('--fallback-root', help='Optional separate output root, such as another drive')
    parser.add_argument('--output', default=str(SKILL / 'config/profile.local.json'))
    parser.add_argument('--overwrite', action='store_true')
    args = parser.parse_args()
    target = Path(args.output).expanduser().resolve()
    if target.exists() and not args.overwrite:
        parser.error('Configuration exists; review it or use --overwrite explicitly')
    profile = make_profile(args.project_root, args.delivery_root, args.fallback_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': 'configured', 'profile': str(target)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
