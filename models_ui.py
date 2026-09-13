"""Model choices from the running ComfyUI server."""
import copy
import json
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from creator import ROOT, Comfy, model_options, validate_models


class ModelSettings(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title('Models & LoRAs')
        self.geometry('850x780')
        self.minsize(650, 580)
        self.transient(app)
        self.grab_set()
        self.config_data = copy.deepcopy(app.config_data)
        self.info = None
        self.results = queue.Queue()
        self.fields = {}
        self.boxes = {}
        self.lora_panels = {}
        ttk.Label(self, text='Model settings apply to future renders in all projects. Existing artwork is kept.', wraplength=790).pack(anchor='w', padx=14, pady=(12, 0))
        ttk.Label(self, text='Use SDXL-compatible checkpoints for references; match edit models, encoders and VAEs to the Qwen or Klein workflow. Lightning and Klein distilled presets use four steps.', wraplength=790).pack(anchor='w', padx=14, pady=5)
        ttk.Button(self, text='Use installed fast pair: Lightning + Klein', command=self.fast_pair).pack(anchor='w',padx=14)
        self.status = ttk.Label(self, text='Loading model lists…')
        self.status.pack(anchor='w', padx=14)
        tabs = ttk.Notebook(self)
        tabs.pack(fill='both', expand=True, padx=14, pady=8)
        reference = ttk.Frame(tabs, padding=10)
        edit = ttk.Frame(tabs, padding=10)
        tabs.add(reference, text='Initial reference (SDXL)')
        tabs.add(edit, text='Angles & parts')
        self.choice(reference, 'Checkpoint', 'checkpoint')
        self.choice(reference, 'VAE (blank uses checkpoint VAE)', 'reference_vae')
        self.config_data.setdefault('edit_family','qwen')
        self.choice(edit, 'Edit workflow', 'edit_family')
        self.boxes['edit_family']['values'] = ['qwen','klein4b']
        self.boxes['edit_family'].bind('<<ComboboxSelected>>',self.change_family)
        self.choice(edit, 'Model loader', 'edit_loader')
        self.choice(edit, 'Image edit model', 'edit_model')
        self.choice(edit, 'Text encoder', 'text_encoder')
        self.choice(edit, 'VAE', 'vae')
        speed = ttk.Frame(edit)
        speed.pack(fill='x', pady=4)
        for key, label, default in [('edit_steps','Edit steps',20),('edit_resolution','Working size',1024),('edit_cfg','Guidance',2.5)]:
            ttk.Label(speed, text=label).pack(side='left')
            self.fields[key] = tk.StringVar(value=str(self.config_data.get(key, default)))
            ttk.Entry(speed, textvariable=self.fields[key], width=6).pack(side='left', padx=(0, 8))
        ttk.Label(edit, text='Qwen uses the entered steps/guidance. Klein distilled always uses 4 steps / guidance 1. Working size applies to both; output canvas stays unchanged.', wraplength=740).pack(anchor='w')
        self.boxes['edit_loader'].bind('<<ComboboxSelected>>', self.update_edit_models)
        self.lora_panel(reference, 'reference_loras')
        self.lora_panel(edit, 'edit_loras')
        actions = ttk.Frame(self)
        actions.pack(fill='x', padx=14, pady=(0, 12))
        self.refresh_button = ttk.Button(actions, text='Refresh from ComfyUI', command=self.refresh_models)
        self.refresh_button.pack(side='left')
        self.save_button = ttk.Button(actions, text='Save settings', command=self.commit, state='disabled')
        self.save_button.pack(side='right')
        ttk.Button(actions, text='Cancel', command=self.destroy).pack(side='right', padx=8)
        self.refresh_models()

    def choice(self, parent, label, key):
        row = ttk.Frame(parent)
        row.pack(fill='x', pady=3)
        ttk.Label(row, text=label, width=31).pack(side='left')
        self.fields[key] = tk.StringVar(value=self.config_data.get(key, ''))
        box = ttk.Combobox(row, textvariable=self.fields[key], state='readonly')
        box.pack(side='left', fill='x', expand=True)
        self.boxes[key] = box

    def fast_pair(self):
        for key,value in dict(checkpoint='sdxl_lightning_4step.safetensors',reference_vae='',
                edit_family='klein4b',edit_loader='UNETLoader',edit_model='flux-2-klein-4b-fp8.safetensors',
                text_encoder='qwen_3_4b.safetensors',vae='flux2-vae.safetensors',
                edit_steps='4',edit_cfg='1',edit_resolution='1024').items():
            self.fields[key].set(value)
        self.update_edit_models()
        for tree, box in self.lora_panels.values():
            tree.delete(*tree.get_children())
        self.status.configure(text='Fast pair selected, with empty LoRA stacks. Click Save settings to apply.')

    def change_family(self, event=None):
        if self.fields['edit_family'].get() == 'klein4b':
            values=dict(edit_loader='UNETLoader',edit_model='flux-2-klein-4b-fp8.safetensors',
                text_encoder='qwen_3_4b.safetensors',vae='flux2-vae.safetensors',edit_steps='4',edit_cfg='1')
        else:
            values=dict(edit_loader='UnetLoaderGGUF',edit_model='gguf/Qwen_Image_Edit-Q5_0.gguf',
                text_encoder='qwen_2.5_vl_7b_fp8_scaled.safetensors',vae='qwen_image_vae.safetensors',edit_steps='20',edit_cfg='2.5')
        for key,value in values.items():
            self.fields[key].set(value)
        self.update_edit_models()
        tree,_=self.lora_panels['edit_loras']
        tree.delete(*tree.get_children())
        self.status.configure(text='Matching edit model, encoder and VAE selected; edit LoRAs cleared. Save settings to apply.')

    def lora_panel(self, parent, key):
        panel = ttk.LabelFrame(parent, text='LoRAs (applied in listed order)', padding=6)
        panel.pack(fill='both', expand=True, pady=(8, 0))
        tree = ttk.Treeview(panel, columns=('name', 'model', 'clip'), show='headings', height=4, selectmode='browse')
        for col, label, width in [('name', 'LoRA', 420), ('model', 'Model strength', 95), ('clip', 'Text strength', 95)]:
            tree.heading(col, text=label)
            tree.column(col, width=width, stretch=col == 'name')
        tree.pack(fill='both', expand=True)
        for item in self.config_data.get(key, []):
            tree.insert('', 'end', values=(item['name'], item['strength_model'], item['strength_clip']))
        name = ttk.Combobox(panel, state='readonly')
        name.pack(fill='x', pady=5)
        row = ttk.Frame(panel)
        row.pack(fill='x')
        model, clip = tk.StringVar(value='1.0'), tk.StringVar(value='1.0')
        for label, var in [('Model', model), ('Text', clip)]:
            ttk.Label(row, text=label).pack(side='left')
            ttk.Entry(row, textvariable=var, width=6).pack(side='left', padx=(0, 6))
        def put(update=False):
            try:
                if not self.info or name.get() not in model_options(self.info, 'LoraLoader', 'lora_name'):
                    raise ValueError('Choose a detected LoRA first.')
                candidate = dict(name=name.get(), strength_model=model.get(), strength_clip=clip.get())
                # Validate strengths without loading or generating any artwork.
                import math
                for field in ('strength_model', 'strength_clip'):
                    value = float(candidate[field])
                    if not math.isfinite(value) or not -100 <= value <= 100:
                        raise ValueError('Strengths must be finite numbers between -100 and 100.')
                values = (candidate['name'], candidate['strength_model'], candidate['strength_clip'])
                selected = tree.selection()
                if update:
                    if not selected:
                        raise ValueError('Select a LoRA row to update.')
                    tree.item(selected[0], values=values)
                else:
                    tree.insert('', 'end', values=values)
            except ValueError as error:
                messagebox.showerror('LoRA settings', str(error), parent=self)
        def selected(event=None):
            if tree.selection():
                values = tree.item(tree.selection()[0], 'values')
                name.set(values[0]); model.set(values[1]); clip.set(values[2])
        tree.bind('<<TreeviewSelect>>', selected)
        ttk.Button(row, text='Add', command=put).pack(side='left')
        ttk.Button(row, text='Update selected', command=lambda: put(True)).pack(side='left', padx=4)
        ttk.Button(row, text='Remove', command=lambda: tree.delete(*tree.selection())).pack(side='left')
        self.lora_panels[key] = (tree, name)

    def refresh_models(self):
        self.refresh_button.configure(state='disabled')
        self.save_button.configure(state='disabled')
        self.status.configure(text='Reading model lists from ComfyUI…')
        def fetch():
            try:
                client = Comfy(self.config_data)
                try:
                    self.results.put((True, client.get('/object_info').json()))
                finally:
                    client.http.close()
            except Exception as error:
                self.results.put((False, str(error)))
        threading.Thread(target=fetch, daemon=True).start()
        self.after(100, self.receive_models)

    def receive_models(self):
        if self.results.empty():
            self.after(100, self.receive_models)
            return
        ok, result = self.results.get()
        self.refresh_button.configure(state='normal')
        if not ok:
            self.status.configure(text='Could not connect. Start ComfyUI, then refresh.', foreground='#b12424')
            return
        self.info = result
        self.boxes['edit_loader']['values'] = [kind for kind in ('UnetLoaderGGUF', 'UNETLoader') if kind in result]
        for key, kind, field in [('checkpoint','CheckpointLoaderSimple','ckpt_name'),
                                 ('text_encoder','CLIPLoader','clip_name'), ('vae','VAELoader','vae_name'),
                                 ('reference_vae','VAELoader','vae_name')]:
            values = model_options(result, kind, field)
            self.set_choices(key, ([''] if key == 'reference_vae' else []) + values)
        self.update_edit_models()
        loras = model_options(result, 'LoraLoader', 'lora_name')
        for tree, box in self.lora_panels.values():
            box['values'] = loras
        self.status.configure(text=f'Connected. {len(loras)} LoRA files detected. File compatibility depends on the selected base model.', foreground='#23763c')
        self.save_button.configure(state='normal')

    def set_choices(self, key, values):
        self.boxes[key]['values'] = values
        current = self.fields[key].get().replace('\\', '/')
        match = next((v for v in values if v.replace('\\', '/') == current), None)
        if match is not None:
            self.fields[key].set(match)

    def update_edit_models(self, event=None):
        if self.info:
            values = model_options(self.info, self.fields['edit_loader'].get(), 'unet_name')
            self.set_choices('edit_model', values)
            if event is not None and self.fields['edit_model'].get() not in values:
                self.fields['edit_model'].set('')

    def commit(self):
        try:
            updated = dict(self.config_data, **{k: v.get() for k, v in self.fields.items()})
            for key, (tree, box) in self.lora_panels.items():
                updated[key] = []
                for row in tree.get_children():
                    name, model, clip = tree.item(row, 'values')
                    updated[key].append(dict(name=name, strength_model=model, strength_clip=clip))
            updated = validate_models(updated, self.info)
            path = ROOT / 'config.json'
            temporary = path.with_suffix('.tmp.json')
            temporary.write_text(json.dumps(updated, indent=2), encoding='utf-8')
            temporary.replace(path)
            self.app.config_data.clear()
            self.app.config_data.update(updated)
            self.app.comfy.config = self.app.config_data
            self.app.log('Model and LoRA settings saved for future renders.')
            self.destroy()
        except Exception as error:
            messagebox.showerror('Model settings', str(error), parent=self)
