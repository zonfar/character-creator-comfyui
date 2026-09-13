"""Local ComfyUI rendering, aligned artwork preparation, and Adobe PSD export."""
import io
import hashlib
import json
import subprocess
import time
import threading
import uuid
from pathlib import Path

import numpy as np
import requests
import websocket
from PIL import Image, ImageChops, ImageDraw
from psd_tools import PSDImage
from psd_tools.api.layers import Group, PixelLayer

ROOT = Path(__file__).resolve().parent
PROJECT_SAVE_LOCK = threading.RLock()
VIEWS = ['Frontal', 'Left Quarter', 'Right Quarter', 'Left Profile', 'Right Profile']
SECTIONS = {'Frontal': 'Front', 'Left Quarter': 'Quarter', 'Left Profile': 'Side'}
VISEMES = {
    'Neutral': 'relaxed closed lips, resting expression',
    'Ah': 'wide open mouth saying ah, tongue low',
    'D': 'slightly open mouth, tongue touching upper teeth',
    'Ee': 'wide stretched lips saying ee, teeth visible',
    'F': 'upper teeth touching lower lip saying f',
    'L': 'open mouth with tongue tip touching upper teeth saying l',
    'M': 'firmly closed lips saying m or b or p',
    'Oh': 'large rounded open lips saying oh',
    'R': 'partly rounded lips saying r',
    'S': 'nearly closed teeth saying s, lips slightly apart',
    'Uh': 'medium open relaxed mouth saying uh',
    'W-Oo': 'small tightly rounded protruding lips saying oo',
    'Smile': 'closed smiling lips',
    'Surprised': 'round open mouth in surprise',
}


def save(project, folder):
    with PROJECT_SAVE_LOCK:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        temp = folder / 'project.tmp'
        temp.write_text(json.dumps(project, indent=2), encoding='utf-8')
        temp.replace(folder / 'project.json')


def new_project(name, description, mode='visemes', views=None, size=(1024, 1024)):
    if mode not in ('visemes', 'jaw'):
        raise ValueError('Choose visemes or jaw.')
    views = views or list(SECTIONS)
    if not views or 'Frontal' not in views or any(v not in VIEWS for v in views):
        raise ValueError('Views must include Frontal and use Adobe view names.')
    if any(n < 256 or n > 4096 or n % 64 for n in size):
        raise ValueError('Canvas dimensions must be multiples of 64 from 256 to 4096.')
    project = dict(version=1, name=name, description=description, mode=mode,
                   views=views, size=list(size), seed=123456, assets=[])
    def add(view, path, instruction, kind='part', visible=True):
        project['assets'].append(dict(id=f'a{len(project["assets"]):03d}', view=view,
            path=path, instruction=instruction, kind=kind, visible=visible,
            file=None, approved=False, x=0, y=0, scale=1.0, key=False,
            tolerance=45, polygon=[]))
    for view in views:
        add(view, ['Reference'], 'complete character reference', 'reference')
        # Arrays are in painter order: background to foreground.
        for part, instruction in [
            ('Right Leg', 'character anatomical right leg including foot, complete upper leg under clothing'),
            ('Left Leg', 'character anatomical left leg including foot, complete upper leg under clothing'),
            ('Right Arm', 'character anatomical right arm and hand, full shoulder cap under torso'),
            ('Torso', 'torso and clothing only, no limbs or head, fill behind arms and head'),
            ('Neck', 'neck only, extend upward underneath the head and downward underneath shirt'),
            ('Left Arm', 'character anatomical left arm and hand, full shoulder cap under torso'),
        ]:
            add(view, ['Body', view, '+' + part if 'Arm' in part else part], instruction)
        head = ['+Head', view]
        face = ('head silhouette including hair and ears, remove eyes, eyebrows, nose and mouth; '
                'reconstruct continuous skin behind all removed features')
        if mode == 'jaw':
            face += '; remove the movable lower jaw and chin; paint a dark oral cavity behind the jaw'
        add(view, head + ['Face'], face)
        add(view, head + ['Nose'], 'nose only')
        for side in ['Right', 'Left']:
            add(view, head + [side + ' Eye', side + ' Eyeball'],
                f'character anatomical {side.lower()} eye white and eye outline only, no pupil or iris')
            add(view, head + [side + ' Eye', '+' + side + ' Pupil'],
                f'character anatomical {side.lower()} iris and pupil only, no eye white')
            add(view, head + [side + ' Eye', side + ' Blink'],
                f'character anatomical {side.lower()} closed eye with skin fill completely covering open eye', visible=False)
            add(view, head + ['+' + side + ' Eyebrow'],
                f'character anatomical {side.lower()} eyebrow only')
        if mode == 'jaw':
            add(view, head + ['+Jaw'], 'movable lower jaw including lower lip and chin, full overlapping jaw piece')
        else:
            for shape, instruction in VISEMES.items():
                add(view, head + ['Mouth', shape], f'mouth only: {instruction}; maintain the same mouth center and width scale', visible=shape == 'Neutral')
    return project


