import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
import queue
import threading
import requests
from pathlib import Path
from PIL import Image
from psd_tools import PSDImage
from creator import new_project, prepare, export_psd, validate, workflow, invalidate_dependents, composite, segment_image, ROOT, Comfy, ComfyUnavailable


def child(group, name):
    return next(layer for layer in group if layer.name == name)


class ArtworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def artwork(self, project):
        for n, a in enumerate(project['assets']):
            if a['kind'] == 'reference':
                continue
            im = Image.new('RGBA', (16, 16), (20+n % 200, 70, 120, 255))
            name = a['id'] + '.png'
            im.save(self.folder / name)
            a.update(file=name, x=80, y=90, approved=True)

    def test_alignment_and_alpha(self):
        p = new_project('Test', 'test', views=['Frontal'], size=(256,256))
        a = p['assets'][1]
        im = Image.new('RGBA', (16,16), (255,0,255,255))
        im.paste((10,20,30,128), (4,4,12,12))
        im.save(self.folder / 'part.png')
        a.update(file='part.png', x=20, y=30, key=True)
        result = prepare(self.folder, a, p['size'])
        self.assertEqual(result.getbbox(), (24,34,32,42))
        self.assertEqual(result.getpixel((25,35)), (10,20,30,128))
        a['polygon'] = [[0,0],[7,0],[7,15],[0,15]]
        self.assertEqual(prepare(self.folder,a,p['size']).getbbox(), (24,34,28,42))

    def test_psd_roundtrip_modes_and_positions(self):
        for mode in ['visemes', 'jaw']:
            with self.subTest(mode=mode):
                p = new_project('Test', 'test', mode=mode, size=(256,256))
                self.artwork(p)
                target = self.folder / (mode + '.psd')
                export_psd(p, self.folder, target)
                psd = PSDImage.open(target)
                head = child(psd[0], '+Head')
                self.assertEqual(set(g.name for g in head), set(p['views']))
                self.assertFalse(child(head, 'Left Quarter').visible)
                frontal = child(head, 'Frontal')
                face = child(frontal, 'Face')
                self.assertEqual(face.bbox, (80,90,96,106))
                self.assertEqual(face.topil().getpixel((0,0))[3], 255)
                self.assertEqual(psd.topil().convert('RGB').getpixel((82,92)), composite(p,self.folder).convert('RGB').getpixel((82,92)))
                if mode == 'visemes':
                    mouth = child(frontal, 'Mouth')
                    self.assertEqual(len(mouth), 14)
                    self.assertTrue(child(mouth, 'Neutral').visible)
                    self.assertFalse(child(mouth, 'Ah').visible)
                    self.assertNotIn('+Jaw', [l.name for l in frontal])
                else:
                    self.assertIn('+Jaw', [l.name for l in frontal])
                    self.assertNotIn('Mouth', [l.name for l in frontal])

    def test_incomplete_cannot_export_as_reviewed(self):
        p = new_project('Test', 'test', views=['Frontal'], size=(256,256))
        with self.assertRaises(ValueError):
            export_psd(p, self.folder, self.folder / 'bad.psd')
        self.assertFalse((self.folder / 'bad.psd').exists())

    def test_reject_opaque_background_and_missing_file(self):
        p = new_project('Test', 'test', views=['Frontal'], size=(256,256))
        self.artwork(p)
        a = p['assets'][1]
        Image.new('RGBA',(256,256),'white').save(self.folder / a['file'])
        a.update(x=0,y=0)
        self.assertTrue(any('No transparency' in s for s in validate(p,self.folder)))

    def test_graph_links_resolve(self):
        config = json.loads((ROOT/'config.json').read_text())
        p = new_project('Test','test')
        for ref in (None, 'reference.png'):
            graph = workflow(config,p,'test',ref)
            for n in graph.values():
                for value in n['inputs'].values():
                    if isinstance(value,list):
                        self.assertIn(value[0],graph)
                        self.assertIsInstance(value[1],int)

    def test_reference_change_invalidates_review(self):
        p = new_project('Test','test')
        for a in p['assets']:
            a['approved'] = True
        left = next(a for a in p['assets'] if a['kind']=='reference' and a['view']=='Left Quarter')
        invalidate_dependents(p,left)
        self.assertFalse(next(a for a in p['assets'] if a['view']=='Left Quarter' and a['kind']=='part')['approved'])
        self.assertTrue(p['assets'][1]['approved'])
        invalidate_dependents(p,p['assets'][0])
        self.assertTrue(all(not a['approved'] for a in p['assets'][1:]))

    def test_segmentation_retains_source_pixels_without_merging_opposite_limbs(self):
        class FakeComfy:
            config = {'comfy_root':str(ROOT)}
            def upload(self,path):
                return 'source.png'
            def run(self,*args,**kwargs):
                a,b = Image.new('L',(64,64)),Image.new('L',(64,64))
                a.paste(255,(10,12,20,22))
                b.paste(255,(40,42,50,52))
                return [a,b]
        p = new_project('Test','test')
        image = Image.new('RGBA',(64,64),(12,34,56,128))
        result = segment_image(FakeComfy(),image,self.folder,p['assets'][1])
        self.assertEqual(result.getpixel((10,12)),(12,34,56,128))
        self.assertEqual(result.getpixel((40,42))[3],0)
        self.assertEqual(result.getpixel((0,0))[3],0)
        asset = p['assets'][1]
        self.assertTrue(asset['mask_choice_pending'])
        self.assertEqual(len(asset['mask_candidates']),2)
        second = Image.open(self.folder / asset['mask_candidates'][1])
        self.assertEqual(second.getpixel((40,42)),(12,34,56,128))
        self.assertEqual(second.getpixel((10,12))[3],0)

    def test_disconnect_is_actionable_and_submission_is_not_retried(self):
        comfy = Comfy({'server':'http://127.0.0.1:8188'})
        comfy.http = Mock()
        comfy.http.get.side_effect = requests.ConnectionError('refused')
        comfy.http.post.side_effect = requests.ConnectionError('reset')
        with self.assertRaisesRegex(ComfyUnavailable, 'Completed artwork is saved'):
            comfy.check()
        with self.assertRaisesRegex(ComfyUnavailable, 'not automatically resubmitted'):
            comfy.run({})
        self.assertEqual(comfy.http.post.call_count,1)

    def test_pending_mask_cannot_export_reviewed(self):
        p = new_project('Test','test',views=['Frontal'],size=(256,256))
        self.artwork(p)
        p['assets'][1]['mask_choice_pending'] = True
        self.assertTrue(any('Choose a mask' in s for s in validate(p,self.folder)))

    def test_preview_defaults_to_single_layer_and_candidate_requires_commit(self):
        from app import App
        app = App()
        app.withdraw()
        try:
            self.assertTrue(app.raw.get())
            self.assertFalse(app.overlay.get())
            self.assertFalse(app.auto_approve.get())
            p = new_project('Test','test',views=['Frontal'],size=(256,256))
            self.artwork(p)
            a = p['assets'][1]
            a.update(mask_candidates=[a['file'],p['assets'][2]['file']], mask_choice_pending=True)
            app.project, app.folder = p, self.folder
            app.refresh()
            app.tree.selection_set(a['id'])
            app.select()
            app.mask_candidate.current(1)
            with self.assertRaisesRegex(ValueError,'Use this mask'):
                app.require_committed_mask(a)
            app.choose_mask()
            self.assertEqual(a['file'],a['mask_candidates'][1])
            self.assertFalse(a['mask_choice_pending'])
            app.preview()
            self.assertIn('Selected layer',app.preview_label.cget('text'))
        finally:
            app.destroy()


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.project = new_project('Batch', 'test', views=['Frontal'])
        self.assets = self.project['assets'][1:4]
        self.app = SimpleNamespace(project=self.project, folder=Path('unused'),
            comfy=Mock(), messages=queue.Queue(), cancel=threading.Event(),
            auto_approve=Mock(get=Mock(return_value=False)),
            worker=lambda action: action())

    def logs(self):
        return '\n'.join(str(message) for kind,message in self.app.messages.queue if kind == 'log')

    def test_fast_batch_extracts_only_unchanged_features(self):
        from app import App
        self.app.fast_extract = Mock(get=Mock(return_value=True))
        assets = self.project['assets'][1:]
        with patch('app.render_asset') as render:
            App.run_assets(self.app,assets,batch=True)
        extracted = [call.args[2]['path'][-1] for call in render.call_args_list if call.kwargs['extract']]
        self.assertEqual(set(extracted),{'Nose','+Left Eyebrow','+Right Eyebrow','Neutral'})
        self.assertEqual(len(extracted),4)

    def test_pending_mask_does_not_stop_remaining_parts_or_approve_them(self):
        from app import App
        def render(project, folder, asset, *args, **kwargs):
            asset.update(file=asset['id']+'.png',mask_choice_pending=True)
        with patch('app.render_asset', side_effect=render) as renderer:
            App.run_assets(self.app,self.assets,batch=True)
        self.assertEqual(renderer.call_count,3)
        self.assertTrue(all(a['mask_choice_pending'] and not a['approved'] for a in self.assets))
        self.assertIn('3 saved, 0 failed, 0 still missing',self.logs())
        self.assertEqual(sum(kind=='artwork_saved' for kind,_ in self.app.messages.queue),3)

    def test_part_failure_does_not_skip_later_parts(self):
        from app import App
        def render(project, folder, asset, *args, **kwargs):
            if asset is self.assets[1]:
                raise ValueError('No usable mask')
            asset['file'] = asset['id']+'.png'
        with patch('app.render_asset',side_effect=render) as renderer:
            App.run_assets(self.app,self.assets,batch=True)
        self.assertEqual(renderer.call_count,3)
        self.assertIsNone(self.assets[1]['file'])
        self.assertIn('2 saved, 1 failed, 1 still missing',self.logs())

    def test_stop_prevents_next_submission(self):
        from app import App
        def render(project,folder,asset,*args,**kwargs):
            asset['file'] = asset['id']+'.png'
            self.app.cancel.set()
        with patch('app.render_asset',side_effect=render) as renderer:
            App.run_assets(self.app,self.assets,batch=True)
        self.assertEqual(renderer.call_count,1)
        self.assertIn('Batch stopped',self.logs())

    def test_disconnect_stops_without_submitting_more_jobs(self):
        from app import App
        with patch('app.render_asset',side_effect=ComfyUnavailable('offline')) as renderer:
            with self.assertRaises(ComfyUnavailable):
                App.run_assets(self.app,self.assets,batch=True)
        self.assertEqual(renderer.call_count,1)

    def test_resume_from_view_header_skips_saved_pending_masks(self):
        from app import App
        self.project['assets'][0].update(file='reference.png',approved=True)
        self.assets[0].update(file='candidate.png',mask_choice_pending=True)
        self.app.selected = Mock(return_value=None)
        self.app.tree = Mock()
        self.app.tree.selection.return_value = ('Frontal',)
        self.app.log = Mock()
        self.app.run_assets = Mock()
        App.render_view(self.app)
        queued = self.app.run_assets.call_args.args[0]
        self.assertEqual(len(queued),len(self.project['assets'])-2)
        self.assertNotIn(self.assets[0],queued)
        self.assertTrue(self.app.run_assets.call_args.kwargs['batch'])

    def test_checkbox_is_captured_for_entire_batch(self):
        from app import App
        self.app.auto_approve.get.return_value = True
        def render(project,folder,asset,*args,**kwargs):
            self.app.auto_approve.get.return_value = False
            asset['file'] = 'rendered.png'
        with patch('app.render_asset',side_effect=render) as renderer:
            App.run_assets(self.app,self.assets,batch=True)
        self.assertTrue(all(call.kwargs['auto_approve'] for call in renderer.call_args_list))


