"""Split a tar archive into small data-only image layers and publish a manifest."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--repository', required=True, help='Docker Hub account/repository')
    parser.add_argument('--tag-prefix', required=True, help='Unique release name; avoid reusing tags')
    parser.add_argument('--work-dir', type=Path, required=True, help='New staging folder outside Git')
    parser.add_argument('--manifest-out', type=Path, required=True)
    parser.add_argument('--checksum-file', help='Path inside archive to a sha256sum checksum file')
    parser.add_argument('--part-mib', type=int, default=2, choices=range(1, 9))
    parser.add_argument('--parts-per-image', type=int, default=60, choices=range(1, 101))
    parser.add_argument('--workers', type=int, default=2, choices=range(1, 5))
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z0-9._-]+/[a-z0-9._/-]+', args.repository):
        raise ValueError('Expected Docker Hub account/repository')
    if not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,100}', args.tag_prefix):
        raise ValueError('Invalid tag prefix')
    root = args.work_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    parts = []
    h = hashlib.sha256()
    size = 0
    with args.archive.open('rb') as source:
        while chunk := source.read(args.part_mib * 1024 * 1024):
            i = len(parts)
            batch = i // args.parts_per_image
            folder = root / f'batch-{batch:02d}'
            folder.mkdir(exist_ok=True)
            name = f'archive.part-{i:06d}'
            (folder / name).write_bytes(chunk)
            h.update(chunk)
            size += len(chunk)
            parts.append({'part': i, 'batch': batch, 'name': name, 'bytes': len(chunk),
                          'sha256': hashlib.sha256(chunk).hexdigest()})
    if not parts:
        raise ValueError('Empty input archive')
    batches = []
    for i in range(parts[-1]['batch'] + 1):
        group = [p for p in parts if p['batch'] == i]
        folder = root / f'batch-{i:02d}'
        (folder / 'Dockerfile').write_text('FROM scratch\n' + ''.join(
            'COPY ' + p['name'] + ' /payload/' + p['name'] + '\n' for p in group))
        (folder / '.dockerignore').write_text('*\n!Dockerfile\n!.dockerignore\n!archive.part-*\n')
        batches.append({'batch': i, 'image': f'{args.repository}:{args.tag_prefix}-b{i:02d}',
                        'parts': len(group), 'bytes': sum(p['bytes'] for p in group)})

    def publish(batch):
        folder = root / f'batch-{batch["batch"]:02d}'
        with (folder / 'publish.log').open('w') as log:
            subprocess.run(['docker', 'buildx', 'build', '--platform', 'linux/amd64',
                            '--provenance=false', '--load', '-t', batch['image'], str(folder)],
                           check=True, stdout=log, stderr=subprocess.STDOUT)
            result = subprocess.run(['docker', 'push', batch['image']], check=True,
                                    capture_output=True, text=True)
            log.write(result.stdout + result.stderr)
        digest = re.search(r'digest: (sha256:[0-9a-f]{64})', result.stdout + result.stderr)
        if not digest:
            raise RuntimeError('Push did not return a registry digest')
        receipt = {**batch, 'digest': digest.group(1)}
        (folder / 'published.json').write_text(json.dumps(receipt, indent=2) + '\n')
        print('PUBLISHED', batch['image'], receipt['digest'], flush=True)
        return receipt

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        receipts = list(pool.map(publish, batches))
    manifest = {'archive': args.archive.name, 'archive_bytes': size, 'archive_sha256': h.hexdigest(),
                'parts': parts, 'batches': receipts}
    if args.checksum_file:
        manifest['checksum_file'] = args.checksum_file
    args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
    args.manifest_out.write_text(json.dumps(manifest, indent=2) + '\n')
    print('MANIFEST_READY', args.manifest_out, flush=True)


if __name__ == '__main__':
    main()