def ensure_sections(project):
    """Add the single-sided sections without discarding existing artwork."""
    if 'enabled_views' not in project:
        project['enabled_views'] = [v for v in project['views'] if v in SECTIONS]
    template = new_project(project['name'], project['description'], project['mode'], size=project['size'])
    for view in SECTIONS:
        if view not in project['views']:
            project['views'].append(view)
            for asset in template['assets']:
                if asset['view'] == view:
                    asset['id'] = 'a' + uuid.uuid4().hex[:12]
                    project['assets'].append(asset)


def enabled_views(project):
    return project.get('enabled_views', project['views'])


def asset_path(folder, asset):
    if not asset['file']:
        raise ValueError('This artwork has not been rendered or imported.')
    folder = Path(folder).resolve()
    path = (folder / asset['file']).resolve()
    if not path.is_relative_to(folder):
        raise ValueError('Artwork path must stay inside its project.')
    return path


def invalidate_dependents(project, changed):
    if changed['kind'] != 'reference':
        return
    for asset in project['assets']:
        if asset['id'] != changed['id'] and (changed['view'] == 'Frontal' or asset['view'] == changed['view']):
            asset['approved'] = False


def prepare(folder, asset, size):
    im = Image.open(asset_path(folder, asset)).convert('RGBA')
    if asset.get('key'):
        pixels = np.asarray(im).copy()
        distance = np.max(np.abs(pixels[:, :, :3].astype(float) - [255, 0, 255]), axis=2)
        # Preserve original alpha; soften only the edge of the selected key range.
        alpha = np.clip((distance - asset['tolerance']) / 20, 0, 1)
        pixels[:, :, 3] = (pixels[:, :, 3] * alpha).astype('uint8')
        im = Image.fromarray(pixels)
    if len(asset.get('polygon', [])) >= 3:
        mask = Image.new('L', im.size)
        ImageDraw.Draw(mask).polygon([tuple(p) for p in asset['polygon']], fill=255)
        im.putalpha(ImageChops.multiply(im.getchannel('A'), mask))
    scale = float(asset['scale'])
    if not 0.05 <= scale <= 4:
        raise ValueError('Scale must be between 0.05 and 4.')
    im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.Resampling.LANCZOS)
    canvas = Image.new('RGBA', tuple(size))
    canvas.alpha_composite(im, (int(asset['x']), int(asset['y'])))
    return canvas


def composite(project, folder, view='Frontal', selected=None):
    canvas = Image.new('RGBA', tuple(project['size']))
    for asset in project['assets']:
        if asset['kind'] != 'part' or asset['view'] != view or not asset['file']:
            continue
        visible = asset['visible']
        if selected and selected['view'] == view:
            if 'Mouth' in selected['path'] and 'Mouth' in asset['path']:
                visible = asset['id'] == selected['id']
            if selected['path'][-1].endswith('Blink') and asset['path'][:-1] == selected['path'][:-1]:
                visible = asset['id'] == selected['id']
        if visible:
            canvas.alpha_composite(prepare(folder, asset, project['size']))
    return canvas


def validate(project, folder):
    problems = []
    for a in project['assets']:
        if a['view'] not in enabled_views(project):
            continue
        if a['kind'] == 'reference':
            continue
        label = '/'.join(a['path'])
        if not a['file']:
            problems.append(f'Missing: {label}')
            continue
        if not a['approved']:
            problems.append(f'Needs review: {label}')
        if a.get('mask_choice_pending'):
            problems.append(f'Choose a mask: {label}')
        image = prepare(folder, a, project['size'])
        alpha = image.getchannel('A')
        if not alpha.getbbox() and not a.get('occluded'):
            problems.append(f'Empty: {label} (mark occluded only for a hidden far-side feature)')
        if alpha.getextrema()[0] == 255:
            problems.append(f'No transparency: {label}')
    return problems


def export_psd(project, folder, destination, draft=False):
    if not enabled_views(project):
        raise ValueError('Select at least one section to export.')
    problems = validate(project, folder)
    if problems and not draft:
        raise ValueError(f'{len(problems)} artwork issues remain.\n' + '\n'.join(problems[:12]))
    psd = PSDImage.new('RGBA', tuple(project['size']), color=(0, 0, 0, 0))
    root = Group.new(psd, project['name'])
    groups = {(): root}
    for asset in project['assets']:
        if asset['kind'] != 'part' or not asset['file'] or asset['view'] not in enabled_views(project):
            continue
        im = prepare(folder, asset, project['size'])
        box = im.getbbox()
        if box is None:
            continue
        path = asset['path']
        for i in range(1, len(path)):
            key = tuple(path[:i])
            if key not in groups:
                group = Group.new(groups[key[:-1]], path[i-1])
                # Keep the PSD's resting composite frontal. Verify swap visibility in CA.
                if path[i-1] in VIEWS:
                    group.visible = path[i-1] == enabled_views(project)[0]
                groups[key] = group
        layer = PixelLayer.frompil(im.crop(box), groups[tuple(path[:-1])], path[-1], top=box[1], left=box[0])
        layer.visible = asset['visible']
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix('.tmp.psd')
    psd.save(temp)
    check = PSDImage.open(temp)
    if check.size != tuple(project['size']) or not len(check):
        raise RuntimeError('PSD verification failed.')
    temp.replace(destination)
    composite(project, folder, view=enabled_views(project)[0]).save(destination.with_suffix('.preview.png'))
    destination.with_suffix('.rigging.txt').write_text(
        ('DRAFT: artwork is incomplete or unreviewed.\n' if draft else '') +
        'Import this PSD into Adobe Character Animator.\n'
        'Verify Head, Eye, Pupil, Blink, mouth and view tags after import.\n'
        'Add/configure Head & Body Turner for the Head and Body view groups.\n'
        'Check that hidden alternative views and mouth/blink artwork activate correctly.\n'
        'Set the Head origin at the neck overlap. Set shoulder origins for independent arms.\n'
        'Add wrist/elbow/hip/knee/ankle handles and sticks in Character Animator as needed.\n' +
        ('Add Nutcracker Jaw; set the jaw pivot/origin and travel. Do not add viseme mouth artwork.\n'
         if project['mode'] == 'jaw' else 'Configure Lip Sync and test each viseme, smile and surprised shape.\n') +
        'PSD layer names do not encode behavior settings, handles or a finished .puppet rig.\n\n' +
        '\n'.join(problems), encoding='utf-8')
    return destination