class GenerationApprovalTests(unittest.TestCase):
    def test_mask_selection_and_approval_are_saved_only_when_enabled(self):
        from creator import render_asset
        for enabled in (False, True):
            with self.subTest(auto_approve=enabled), tempfile.TemporaryDirectory() as temp:
                folder = Path(temp)
                p = new_project('Test','test',views=['Frontal'],size=(256,256))
                image = Image.new('RGBA',(256,256))
                image.paste((12,34,56,255),(10,10,30,30))
                for filename in ['reference.png','first.png','second.png']:
                    image.save(folder/filename)
                p['assets'][0].update(file='reference.png',approved=True)
                a = p['assets'][1]
                comfy = Mock(config=json.loads((ROOT/'config.json').read_text()))
                comfy.upload.return_value = 'reference.png'
                comfy.run.return_value = image
                def segment(*args,**kwargs):
                    a.update(mask_candidates=['first.png','second.png'],mask_choice_pending=True)
                    return image
                with patch('creator.workflow_ui',return_value={}), patch('creator.segment_image',side_effect=segment):
                    render_asset(p,folder,a,comfy,log=lambda _:None,auto_approve=enabled)
                stored = json.loads((folder/'project.json').read_text())['assets'][1]
                self.assertEqual(stored['approved'],enabled)
                self.assertEqual(stored['mask_choice_pending'],not enabled)
                self.assertEqual(stored['mask_candidates'],['first.png','second.png'])
                if enabled:
                    self.assertEqual(stored['file'],'first.png')


