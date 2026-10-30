"""Receive data-only Docker images, validate every part, and safely unpack an archive."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import time


def sha256(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def relative_path(value):
    path = Path(value)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError(f'Unsafe relative path: {value!r}')
    return path


def validate_manifest(data):
    archive = relative_path(data['archive'])
    if len(archive.parts) != 1:
        raise ValueError('Archive must be a basename')
    if not re.fullmatch(r'[0-9a-f]{64}', data['archive_sha256']):
        raise ValueError('Invalid archive SHA256')
    if not isinstance(data['archive_bytes'], int) or data['archive_bytes'] <= 0:
        raise ValueError('Invalid archive size')
    batches = {b['batch'] for b in data['batches']}
    if len(batches) != len(data['batches']):
        raise ValueError('Duplicate batch ids')
    names = []
    for i, part in enumerate(data['parts']):
        name = relative_path(part['name'])
        if len(name.parts) != 1 or part['part'] != i or part['batch'] not in batches:
            raise ValueError('Invalid part ordering or batch')
        if not re.fullmatch(r'[0-9a-f]{64}', part['sha256']) or part['bytes'] <= 0:
            raise ValueError('Invalid part hash or size')
        names.append(part['name'])
    if len(names) != len(set(names)) or sum(p['bytes'] for p in data['parts']) != data['archive_bytes']:
        raise ValueError('Duplicate parts or invalid total size')
    for batch in data['batches']:
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', batch['digest']):
            raise ValueError('Pinned registry digest required')
        if not re.fullmatch(r'[a-z0-9._/-]+:[a-zA-Z0-9_.-]+', batch['image']):
            raise ValueError('Invalid image tag')
    if data.get('checksum_file'):
        relative_path(data['checksum_file'])


def verify_parts(root, parts):
    for part in parts:
        path = root / part['name']
        if not path.is_file() or path.stat().st_size != part['bytes'] or sha256(path) != part['sha256']:
            raise ValueError('Part mismatch: ' + part['name'])


def verify_payload(extracted, checksum_file):
    for line in (extracted / relative_path(checksum_file)).read_text().splitlines():
        expected, name = line.split('  ', 1)
        path = extracted / relative_path(checksum_file).parent / relative_path(name)
        if not re.fullmatch(r'[0-9a-f]{64}', expected) or sha256(path) != expected:
            raise ValueError('Extracted file mismatch: ' + name)


def assemble(data, destination):
    # Revalidation must not leave a previous success marker after a failure.
    (destination / 'TRANSFER-VERIFIED.json').unlink(missing_ok=True)
    verify_parts(destination / 'parts', data['parts'])
    archive = destination / data['archive']
    temporary = archive.with_name(archive.name + '.uploading')
    with temporary.open('wb') as output:
        for part in data['parts']:
            with (destination / 'parts' / part['name']).open('rb') as source:
                shutil.copyfileobj(source, output, length=1024 * 1024)
    if temporary.stat().st_size != data['archive_bytes'] or sha256(temporary) != data['archive_sha256']:
        raise ValueError('Reassembled archive hash or size mismatch')
    temporary.replace(archive)
    staging = destination / 'extracted.uploading'
    final = destination / 'extracted'
    if final.exists():
        if not data.get('checksum_file'):
            raise FileExistsError('Existing extraction has no payload checksum manifest')
        verify_payload(final, data['checksum_file'])
    else:
        staging.mkdir(exist_ok=False)
        with tarfile.open(archive) as bundle:
            bundle.extractall(staging, filter='data')
        if data.get('checksum_file'):
            verify_payload(staging, data['checksum_file'])
        staging.rename(final)
    result = {'status': 'verified', 'archive': str(archive), 'extracted': str(final),
              'archive_bytes': data['archive_bytes'], 'sha256': data['archive_sha256'],
              'inference_test': 'not run'}
    (destination / 'TRANSFER-VERIFIED.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def receive_batch(data, destination, batch, timeout, attempts):
    parts = [p for p in data['parts'] if p['batch'] == batch['batch']]
    try:
        verify_parts(destination / 'parts', parts)
        print('BATCH_CACHED', batch['batch'], flush=True)
        return
    except (OSError, ValueError):
        pass
    # Pin image contents even if the publisher later changes the human-readable tag.
    image = batch['image'].rsplit(':', 1)[0] + '@' + batch['digest']
    log = destination / f'pull-b{batch["batch"]:02d}.log'
    with log.open('a', buffering=1) as handle:
        for attempt in range(1, attempts + 1):
            print('PULL_START', batch['batch'], 'attempt', attempt, flush=True)
            try:
                result = subprocess.run(['docker', 'pull', image], stdout=handle,
                                        stderr=subprocess.STDOUT, timeout=timeout)
            except subprocess.TimeoutExpired:
                continue
            if result.returncode == 0:
                break
        else:
            raise RuntimeError(f'Pull failed; see {log}')
    cid = subprocess.check_output(['docker', 'create', image, '/unused'], text=True).strip()
    try:
        subprocess.run(['docker', 'cp', cid + ':/payload/.', str(destination / 'parts')], check=True)
    finally:
        subprocess.run(['docker', 'rm', cid], check=True)
    verify_parts(destination / 'parts', parts)
    print('BATCH_VERIFIED', batch['batch'], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--dest', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=2, choices=range(1, 5))
    parser.add_argument('--pull-timeout', type=int, default=1200)
    parser.add_argument('--attempts', type=int, default=3)
    parser.add_argument('--verify-only', action='store_true', help='Use already copied parts; no Docker calls')
    args = parser.parse_args()
    data = json.loads(args.manifest.read_text())
    validate_manifest(data)
    destination = args.dest.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    (destination / 'parts').mkdir(exist_ok=True)
    if shutil.disk_usage(destination).free < 4 * data['archive_bytes']:
        raise RuntimeError('Insufficient space for layers, parts, archive and extraction')
    started = time.monotonic()
    if not args.verify_only:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(lambda batch: receive_batch(data, destination, batch,
                                                     args.pull_timeout, args.attempts), data['batches']))
    result = assemble(data, destination)
    result['seconds'] = round(time.monotonic() - started, 3)
    print('TRANSFER_VERIFIED', json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