def node(kind, **inputs):
    return dict(class_type=kind, inputs=inputs)


def workflow_ui(graph, info):
    """Represent this small API graph as a draggable ComfyUI frontend workflow."""
    nodes, links, depths, rows = {}, [], {}, {}
    def depth(key):
        if key not in depths:
            parents = [v[0] for v in graph[key]['inputs'].values() if isinstance(v, list)]
            depths[key] = 1 + max((depth(k) for k in parents), default=-1)
        return depths[key]
    for key, spec in graph.items():
        kind, inputs = spec['class_type'], spec['inputs']
        schema = info[kind]
        column = depth(key)
        row = rows.get(column, 0)
        rows[column] = row + 1
        n = dict(id=int(key), type=kind, pos=[column*340, row*370], size=[300, 310],
                 flags={}, order=len(nodes), mode=0, inputs=[], outputs=[],
                 properties={'Node name for S&R': kind}, widgets_values=[])
        for group in ('required', 'optional'):
            for name, definition in schema['input'].get(group, {}).items():
                typ = definition[0]
                options = definition[1] if len(definition) > 1 else {}
                value = inputs.get(name)
                if isinstance(value, list):
                    link_id = len(links)+1
                    input_type = 'COMBO' if isinstance(typ,list) else typ
                    socket = dict(name=name,type=input_type,link=link_id)
                    if input_type in ('INT','FLOAT','STRING','BOOLEAN','COMBO'):
                        socket['widget'] = {'name':name}
                    links.append([link_id,int(value[0]),value[1],int(key),len(n['inputs']),input_type])
                    n['inputs'].append(socket)
                elif name in inputs:
                    n['widgets_values'].append(value)
                    if options.get('control_after_generate'):
                        n['widgets_values'].append('fixed')
                    if options.get('image_upload'):
                        n['widgets_values'].append('image')
                elif not isinstance(typ,list) and typ not in ('INT','FLOAT','STRING','BOOLEAN'):
                    n['inputs'].append(dict(name=name,type=typ,link=None))
        for index, typ in enumerate(schema.get('output', [])):
            names = schema.get('output_name', schema['output'])
            n['outputs'].append(dict(name=names[index],type=typ,links=[],slot_index=index))
        nodes[key] = n
    for link_id, source, slot, *_ in links:
        nodes[str(source)]['outputs'][slot]['links'].append(link_id)
    return dict(last_node_id=max(n['id'] for n in nodes.values()),last_link_id=len(links),
                nodes=list(nodes.values()),links=links,groups=[],config={},extra={},version=0.4)