class SectionTests(unittest.TestCase):
    def test_default_views_have_one_quarter_and_one_side(self):
        self.assertEqual(new_project('Test','test')['views'],['Frontal','Left Quarter','Left Profile'])

    def test_existing_artwork_is_preserved_when_side_is_added(self):
        from creator import ensure_sections
        p = new_project('Test','test',views=['Frontal','Left Quarter','Right Quarter'])
        existing = list(p['assets'])
        existing[-1]['file'] = 'saved.png'
        ensure_sections(p)
        self.assertEqual(p['enabled_views'],['Frontal','Left Quarter'])
        self.assertIn('Left Profile',p['views'])
        self.assertEqual(p['assets'][:len(existing)],existing)
        self.assertEqual(len({a['id'] for a in p['assets']}),len(p['assets']))
        count = len(p['assets'])
        ensure_sections(p)
        self.assertEqual(len(p['assets']),count)

    def test_batch_renders_only_checked_sections(self):
        from app import App
        p = new_project('Test','test')
        p['enabled_views'] = ['Left Profile']
        ref = next(a for a in p['assets'] if a['kind']=='reference' and a['view']=='Left Profile')
        ref.update(file='side.png',approved=True)
        app = SimpleNamespace(project=p,log=Mock(),run_assets=Mock())
        App.render_sections(app)
        queued = app.run_assets.call_args.args[0]
        self.assertTrue(queued)
        self.assertTrue(all(a['kind']=='part' and a['view']=='Left Profile' for a in queued))

    def test_disabled_missing_views_do_not_block_side_only_export(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            p = new_project('Test','test',size=(256,256))
            p['enabled_views'] = ['Left Profile']
            for a in p['assets']:
                if a['view']=='Left Profile' and a['kind']=='part':
                    Image.new('RGBA',(12,12),'red').save(folder/(a['id']+'.png'))
                    a.update(file=a['id']+'.png',approved=True,x=80,y=90)
            self.assertEqual(validate(p,folder),[])
            export_psd(p,folder,folder/'side.psd')
            psd=PSDImage.open(folder/'side.psd')
            head=child(psd[0],'+Head')
            self.assertEqual([v.name for v in head],['Left Profile'])
            self.assertTrue(head[0].visible)


class ModelSettingsTests(unittest.TestCase):
    def test_klein_rejects_qwen_encoder_or_vae_before_submission(self):
        from creator import validate_edit_pair
        config=dict(edit_family='klein4b',text_encoder='qwen_3_4b.safetensors',vae='flux2-vae.safetensors')
        validate_edit_pair(config)
        for change in [dict(text_encoder='qwen_2.5_vl_7b_fp8_scaled.safetensors'),dict(vae='qwen_image_vae.safetensors')]:
            with self.subTest(change=change),self.assertRaisesRegex(ValueError,'Klein 4B needs'):
                validate_edit_pair(dict(config,**change))

    def test_fast_models_use_distilled_sampling_and_reference_conditioning(self):
        config = json.loads((ROOT/'config.json').read_text())
        config.update(checkpoint='sdxl_lightning_4step.safetensors',edit_family='klein4b',
            edit_model='flux-2-klein-4b-fp8.safetensors',text_encoder='qwen_3_4b.safetensors',
            vae='flux2-vae.safetensors',edit_loader='UNETLoader',edit_loras=[])
        project=new_project('Test','test')
        graph=workflow(config,project,'test')
        self.assertEqual(graph['5']['inputs']['steps'],4)
        self.assertEqual(graph['5']['inputs']['cfg'],1)
        self.assertEqual(graph['5']['inputs']['scheduler'],'sgm_uniform')
        graph=workflow(config,project,'test','source.png')
        self.assertEqual(graph['14']['inputs']['steps'],4)
        self.assertEqual(graph['2']['inputs']['type'],'flux2')
        self.assertEqual(graph['15']['class_type'],'EmptyFlux2LatentImage')
        self.assertEqual(graph['9']['inputs']['latent'],['6',0])
        self.assertEqual(graph['10']['inputs']['latent'],['6',0])
        self.assertEqual(graph['18']['inputs']['width'],1024)

    def test_speed_controls_reduce_working_canvas_keep_output_size(self):
        config = json.loads((ROOT/'config.json').read_text())
        config.update(edit_family='qwen',edit_resolution=768,edit_steps=12)
        graph = workflow(config,new_project('Test','test'),'test','reference.png')
        self.assertEqual(graph['13']['inputs']['width'],768)
        self.assertEqual(graph['13']['inputs']['height'],768)
        self.assertEqual(graph['14']['inputs']['width'],1024)
        self.assertEqual(graph['9']['inputs']['steps'],12)

    def test_lora_stack_reaches_sampler_and_both_text_prompts(self):
        config = json.loads((ROOT/'config.json').read_text())
        config['edit_family']='qwen'
        loras = [dict(name='style.safetensors',strength_model=0.7,strength_clip=0.4),
                 dict(name='detail.safetensors',strength_model=0.2,strength_clip=0)]
        config.update(reference_loras=loras,edit_loras=loras,reference_vae='custom.vae')
        project = new_project('Test','test')
        reference = workflow(config,project,'test')
        self.assertEqual(reference['100']['inputs']['model'],['1',0])
        self.assertEqual(reference['101']['inputs']['model'],['100',0])
        self.assertEqual(reference['101']['inputs']['clip'],['100',1])
        self.assertEqual(reference['5']['inputs']['model'],['101',0])
        for key in ('2','3'):
            self.assertEqual(reference[key]['inputs']['clip'],['101',1])
        self.assertEqual(reference['6']['inputs']['vae'],['90',0])
        self.assertEqual(reference['100']['inputs']['strength_model'],0.7)
        edited = workflow(config,project,'test','reference.png')
        self.assertEqual(edited['7']['inputs']['model'],['101',0])
        for key in ('5','6'):
            self.assertEqual(edited[key]['inputs']['clip'],['101',1])
        self.assertEqual(edited['3']['inputs']['vae_name'],config['vae'])

    def test_detected_files_and_lora_strengths_are_validated(self):
        from creator import validate_models
        config = dict(checkpoint='base',edit_loader='UnetLoaderGGUF',edit_model='gguf/model',text_encoder='clip',vae='vae')
        info = {}
        for kind, field, values in [('CheckpointLoaderSimple','ckpt_name',['base']),
            ('UnetLoaderGGUF','unet_name',['gguf\\model']),('CLIPLoader','clip_name',['clip']),
            ('VAELoader','vae_name',['vae']),('LoraLoader','lora_name',['style'])]:
            info[kind] = {'input':{'required':{field:[values]}}}
        self.assertEqual(validate_models(config,info)['edit_model'],'gguf\\model')
        with self.assertRaises(ValueError):
            validate_models(dict(config,checkpoint='missing'),info)
        for strength in ('nan','inf',101):
            with self.subTest(strength=strength), self.assertRaises(ValueError):
                validate_models(dict(config,edit_loras=[dict(name='style',strength_model=strength,strength_clip=1)]),info)
        with self.assertRaises(ValueError):
            validate_models(dict(config,reference_loras=[dict(name='missing',strength_model=1,strength_clip=1)]),info)


class QueueTests(unittest.TestCase):
    def test_cancellation_targets_only_the_requested_job(self):
        comfy=Comfy({'server':'http://localhost:8188'})
        comfy.post=Mock(return_value=Mock(json=lambda:{'cancelled':True}))
        self.assertTrue(comfy.cancel_job('job-123'))
        comfy.post.assert_called_once_with('/api/jobs/job-123/cancel',json={},timeout=30)

    def test_queue_window_stops_waiting_before_running_and_pauses_batch(self):
        import time
        from app import App
        from queue_ui import QueueWindow
        app=App()
        try:
            jobs=[dict(id='running',order=0,state='Running',artwork='edit',model='Qwen'),
                  dict(id='waiting',order=1,state='Waiting',artwork='reference',model='SDXL')]
            client=Mock()
            client.queue_jobs.side_effect=lambda:list(jobs)
            def cancel(job_id):
                jobs[:]=[j for j in jobs if j['id']!=job_id]
                return True
            client.cancel_job.side_effect=cancel
            with patch('queue_ui.Comfy',return_value=client):
                window=QueueWindow(app)
            def finish():
                deadline=time.monotonic()+3
                while window.loading and time.monotonic()<deadline:
                    app.update()
                    time.sleep(.01)
                self.assertFalse(window.loading)
            finish()
            self.assertEqual(len(window.tree.get_children()),2)
            app.busy=True
            window.refresh('all')
            finish()
            self.assertTrue(app.cancel.is_set())
            self.assertEqual([c.args[0] for c in client.cancel_job.call_args_list],['waiting','running'])
            self.assertFalse(window.tree.get_children())
            window.destroy()
        finally:
            app.destroy()


class ConcurrentReviewTests(unittest.TestCase):
    def test_mask_choice_stays_selected_and_saves_while_other_part_runs(self):
        from app import App
        from tkinter import ttk
        with tempfile.TemporaryDirectory() as temp:
            app = App()
            try:
                app.folder = Path(temp)
                app.project = new_project('Test','test',views=['Frontal'],size=(256,256))
                for name in ('first.png','second.png'):
                    Image.new('RGBA',(256,256),'red').save(app.folder/name)
                finished, running = app.project['assets'][1:3]
                finished.update(file='first.png',mask_candidates=['first.png','second.png'],mask_choice_pending=True)
                app.build_sections(); app.refresh()
                app.tree.selection_set(finished['id'])
                app.update()
                app.select()
                app.mask_candidate.current(1)
                app.busy = app.review_during_render = True
                app.rendering_asset_ids = {running['id']}
                app.messages.put(('progress',(running['id'],dict(state='running',stage='generation',percent=10))))
                app.messages.put(('artwork_saved',app.project['assets'][3]['id']))
                app.poll(); app.update()
                self.assertEqual(app.selected()['id'],finished['id'])
                self.assertEqual(app.mask_candidate.current(),1)
                def descendants(widget):
                    for child in widget.winfo_children():
                        yield child
                        yield from descendants(child)
                buttons = {w['text']:w for w in descendants(app) if isinstance(w,ttk.Button)}
                self.assertNotIn(buttons['Use this mask'],app.edit_buttons)
                buttons['Use this mask'].invoke()
                self.assertEqual(finished['file'],'second.png')
                self.assertFalse(finished['mask_choice_pending'])
                buttons['Approve selected artwork'].invoke()
                self.assertTrue(finished['approved'])
                stored = json.loads((app.folder/'project.json').read_text())
                self.assertTrue(stored['assets'][1]['approved'])
            finally:
                app.destroy()

    def test_only_finished_parts_can_be_reviewed_during_render(self):
        from app import App
        a = dict(id='finished',kind='part',file='saved.png')
        app = SimpleNamespace(busy=True,review_during_render=True,rendering_asset_ids={'running'},selected=lambda:a)
        App.require_reviewable(app)
        for change in [dict(id='running'),dict(kind='reference'),dict(file=None)]:
            old = dict(a)
            a.update(change)
            with self.assertRaises(ValueError):
                App.require_reviewable(app)
            a.clear(); a.update(old)
        app.review_during_render = False
        with self.assertRaises(ValueError):
            App.require_reviewable(app)

    def test_simultaneous_project_saves_are_serialized(self):
        from concurrent.futures import ThreadPoolExecutor
        from creator import save
        with tempfile.TemporaryDirectory() as temp:
            project = new_project('Test','test')
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda _:save(project,temp),range(20)))
            self.assertEqual(json.loads((Path(temp)/'project.json').read_text()),project)


