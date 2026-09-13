"""One-at-a-time public dataset installation for the SGO persistent volume."""
import gc
import json
import os
import shutil
import sys
import time
from pathlib import Path

COUNTRIES = {
    'USA': '5b4cd35ab46490c1da1bd2b5a2324d6f871be180',
    'Japan': 'f1f37019d8497143c507b3deb547e65646de2ab7',
    'India': 'adefeefcc0fc3f85726d20bed0e4ed8d66372a54',
    'Singapore': 'a3994709410410f834bd949d643d3f2796908969',
    'Brazil': '441be2bd83a829020452ba9242efd31d212ae602',
    'France': 'ca99b66bb9cd7bc5dc6e5bef0f77ed7bfc443715',
}
def main(country):
    if country not in COUNTRIES:
        raise SystemExit('Unknown country')
    root = Path('/app/data')
    cache = root / '.dataset-download-cache' / country
    os.environ['HF_HOME'] = str(cache / 'hf')
    os.environ['HF_HUB_DISABLE_PROGRESS_BARS'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    os.environ['HF_XET_NUM_CONCURRENT_RANGE_GETS'] = '2'
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'
    sys.path.insert(0, '/app')
    from datasets import load_dataset, load_from_disk, disable_progress_bars
    from huggingface_hub import HfApi
    import pyarrow as pa
    from web.app import dataset_path, validate_dataset_identity, _dataset_is_complete
    pa.set_cpu_count(1)
    pa.set_io_thread_count(2)
    disable_progress_bars()
    repo = f'nvidia/Nemotron-Personas-{country}'
    revision = COUNTRIES[country]
    split = 'en_IN' if country == 'India' else 'train'
    destination = dataset_path(country)
    report = root / 'dataset-installation-status.json'

    def record(stage, **extra):
        state = json.loads(report.read_text()) if report.exists() else {}
        state[country] = dict(repo=repo, revision=revision, split=split, path=str(destination), stage=stage,
                              updated_at=time.time(), **extra)
        tmp = report.with_suffix('.json.next')
        tmp.write_text(json.dumps(state, indent=2)+'\n')
        tmp.replace(report)
        print(json.dumps({'country':country,'stage':stage,**extra}), flush=True)

    try:
        if destination.exists():
            validate_dataset_identity(destination, country)
            if not _dataset_is_complete(destination):
                raise RuntimeError('Existing dataset is incomplete; preserved for inspection')
            dataset = load_from_disk(str(destination))
            record('ready', rows=len(dataset), bytes=sum(p.stat().st_size for p in destination.rglob('*') if p.is_file()))
            raise SystemExit(0)
        info = HfApi().dataset_info(repo, revision=revision, files_metadata=True)
        selected = [f for f in info.siblings if f.rfilename.endswith('.parquet') and
                    (country != 'India' or f.rfilename.startswith('data/en_IN-'))]
        if not selected:
            raise RuntimeError('No matching parquet files')
        compressed = sum(f.size or 0 for f in selected)
        card = info.card_data.to_dict() if info.card_data else {}
        data_info = card.get('dataset_info') or {}
        entries = data_info if isinstance(data_info, list) else [data_info]
        matching_splits = [s for e in entries for s in e.get('splits',[]) if s.get('name') == split]
        expected_rows = matching_splits[0].get('num_examples') if matching_splits else None
        expanded = matching_splits[0].get('num_bytes', compressed*3) if matching_splits else compressed*3
        free = shutil.disk_usage(root).free
        required = compressed + expanded*2 + 6*1024**3
        if free < required:
            raise RuntimeError(f'Insufficient safe headroom: free={free}, required={required}')
        record('downloading', files=len(selected), compressed_bytes=compressed, expected_rows=expected_rows,
               estimated_expanded_bytes=expanded, free_bytes=free)
        # Explicit data_files prevent India from preparing unused Hindi splits.
        dataset = load_dataset(repo, name='default', revision=revision,
                               data_files={split:[f.rfilename for f in selected]}, split=split,
                               cache_dir=str(cache/'prepared'), keep_in_memory=False, writer_batch_size=1000,
                               # India's card lists all languages; this explicit subset has
                               # its own mandatory row-count and identity checks below.
                               verification_mode='no_checks' if country == 'India' else 'basic_checks')
        if expected_rows is not None and len(dataset) != expected_rows:
            raise RuntimeError(f'Row count mismatch: {len(dataset)} != {expected_rows}')
        if not len(dataset):
            raise RuntimeError('Empty dataset')
        staging = root / f'.install-{country}'
        if staging.exists():
            raise RuntimeError('Previous staging directory exists; inspect before retry')
        record('saving', rows=len(dataset))
        dataset.save_to_disk(str(staging), max_shard_size='256MB', num_proc=None)
        validate_dataset_identity(staging, country)
        if not _dataset_is_complete(staging):
            raise RuntimeError('Saved shards are incomplete')
        rows = len(dataset)
        del dataset
        gc.collect()
        # Verify a fresh load and read both ends before publishing.
        check = load_from_disk(str(staging), keep_in_memory=False)
        assert len(check) == rows
        assert isinstance(check[0], dict) and isinstance(check[rows-1], dict)
        del check
        gc.collect()
        staging.replace(destination)
        used = sum(p.stat().st_size for p in destination.rglob('*') if p.is_file())
        # Only this task's country-specific cache is removed; installed data is preserved.
        if cache.exists():
            shutil.rmtree(cache)
        record('ready', rows=rows, bytes=used, free_bytes=shutil.disk_usage(root).free)
    except Exception as error:
        record('failed', error=f'{type(error).__name__}: {error}')
        raise


if __name__ == '__main__':
    main(sys.argv[1])