def workflow(config, project, instruction, reference=None):
    width, height = project['size']
    if reference is not None and config.get('edit_family') == 'klein4b':
        validate_edit_pair(config)
        return klein_workflow(config, project, instruction, reference)
    if reference is None:
        graph = {
            '1': node('CheckpointLoaderSimple', ckpt_name=config['checkpoint']),
            '2': node('CLIPTextEncode', clip=['1', 1], text=instruction),
            '3': node('CLIPTextEncode', clip=['1', 1], text='text, watermark, cropped feet, cropped head, multiple characters, contact sheet, scenery'),
            '4': node('EmptyLatentImage', width=width, height=height, batch_size=1),
            '5': node('KSampler', model=['1', 0], positive=['2', 0], negative=['3', 0], latent_image=['4', 0], seed=project['seed'], steps=25, cfg=7.0, sampler_name='euler', scheduler='normal', denoise=1.0),
            '6': node('VAEDecode', samples=['5', 0], vae=['1', 2]),
            '7': node('SaveImage', images=['6', 0], filename_prefix='CharacterCreator/reference'),
        }
        model, clip = add_loras(graph, config.get('reference_loras', []), ['1', 0], ['1', 1])
        graph['5']['inputs']['model'] = model
        if Path(config['checkpoint'].replace('\\', '/')).name == 'sdxl_lightning_4step.safetensors':
            graph['5']['inputs'].update(steps=4, cfg=1.0, sampler_name='euler', scheduler='sgm_uniform')
        for key in ('2', '3'):
            graph[key]['inputs']['clip'] = clip
        if config.get('reference_vae'):
            graph['90'] = node('VAELoader', vae_name=config['reference_vae'])
            graph['6']['inputs']['vae'] = ['90', 0]
        return graph
    loader = config['edit_loader']
    resolution = int(config.get('edit_resolution', 1024))
    factor = (resolution*resolution/(width*height))**0.5
    work_width, work_height = max(64,round(width*factor/8)*8),max(64,round(height*factor/8)*8)
    graph = {
        '1': node(loader, **({'unet_name': config['edit_model']} if loader == 'UnetLoaderGGUF' else {'unet_name': config['edit_model'], 'weight_dtype': 'default'})),
        '2': node('CLIPLoader', clip_name=config['text_encoder'], type='qwen_image', device='default'),
        '3': node('VAELoader', vae_name=config['vae']),
        '4': node('LoadImage', image=reference),
        '5': node('TextEncodeQwenImageEdit', clip=['2', 0], vae=['3', 0], image=['13', 0], prompt=instruction),
        '6': node('TextEncodeQwenImageEdit', clip=['2', 0], vae=['3', 0], image=['13', 0], prompt=''),
        '7': node('ModelSamplingAuraFlow', model=['1', 0], shift=3.0),
        '8': node('VAEEncode', pixels=['13', 0], vae=['3', 0]),
        '9': node('KSampler', model=['12', 0], positive=['5', 0], negative=['6', 0], latent_image=['8', 0], seed=project['seed'], steps=config['edit_steps'], cfg=config['edit_cfg'], sampler_name='euler', scheduler='simple', denoise=1.0),
        '10': node('VAEDecode', samples=['9', 0], vae=['3', 0]),
        '11': node('SaveImage', images=['14', 0], filename_prefix='CharacterCreator/edit'),
        '12': node('CFGNorm', model=['7', 0], strength=1.0),
        '13': node('ImageScale', image=['4',0], upscale_method='lanczos', width=work_width,height=work_height,crop='disabled'),
        '14': node('ImageScale', image=['10',0], upscale_method='lanczos', width=width,height=height,crop='disabled'),
    }
    model, clip = add_loras(graph, config.get('edit_loras', []), ['1', 0], ['2', 0])
    graph['7']['inputs']['model'] = model
    for key in ('5', '6'):
        graph[key]['inputs']['clip'] = clip
    return graph


def klein_workflow(config, project, instruction, reference):
    width, height = project['size']
    resolution = int(config.get('edit_resolution', 1024))
    factor = resolution / (width*height)**0.5
    w, h = max(64,round(width*factor/16)*16), max(64,round(height*factor/16)*16)
    graph = {
        '1': node('UNETLoader', unet_name=config['edit_model'], weight_dtype='default'),
        '2': node('CLIPLoader', clip_name=config['text_encoder'], type='flux2', device='default'),
        '3': node('VAELoader', vae_name=config['vae']),
        '4': node('LoadImage', image=reference),
        '5': node('ImageScale', image=['4',0], upscale_method='lanczos',width=w,height=h,crop='disabled'),
        '6': node('VAEEncode', pixels=['5',0], vae=['3',0]),
        '7': node('CLIPTextEncode', clip=['2',0], text=instruction),
        '8': node('ConditioningZeroOut', conditioning=['7',0]),
        '9': node('ReferenceLatent', conditioning=['7',0], latent=['6',0]),
        '10': node('ReferenceLatent', conditioning=['8',0], latent=['6',0]),
        '11': node('CFGGuider', model=['1',0], positive=['9',0], negative=['10',0], cfg=1.0),
        '12': node('RandomNoise', noise_seed=project['seed']),
        '13': node('KSamplerSelect', sampler_name='euler'),
        '14': node('Flux2Scheduler', steps=4,width=w,height=h),
        '15': node('EmptyFlux2LatentImage', width=w,height=h,batch_size=1),
        '16': node('SamplerCustomAdvanced', noise=['12',0], guider=['11',0], sampler=['13',0], sigmas=['14',0], latent_image=['15',0]),
        '17': node('VAEDecode', samples=['16',0], vae=['3',0]),
        '18': node('ImageScale', image=['17',0], upscale_method='lanczos',width=width,height=height,crop='disabled'),
        '19': node('SaveImage', images=['18',0], filename_prefix='CharacterCreator/klein-edit'),
    }
    model, clip = add_loras(graph,config.get('edit_loras',[]),['1',0],['2',0])
    graph['11']['inputs']['model'] = model
    graph['7']['inputs']['clip'] = clip
    return graph


def add_loras(graph, loras, model, clip):
    for index, lora in enumerate(loras, 100):
        key = str(index)
        graph[key] = node('LoraLoader', model=model, clip=clip, lora_name=lora['name'],
                          strength_model=float(lora['strength_model']), strength_clip=float(lora['strength_clip']))
        model, clip = [key, 0], [key, 1]
    return model, clip


def model_options(info, kind, field):
    inputs = info.get(kind, {}).get('input', {})
    definition = inputs.get('required', {}).get(field, inputs.get('optional', {}).get(field, []))
    return definition[0] if definition and isinstance(definition[0], list) else []