class ProgressTests(unittest.TestCase):
    def test_generation_hands_off_to_mask_and_records_both_jobs(self):
        from creator import render_asset
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            p = new_project('Test','test',views=['Frontal'],size=(256,256))
            image = Image.new('RGBA',(256,256),(12,34,56,255))
            image.save(folder/'reference.png')
            p['assets'][0].update(file='reference.png',approved=True)
            a = p['assets'][1]
            comfy = Mock(config=json.loads((ROOT/'config.json').read_text()))
            comfy.upload.return_value = 'reference.png'
            events = []
            def generate(*args,**kwargs):
                kwargs['progress'](dict(state='queued',prompt_id='generation-job',percent=None))
                return image
            def mask(*args,**kwargs):
                self.assertTrue((folder/a['render_plate']).exists())
                kwargs['progress'](dict(state='queued',prompt_id='mask-job',percent=None))
                return image
            comfy.run.side_effect = generate
            with patch('creator.workflow_ui',return_value={}), patch('creator.segment_image',side_effect=mask):
                render_asset(p,folder,a,comfy,log=lambda _:None,progress=events.append)
            stored = json.loads((folder/'project.json').read_text())['assets'][1]
            self.assertEqual(stored['render_jobs'],{'generation':'generation-job','mask':'mask-job'})
            self.assertTrue((folder/stored['file']).exists())
            self.assertEqual([e['stage'] for e in events if e['state']=='queued'],['generation','mask'])

    def test_progress_ignores_other_jobs_and_tracks_current_protocol(self):
        from creator import progress_event
        graph = {'5': {'class_type':'KSampler'}, '6': {'class_type':'VAEDecode'}}
        event = {'type':'progress', 'data':{'prompt_id':'other','value':4,'max':20}}
        self.assertIsNone(progress_event(event,'ours',graph))
        event = {'type':'progress_state','data':{'prompt_id':'ours','nodes':{
            '5':{'state':'running','value':4,'max':20}}}}
        update = progress_event(event,'ours',graph)
        self.assertEqual(update['percent'],20)
        update = progress_event({'type':'executing','data':{'prompt_id':'ours','node':'6'}},'ours',graph)
        self.assertEqual(update['detail'],'Decoding image')
        self.assertIsNone(update['percent'])

    def test_artwork_states_have_distinct_labels_and_colors(self):
        from app import artwork_status
        a = {'file':None,'approved':False}
        self.assertEqual(artwork_status(a)[2],'missing')
        label, bar, tag = artwork_status(a,dict(state='running',stage='mask',percent=50))
        self.assertEqual(label,'Masking')
        self.assertIn('50%',bar)
        self.assertEqual(tag,'running')
        self.assertEqual(artwork_status(a,dict(state='failed'))[2],'failed')
        a.update(file='piece.png')
        self.assertEqual(artwork_status(a)[2],'review')
        a.update(mask_choice_pending=True)
        self.assertEqual(artwork_status(a)[2],'mask')
        a.update(mask_choice_pending=False,approved=True)
        self.assertEqual(artwork_status(a)[2],'approved')


if __name__ == '__main__':
    unittest.main()
