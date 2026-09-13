"""Download official model files, verify hashes, then expose to ComfyUI."""
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent
config_path = ROOT/'config.json'
if not config_path.exists():
    raise SystemExit('Create config.json from config.example.json and set comfy_root first.')
MODELS = Path(json.loads(config_path.read_text())['comfy_root'])/'models'
FILES = [
    ('ByteDance/SDXL-Lightning','sdxl_lightning_4step.safetensors','checkpoints'),
    ('black-forest-labs/FLUX.2-klein-4b-fp8','flux-2-klein-4b-fp8.safetensors','diffusion_models'),
    ('Comfy-Org/flux2-dev','split_files/vae/flux2-vae.safetensors','vae'),
    ('Comfy-Org/flux2-klein-4B','split_files/text_encoders/qwen_3_4b.safetensors','text_encoders'),
]

def install(spec):
    repo, filename, category = spec
    response = requests.get(f'https://huggingface.co/api/models/{repo}?blobs=true',timeout=60)
    response.raise_for_status()
    metadata = response.json()
    entry = next(f for f in metadata['siblings'] if f['rfilename']==filename)
    expected = entry['lfs']['sha256']
    size = entry['size']
    target = MODELS/category/Path(filename).name
    target.parent.mkdir(parents=True,exist_ok=True)
    def digest(path):
        with path.open('rb') as stream:
            return hashlib.file_digest(stream,'sha256').hexdigest()
    if target.exists():
        if target.stat().st_size==size and digest(target)==expected:
            print(f'Already verified: {target.name}',flush=True)
            return dict(file=str(target),sha256=expected,size=size)
        raise ValueError(f'Existing file differs; left untouched: {target}')
    partial = target.with_suffix('.download')
    offset = partial.stat().st_size if partial.exists() else 0
    url = f'https://huggingface.co/{repo}/resolve/{metadata["sha"]}/{filename}'
    print(f'Downloading {target.name}: {size/1e9:.2f} GB; resuming at {offset/size:.0%}',flush=True)
    def part(bounds):
        start,end=bounds
        shard=partial.with_name(partial.name+f'.{start}')
        if shard.exists() and shard.stat().st_size==end-start+1:
            return shard
        for attempt in range(4):
            try:
                present=shard.stat().st_size if shard.exists() else 0
                resume=start+present
                with requests.get(url+f'?part={resume}',stream=True,headers={'Range':f'bytes={resume}-{end}'},timeout=(30,90)) as download:
                    download.raise_for_status()
                    if download.status_code!=206 or download.headers.get('Content-Range','').split('/')[0]!=f'bytes {resume}-{end}':
                        raise ValueError('Server did not honor download range')
                    with shard.open('ab') as stream:
                        for chunk in download.iter_content(1024*1024):
                            stream.write(chunk)
                if shard.stat().st_size!=end-start+1:
                    raise ValueError('Incomplete range')
                return shard
            except Exception:
                if attempt==3:
                    raise
                time.sleep(2)
    chunk_size=64*1024*1024
    ranges=[(start,min(size-1,start+chunk_size-1)) for start in range(offset,size,chunk_size)]
    last=time.monotonic()
    with ThreadPoolExecutor(max_workers=24) as pool, partial.open('ab') as stream:
        for shard in pool.map(part,ranges):
            with shard.open('rb') as source:
                while data:=source.read(8*1024*1024):
                    stream.write(data)
                    offset+=len(data)
            stream.flush()
            shard.unlink()
            if time.monotonic()-last>30:
                print(f'{target.name}: {offset/size:.0%}',flush=True)
                last=time.monotonic()
    if partial.stat().st_size!=size or digest(partial)!=expected:
        raise ValueError(f'Download verification failed: {partial}')
    partial.replace(target)
    print(f'Installed and verified: {target.name}',flush=True)
    return dict(file=str(target),sha256=expected,size=size,source=url)

if __name__=='__main__':
    with ThreadPoolExecutor(max_workers=2) as pool:
        result=list(pool.map(install,FILES))
    (ROOT/'fast-model-install.json').write_text(json.dumps(result,indent=2))