def validate_edit_pair(config):
    if config.get('edit_family') == 'klein4b':
        encoder = Path(config.get('text_encoder','').replace('\\','/')).name.lower()
        vae = Path(config.get('vae','').replace('\\','/')).name.lower()
        if 'qwen_3_4b' not in encoder or 'flux2' not in vae:
            raise ValueError('Klein 4B needs qwen_3_4b.safetensors as its text encoder and flux2-vae.safetensors as its VAE. '
                             'The Qwen Image Edit encoder/VAE are incompatible. Click Use installed fast pair in Models & LoRAs, then Save settings.')


def validate_models(config, info):
    import math
    validate_edit_pair(config)
    result = dict(config)
    if config.get('edit_family', 'qwen') not in ('qwen','klein4b'):
        raise ValueError('Select Qwen or Klein 4B as the edit workflow.')
    if config.get('edit_family') == 'klein4b' and config['edit_loader'] != 'UNETLoader':
        raise ValueError('The installed Klein FP8 model uses the standard UNETLoader.')
    for key, default, lower, upper in [('edit_steps',20,1,100), ('edit_resolution',1024,512,1536)]:
        value = float(config.get(key, default))
        if not math.isfinite(value) or not value.is_integer() or not lower <= value <= upper:
            raise ValueError(f'{key} must be a whole number from {lower} to {upper}.')
        result[key] = int(value)
    cfg = float(config.get('edit_cfg', 2.5))
    if not math.isfinite(cfg) or not 0 <= cfg <= 30:
        raise ValueError('Edit guidance must be a number from 0 to 30.')
    result['edit_cfg'] = cfg
    checks = [('CheckpointLoaderSimple', 'ckpt_name', 'checkpoint'),
              (config['edit_loader'], 'unet_name', 'edit_model'),
              ('CLIPLoader', 'clip_name', 'text_encoder'), ('VAELoader', 'vae_name', 'vae')]
    if config.get('reference_vae'):
        checks.append(('VAELoader', 'vae_name', 'reference_vae'))
    def match(kind, field, value):
        found = next((v for v in model_options(info, kind, field) if v.replace('\\', '/') == value.replace('\\', '/')), None)
        if found is None:
            raise ValueError(f'Model unavailable: {value or "(none selected)"}. Refresh Models & LoRAs from ComfyUI.')
        return found
    for kind, field, setting in checks:
        result[setting] = match(kind, field, config[setting])
    for setting in ('reference_loras', 'edit_loras'):
        result[setting] = []
        for item in config.get(setting, []):
            lora = dict(item, name=match('LoraLoader', 'lora_name', item['name']))
            for field in ('strength_model', 'strength_clip'):
                strength = float(lora[field])
                if not math.isfinite(strength) or not -100 <= strength <= 100:
                    raise ValueError('LoRA strengths must be finite numbers between -100 and 100.')
                lora[field] = strength
            result[setting].append(lora)
    return result


def segmentation_workflow(config, reference, concept):
    return {
        '1': node('LoadImage', image=reference),
        '2': node('LoadSAM3Model', model_path=str(Path(config['comfy_root']) / 'models/sam3/sam3.pt')),
        '3': node('SAM3Grounding', sam3_model=['2',0], image=['1',0], confidence_threshold=0.2,
                  text_prompt=concept, max_detections=-1, offload_model=True),
        '4': node('MaskToImage', mask=['3',0]),
        '5': node('SaveImage', images=['4',0], filename_prefix='CharacterCreator/mask'),
    }


def concept_for(asset):
    name = asset['path'][-1].lstrip('+')
    if name == 'Face':
        return 'head including hair and ears'
    if 'Mouth' in asset['path']:
        return 'mouth'
    if 'Blink' in name or 'Eyeball' in name:
        return name.split()[0].lower() + ' eye'
    if name == 'Jaw':
        return 'lower jaw and chin'
    return name.lower()


def segment_image(comfy, image, folder, asset, log=print, cancel=None, progress=None):
    folder = Path(folder)
    source = folder / ('segment-input-' + asset['id'] + '.png')
    image.save(source)
    graph = segmentation_workflow(comfy.config, comfy.upload(source), concept_for(asset))
    jobs = folder / 'workflows'
    jobs.mkdir(parents=True, exist_ok=True)
    (jobs/(asset['id']+'.mask.api.json')).write_text(json.dumps(graph,indent=2),encoding='utf-8')
    masks = comfy.run(graph, log, cancel, all_images=True, progress=progress)
    return store_mask_candidates(image, masks, folder, asset, log)


def store_mask_candidates(image, masks, folder, asset, log=print):
    """Keep independent detections separate; never merge opposite limbs."""
    folder = Path(folder)
    candidates = []
    fingerprints = set()
    for candidate in masks:
        if candidate.size != image.size:
            raise ValueError('Segmentation changed the canvas size.')
    artwork = folder / 'artwork'
    artwork.mkdir(parents=True, exist_ok=True)
    for candidate in masks:
        mask = ImageChops.multiply(image.convert('RGBA').getchannel('A'), candidate.convert('L'))
        fingerprint = mask.tobytes()
        if not mask.getbbox() or fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        result = image.convert('RGBA')
        result.putalpha(mask)
        path = artwork / (asset['id'] + '-candidate-' + uuid.uuid4().hex[:8] + '.png')
        result.save(path)
        candidates.append(path.relative_to(folder).as_posix())
    if not candidates:
        raise ValueError('SAM3 did not find this part. Import a manually masked PNG or try a more specific concept in the saved mask workflow.')
    asset.update(mask_candidates=candidates, mask_choice_pending=len(candidates) > 1)
    if len(candidates) > 1:
        log(f'{len(candidates)} separate detections saved. Choose the correct mask in review; they were not merged.')
    return Image.open(folder / candidates[0]).convert('RGBA')


class Comfy:
    def __init__(self, config):
        self.config = config
        self.url = config['server'].rstrip('/')
        self.http = requests.Session()
        self.http.trust_env = False

    def get(self, route, **kwargs):
        try:
            response = self.http.get(self.url + route, timeout=30, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as error:
            raise ComfyUnavailable(self.connection_message()) from error
        response.raise_for_status()
        return response

    def connection_message(self):
        return (f'ComfyUI is unavailable at {self.url}. The render server stopped responding. '
                'Completed artwork is saved. Click Start ComfyUI, then resume missing parts. '
                'A submitted job was not automatically resubmitted; check its history before retrying. '
                'See comfy-errors.log for server details.')

    def post(self, route, **kwargs):
        try:
            return self.http.post(self.url + route, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as error:
            raise ComfyUnavailable(self.connection_message()) from error

    def queue_jobs(self):
        snapshot = self.get('/queue').json()
        jobs = []
        for key, state in [('queue_running','Running'),('queue_pending','Waiting')]:
            for item in snapshot.get(key, []):
                graph = item[2]
                outputs = [n.get('inputs',{}).get('filename_prefix','Image') for n in graph.values() if n.get('class_type')=='SaveImage']
                models = [n.get('inputs',{}).get('ckpt_name') or n.get('inputs',{}).get('unet_name') for n in graph.values()]
                jobs.append(dict(id=item[1],order=item[0],state=state,
                    artwork=', '.join(outputs) or 'ComfyUI job', model=', '.join(m for m in models if m)))
        return jobs

    def cancel_job(self, job_id):
        # The server atomically targets this ID; never interrupt a different job
        # that started after the queue snapshot was read.
        from urllib.parse import quote
        response = self.post('/api/jobs/' + quote(job_id, safe='') + '/cancel',json={},timeout=30)
        response.raise_for_status()
        return response.json().get('cancelled',False)

    def start(self, log=print):
        try:
            self.get('/system_stats')
            return 'ComfyUI is already running; its launch settings were left unchanged.'
        except ComfyUnavailable:
            pass
        root = Path(self.config['comfy_root'])
        python = root.parent / 'python_embeded' / 'python.exe'
        if not python.exists() or not (root / 'main.py').exists():
            raise ValueError('Portable ComfyUI location is invalid. Update config.json.')
        # This launcher owns a local instance only; it never stops an existing server.
        if self.url != 'http://127.0.0.1:8188':
            raise ValueError('Automatic startup requires the default local server address.')
        with (ROOT/'comfy-startup.log').open('a') as out, (ROOT/'comfy-errors.log').open('a') as err:
            flags = ['--disable-dynamic-vram', '--lowvram'] if self.config.get('conservative_memory', True) else []
            subprocess.Popen([str(python), '-s', 'main.py', '--windows-standalone-build',
                '--listen', '127.0.0.1', '--port', '8188', '--reserve-vram',
                str(self.config.get('reserve_vram_gb', 6)), *flags], cwd=root,
                stdout=out, stderr=err, creationflags=subprocess.CREATE_NO_WINDOW)
        log('Starting ComfyUI with GPU memory reserved for other applications…')
        for _ in range(90):
            time.sleep(1)
            try:
                self.get('/system_stats')
                return self.check()
            except ComfyUnavailable:
                continue
        raise TimeoutError('ComfyUI startup is taking longer than expected. Inspect comfy-errors.log before retrying.')

    def check(self):
        info = self.get('/object_info').json()
        required = ['CheckpointLoaderSimple', self.config['edit_loader'], 'TextEncodeQwenImageEdit', 'CLIPLoader', 'VAELoader', 'CFGNorm', 'LoadSAM3Model', 'SAM3Grounding']
        if self.config.get('edit_family') == 'klein4b':
            required = ['CheckpointLoaderSimple','UNETLoader','CLIPLoader','VAELoader','LoadSAM3Model','SAM3Grounding',
                        'ReferenceLatent','Flux2Scheduler','EmptyFlux2LatentImage','SamplerCustomAdvanced','CFGGuider']
        missing = [n for n in required if n not in info]
        if missing:
            raise ValueError('Missing ComfyUI nodes: ' + ', '.join(missing))
        if not (Path(self.config['comfy_root'])/'models/sam3/sam3.pt').exists():
            raise ValueError('SAM3 model not found at models/sam3/sam3.pt.')
        self.config.update(validate_models(self.config, info))
        return 'ComfyUI connected. Selected generation models, SAM3, text encoder and VAE found.'

    def upload(self, file):
        with Path(file).open('rb') as stream:
            name = 'character-' + hashlib.file_digest(stream, 'sha256').hexdigest() + '.png'
            stream.seek(0)
            response = self.post('/upload/image', files={'image': (name, stream, 'image/png')}, timeout=60)
        response.raise_for_status()
        result = response.json()
        return '/'.join(filter(None, [result.get('subfolder'), result['name']]))

    def run(self, graph, log=print, cancel=None, all_images=False, progress=None):
        client_id = uuid.uuid4().hex
        connection = None
        if progress:
            try:
                connection = websocket.create_connection(self.url.replace('http', 'ws', 1) + '/ws?clientId=' + client_id,
                    timeout=3, suppress_origin=True, http_no_proxy=['localhost', '127.0.0.1'])
                connection.settimeout(0.05)
            except (OSError, websocket.WebSocketException) as error:
                if connection:
                    connection.close()
                connection = None
                log(f'Live step progress unavailable ({error}); queue and completion tracking remain active.')
        try:
            return self._run(graph, log, cancel, all_images, progress, client_id, connection)
        finally:
            if connection:
                connection.close()

    def _run(self, graph, log, cancel, all_images, progress, client_id, connection):
        response = self.post('/prompt', json={'prompt': graph, 'client_id': client_id}, timeout=60)
        if not response.ok:
            raise RuntimeError('ComfyUI rejected the workflow: ' + response.text[:3000])
        prompt_id = response.json()['prompt_id']
        with (ROOT / 'submitted-jobs.jsonl').open('a', encoding='utf-8') as journal:
            journal.write(json.dumps({'prompt_id': prompt_id, 'time': time.time()}) + '\n')
        log('Queued render ' + prompt_id + '. Waiting for ComfyUI…')
        start = time.monotonic()
        last_log = start
        status_update = dict(state='queued', detail='Queued in ComfyUI', percent=None, prompt_id=prompt_id)
        if progress:
            progress(dict(status_update, elapsed=0))
        last_queue_check = 0
        while time.monotonic() - start < 3600:
            if cancel is not None and cancel.is_set():
                raise RuntimeError('Stopped waiting. The current ComfyUI job may still finish; no further jobs will be queued.')
            if connection:
                for _ in range(100):
                    try:
                        message = connection.recv()
                    except websocket.WebSocketTimeoutException:
                        break
                    except (OSError, websocket.WebSocketException):
                        connection.close()
                        connection = None
                        log('Live progress connection lost; checking ComfyUI queue and history instead.')
                        break
                    if not message:
                        connection.close()
                        connection = None
                        break
                    if not isinstance(message, str):
                        continue
                    event = json.loads(message)
                    update = progress_event(event, prompt_id, graph)
                    if update:
                        status_update.update(update)
                        if progress:
                            progress(dict(status_update,elapsed=int(time.monotonic()-start)))
            if progress and time.monotonic() - last_queue_check > 5:
                queue_info = self.get('/queue').json()
                if any(item[1] == prompt_id for item in queue_info.get('queue_running', [])) and status_update['state'] == 'queued':
                    status_update.update(state='running', detail='Starting / loading models', percent=None)
                last_queue_check = time.monotonic()
            if progress:
                progress(dict(status_update, elapsed=int(time.monotonic()-start)))
            history = self.get('/history/' + prompt_id).json().get(prompt_id)
            if history:
                status = history.get('status', {})
                if status.get('status_str') == 'error':
                    raise RuntimeError('ComfyUI render failed: ' + json.dumps(status.get('messages', []))[-2500:])
                images = []
                for output_id, output in history.get('outputs', {}).items():
                    if graph.get(output_id, {}).get('class_type') != 'SaveImage':
                        continue
                    for item in output.get('images', []):
                        images.append(Image.open(io.BytesIO(self.get('/view', params=item).content)).convert('RGBA'))
                if images:
                    if progress:
                        progress(dict(status_update, state='completed', detail='Image saved', percent=100, elapsed=int(time.monotonic()-start)))
                    return images if all_images else images[0]
                if status.get('completed'):
                    raise RuntimeError('ComfyUI finished without an image.')
            if time.monotonic() - last_log > 30:
                log(f'{status_update["detail"]}: {int(time.monotonic() - start)} seconds elapsed (job {prompt_id[:8]})')
                last_log = time.monotonic()
            time.sleep(1)
        raise TimeoutError('Render exceeded one hour. Check ComfyUI history before retrying.')


class ComfyUnavailable(RuntimeError):
    pass


def progress_event(event, prompt_id, graph):
    data = event.get('data', {})
    if data.get('prompt_id') != prompt_id:
        return None
    if event['type'] == 'progress_state':
        for node_id, node_state in reversed(list(data.get('nodes', {}).items())):
            if node_state.get('state') != 'running':
                continue
            if node_state.get('max', 0) > 1:
                return progress_event({'type':'progress','data':dict(node_state,prompt_id=prompt_id)}, prompt_id, graph)
            return progress_event({'type':'executing','data':dict(prompt_id=prompt_id,node=node_id)},prompt_id,graph)
    if event['type'] == 'progress':
        value, maximum = data.get('value', 0), data.get('max', 0)
        if maximum > 0:
            return dict(state='running', detail=f'Sampling {value}/{maximum}', percent=min(100,100*value/maximum))
    if event['type'] == 'executing' and data.get('node') is not None:
        kind = graph.get(str(data['node']), {}).get('class_type', 'Processing')
        labels = {'KSampler':'Starting sampler', 'SamplerCustomAdvanced':'Starting sampler', 'VAEDecode':'Decoding image', 'VAEEncode':'Encoding reference',
                  'TextEncodeQwenImageEdit':'Encoding prompt and reference', 'SaveImage':'Saving image',
                  'SAM3Grounding':'Detecting masks', 'LoadSAM3Model':'Loading mask model',
                  'UNETLoader':'Loading image model', 'UnetLoaderGGUF':'Loading image model',
                  'CLIPLoader':'Loading text encoder', 'CheckpointLoaderSimple':'Loading checkpoint'}
        return dict(state='running', detail=labels.get(kind,kind), percent=None)
    return None


def render_asset(project, folder, asset, comfy, log=print, cancel=None, extract=False, auto_approve=False, progress=None):
    folder = Path(folder)
    def stage_progress(stage):
        def update(event):
            if event['state'] == 'queued':
                asset.setdefault('render_jobs', {})[stage] = event['prompt_id']
                save(project, folder)
            if progress:
                progress(dict(event, stage=stage))
        return update
    if progress:
        progress(dict(state='running', stage='generation', detail='Preparing reference and workflow', percent=None, elapsed=0))
    reference = None
    if asset['kind'] == 'reference' and asset['view'] == 'Frontal':
        instruction = project['description'] + '. One full body character, front view, neutral mouth, open eyes, relaxed A pose, arms apart, feet apart, centered, entire body within canvas with margin. Plain light gray background. Clean character design for a layered animation puppet.'
    else:
        reference_view = 'Frontal' if asset['kind'] == 'reference' else asset['view']
        source = next(a for a in project['assets'] if a['kind'] == 'reference' and a['view'] == reference_view)
        if not source['file'] or not source['approved']:
            raise ValueError(f'Render/import and approve the {reference_view} reference first.')
        input_image = Image.new('RGBA', tuple(project['size']), '#cccccc')
        input_image.alpha_composite(prepare(folder, source, project['size']))
        input_path = folder / ('reference-input-' + source['id'] + '.png')
        input_image.convert('RGB').save(input_path)
        reference = comfy.upload(input_path)
        if asset['kind'] == 'reference':
            side = 'left' if 'Left' in asset['view'] else 'right'
            degrees = 45 if 'Quarter' in asset['view'] else 90
            instruction = (f'Turn this same character {degrees} degrees toward the character\'s own {side}. '
                'Preserve identity, outfit, colors, line weight, lighting, body proportions and canvas framing. '
                'Keep feet baseline and neck pivot at the same image coordinates. Full body, same neutral A pose. '
                'One character only, plain light gray background, no text. ' + project['description'])
        else:
            if asset['path'][-1] == 'Face':
                instruction = 'Remove the eyes, eyebrows, nose and mouth from the character. Fill these areas with matching skin. Keep the original head shape, hair, ears and colors. Keep the original full-body framing and exact size and position of the character. Leave everything else unchanged.'
                if project['mode'] == 'jaw':
                    instruction += ' Remove the lower jaw and fill the mouth cavity dark behind it.'
            else:
                instruction = ('Edit this character image to show the ' + asset['instruction'] +
                    '. Keep the original character colors, style, anatomy and framing. '
                    'Keep the part in the same location at the same size. Leave other parts and background unchanged. '
                    'Left and right refer to the character\'s own left and right.')
    graph = workflow(comfy.config, project, instruction, reference)
    jobs = folder / 'workflows'
    jobs.mkdir(parents=True, exist_ok=True)
    (jobs / (asset['id'] + '.api.json')).write_text(json.dumps(graph, indent=2), encoding='utf-8')
    (jobs / (asset['id'] + '.json')).write_text(json.dumps(workflow_ui(graph, comfy.get('/object_info').json()), indent=2), encoding='utf-8')
    image = input_image if extract and asset['kind'] == 'part' else comfy.run(graph, log, cancel, progress=stage_progress('generation'))
    if image.size != tuple(project['size']):
        raise ValueError(f'Render canvas {image.size} differs from project {project["size"]}; no automatic stretching was applied.')
    if asset['kind'] == 'part':
        raw = folder / 'artwork' / (asset['id'] + '-plate-' + uuid.uuid4().hex[:8] + '.png')
        raw.parent.mkdir(parents=True, exist_ok=True)
        image.save(raw)
        asset['render_plate'] = raw.relative_to(folder).as_posix()
        save(project,folder)
        if progress:
            progress(dict(state='running', stage='mask', detail='Image rendered; preparing mask detection', percent=None, elapsed=0))
        log('Separating ' + concept_for(asset) + ' with SAM3 while retaining pixel coordinates…')
        image = segment_image(comfy, image, folder, asset, log, cancel, progress=stage_progress('mask'))
    target = folder / 'artwork' / (asset['id'] + '-' + uuid.uuid4().hex[:8] + '.png')
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target)
    asset.update(file=target.relative_to(folder).as_posix(), approved=False, x=0, y=0, scale=1.0,
                 polygon=[], key=False, occluded=False)
    if auto_approve:
        if asset['kind'] == 'part' and asset.get('mask_candidates'):
            asset['file'] = asset['mask_candidates'][0]
        asset.update(mask_choice_pending=False, approved=True)
    invalidate_dependents(project, asset)
    save(project, folder)
    return target
