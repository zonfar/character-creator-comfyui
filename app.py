import json
import os
import queue
import sys
import threading
import tkinter as tk
import uuid
import shutil
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageChops, ImageDraw, ImageOps, ImageTk

from creator import ROOT, VIEWS, SECTIONS, Comfy, ComfyUnavailable, composite, export_psd, invalidate_dependents, new_project, ensure_sections, enabled_views, prepare, render_asset, save


def artwork_status(asset, progress=None, tick=0):
    progress = progress or {}
    state = progress.get('state')
    if state in ('queued', 'running', 'completed', 'failed', 'stopped'):
        tag = 'running' if state == 'completed' else state
        label = {'queued':'Queued', 'running':'Masking' if progress.get('stage') == 'mask' else 'Rendering',
                 'completed':'Finishing', 'failed':'Failed', 'stopped':'Stopped'}[state]
        percent = progress.get('percent')
        elapsed = progress.get('elapsed', 0)
        if percent is not None:
            blocks = min(10, max(0, int(percent/10)))
            bar = '█'*blocks + '░'*(10-blocks) + f' {percent:.0f}%'
        else:
            bar = ('|/-\\'[tick % 4] + f' {elapsed}s') if tag == 'running' else ''
        return label, bar, tag
    if asset.get('mask_choice_pending'):
        return 'Choose mask', '██████████', 'mask'
    if asset['approved']:
        return 'Approved', '██████████', 'approved'
    if asset['file']:
        return 'Review', '██████████', 'review'
    return 'Missing', '', 'missing'


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Character Creator · ComfyUI → Adobe PSD')
        self.geometry('1320x880')
        self.minsize(1050, 740)
        config_path = ROOT / 'config.json'
        if not config_path.exists():
            shutil.copy2(ROOT / 'config.example.json', config_path)
        self.config_data = json.loads(config_path.read_text())
        self.comfy = Comfy(self.config_data)
        self.project = None
        self.folder = None
        self.busy = False
        self.messages = queue.Queue()
        self.cancel = threading.Event()
        self.artwork_progress = {}
        self.progress_tick = 0
        self.active_artwork_id = None
        self.rendering_asset_ids = set()
        self.review_during_render = False
        self.polygon = []
        self.edit_buttons = []
        self.protocol('WM_DELETE_WINDOW', self.close)
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('TButton', padding=6)
        style.configure('TLabel', padding=3)
        top = ttk.Frame(self, padding=8)
        top.pack(fill='x')
        for text, action in [('New character', self.new), ('Open project', self.open),
                             ('Edit prompt', self.edit_prompt),
                             ('Start ComfyUI', self.start_comfy), ('Check ComfyUI', self.check), ('Open project folder', self.open_folder)]:
            self.button(top, text, action).pack(side='left', padx=3)
        self.title_label = ttk.Label(top, text='Create or open a character project')
        self.title_label.pack(side='left', padx=15)
        settings = ttk.Frame(self, padding=(8, 0, 8, 4))
        settings.pack(fill='x')
        self.button(settings, 'Models & LoRAs', self.model_settings).pack(side='left', padx=3)
        ttk.Button(settings, text='ComfyUI queue / Stop jobs', command=self.show_queue).pack(side='left',padx=3)
        panels = ttk.Panedwindow(self, orient='horizontal')
        panels.pack(fill='both', expand=True, padx=10)
        left = ttk.Frame(panels, width=360)
        right = ttk.Frame(panels)
        panels.add(left, weight=1)
        panels.add(right, weight=3)
        ttk.Label(left, text='1  Reference → 2  Angles → 3  Parts → 4  PSD', font=('Segoe UI', 10, 'bold')).pack(anchor='w')
        self.section_controls = ttk.Frame(left)
        self.section_controls.pack(fill='x')
        self.section_vars = {}
        self.section_buttons = []
        ttk.Label(left, text='Check views above; each checked view includes all its missing parts.', wraplength=450).pack(anchor='w')
        self.tree = ttk.Treeview(left, columns=('state','progress'), show='tree headings', selectmode='browse')
        self.tree.heading('#0', text='Artwork')
        self.tree.heading('state', text='Status')
        self.tree.heading('progress', text='Stage progress')
        self.tree.column('#0', width=265)
        self.tree.column('state', width=85)
        self.tree.column('progress', width=150, stretch=False)
        for tag, color in {'missing':'#777777','queued':'#2266a5','running':'#a35800',
                           'mask':'#793ba8','review':'#8b6900','approved':'#23763c',
                           'failed':'#b12424','stopped':'#8e4242'}.items():
            self.tree.tag_configure(tag, foreground=color)
        self.tree.pack(fill='both', expand=True)
        ttk.Label(left, text='Blue: queued · Orange: working · Purple: choose mask\nGold: review · Green: approved · Red: failed/stopped').pack(anchor='w')
        self.current_progress_label = ttk.Label(left, text='No artwork running', wraplength=470)
        self.current_progress_label.pack(fill='x')
        self.current_progress_bar = ttk.Progressbar(left, maximum=100)
        self.current_progress_bar.pack(fill='x', pady=3)
        self.tree.bind('<<TreeviewSelect>>', self.select)
        row = ttk.Frame(left)
        row.pack(fill='x')
        self.button(row, 'Render selected', self.render).pack(side='left')
        self.button(row, 'Import PNG', self.import_image).pack(side='left')
        self.button(row, 'Extract reference', self.extract).pack(side='left')
        row = ttk.Frame(left)
        row.pack(fill='x')
        self.button(row, 'Start batch: checked sections', self.render_sections).pack(side='left')
        ttk.Button(row, text='Stop batch', command=self.cancel.set).pack(side='left')
        self.auto_approve = tk.BooleanVar(value=False)
        auto = ttk.Checkbutton(left, text='Automatically approve new generations', variable=self.auto_approve)
        auto.pack(anchor='w', pady=(4, 0))
        self.edit_buttons.append(auto)
        self.fast_extract = tk.BooleanVar(value=False)
        fast = ttk.Checkbutton(left, text='Faster batch: extract unchanged facial features', variable=self.fast_extract)
        fast.pack(anchor='w')
        self.edit_buttons.append(fast)
        ttk.Label(left, text='Uses reference pixels for nose, eyebrows and neutral mouth; other parts still render.', wraplength=450).pack(anchor='w')
        ttk.Label(left, text='When enabled, uses the first detected mask. Applies to the next render or batch.', wraplength=340).pack(anchor='w')
        self.canvas = tk.Canvas(right, background='#222831', highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<Configure>', lambda e: self.preview())
        self.canvas.bind('<Button-1>', self.point)
        self.canvas.bind('<Button-3>', self.undo_point)
        controls = ttk.Frame(right, padding=6)
        controls.pack(fill='x')
        self.overlay = tk.BooleanVar(value=False)
        self.raw = tk.BooleanVar(value=True)
        ttk.Checkbutton(controls, text='Reference overlay', variable=self.overlay, command=self.preview).pack(side='left')
        ttk.Checkbutton(controls, text='Selected part only', variable=self.raw, command=self.preview).pack(side='left')
        self.preview_label = ttk.Label(right, text='Selected layer on transparency')
        self.preview_label.pack(anchor='w')
        candidates = ttk.Frame(right, padding=4)
        candidates.pack(fill='x')
        ttk.Label(candidates, text='Detected mask').pack(side='left')
        self.mask_candidate = ttk.Combobox(candidates, state='readonly', width=30)
        self.mask_candidate.pack(side='left')
        self.mask_candidate.bind('<<ComboboxSelected>>', self.preview_candidate)
        self.button(candidates, 'Use this mask', self.choose_mask, review=True).pack(side='left')
        self.button(candidates, 'Add mask to layer', lambda: self.choose_mask(add=True), review=True).pack(side='left')
        self.key = tk.BooleanVar()
        ttk.Checkbutton(controls, text='Remove magenta', variable=self.key).pack(side='left')
        controls = ttk.Frame(right, padding=4)
        controls.pack(fill='x')
        self.fields = {}
        for name, default, width in [('x', '0', 6), ('y', '0', 6), ('scale', '1', 5), ('tolerance', '45', 4)]:
            ttk.Label(controls, text=name.title()).pack(side='left')
            var = tk.StringVar(value=default)
            self.fields[name] = var
            ttk.Entry(controls, textvariable=var, width=width).pack(side='left')
        self.button(controls, 'Apply', self.apply).pack(side='left', padx=4)
        maskrow = ttk.Frame(right, padding=4)
        maskrow.pack(fill='x')
        self.mask_mode = tk.BooleanVar(value=False)
        ttk.Checkbutton(maskrow, text='Draw keep-area polygon (click points; right-click undo)', variable=self.mask_mode).pack(side='left')
        self.button(maskrow, 'Keep polygon', self.keep_polygon).pack(side='left')
        self.button(maskrow, 'Clear mask', self.clear_mask).pack(side='left')
        row = ttk.Frame(right, padding=4)
        row.pack(fill='x')
        self.button(row, 'Approve selected artwork', self.approve, review=True).pack(side='left')
        self.button(row, 'Mark fully occluded', self.occluded).pack(side='left')
        self.button(row, 'Export reviewed PSD', self.export).pack(side='right')
        self.button(row, 'Export draft PSD', lambda: self.export(True)).pack(side='right')
        ttk.Label(right, text='Inspect seams, eye coverage, jaw overlap and identity in every view. PSD export preserves layers; finish handles and behaviors in Character Animator.', wraplength=850).pack(anchor='w')
        self.logbox = tk.Text(self, height=6, state='disabled', background='#f1f3f5', font=('Consolas', 9))
        self.logbox.pack(fill='x', padx=10, pady=8)
        self.log('Ready. Start ComfyUI on port 8188, then create a character. No rendering is started automatically.')
        self.poll_timer = self.after(100, self.poll)

    def button(self, parent, text, command, review=False):
        def guarded():
            if self.busy and not review:
                return
            try:
                if review:
                    self.require_reviewable()
                command()
            except Exception as error:
                self.log(str(error))
                messagebox.showerror('Character Creator', str(error), parent=self)
        b = ttk.Button(parent, text=text, command=guarded)
        if not review:
            self.edit_buttons.append(b)
        return b

    def require_reviewable(self):
        if not self.busy:
            return
        asset = self.selected()
        if (not self.review_during_render or not asset or not asset['file'] or
                asset['kind'] == 'reference' or asset['id'] in self.rendering_asset_ids):
            raise ValueError('Choose a finished part to review. References and queued or running artwork stay locked until rendering finishes.')

    def log(self, text):
        self.logbox.configure(state='normal')
        self.logbox.insert('end', str(text) + '\n')
        self.logbox.see('end')
        self.logbox.configure(state='disabled')

    def worker(self, action):
        self.busy = True
        self.cancel.clear()
        for b in self.edit_buttons:
            b.configure(state='disabled')
        for b in self.section_buttons:
            b.configure(state='disabled')
        def run():
            try:
                action()
            except Exception as error:
                self.messages.put(('error', str(error)))
            finally:
                self.messages.put(('done', ''))
        threading.Thread(target=run, daemon=True).start()

    def poll(self):
        self.after_cancel(self.poll_timer)
        while not self.messages.empty():
            kind, text = self.messages.get()
            if kind == 'done':
                self.busy = False
                self.review_during_render = False
                self.rendering_asset_ids.clear()
                for b in self.edit_buttons:
                    b.configure(state='normal')
                for b in self.section_buttons:
                    b.configure(state='normal')
                for asset_id, status in list(self.artwork_progress.items()):
                    if status['state'] == 'queued':
                        self.artwork_progress.pop(asset_id)
                self.current_progress_bar.stop()
                self.current_progress_bar.configure(mode='determinate', value=0)
                self.current_progress_label.configure(text='Run finished. Check colored artwork statuses and the log for any remaining work.')
            elif kind == 'error':
                self.log(text)
                messagebox.showerror('Character Creator', text, parent=self)
            elif kind == 'artwork_saved':
                self.artwork_progress.pop(text, None)
                self.rendering_asset_ids.discard(text)
                selected = self.selected()
                if selected and selected['id'] == text:
                    self.select()
            elif kind == 'progress':
                asset_id, status = text
                self.artwork_progress[asset_id] = status
                if status['state'] in ('running','completed') and self.active_artwork_id != asset_id:
                    selected = self.selected()
                    follow = not selected or not selected['file']
                    self.active_artwork_id = asset_id
                    if follow and self.tree.exists(asset_id):
                        self.tree.selection_set(asset_id)
                        self.tree.see(asset_id)
                if status['state'] != 'queued' or status.get('prompt_id'):
                    asset = next((a for a in self.project['assets'] if a['id']==asset_id),None) if self.project else None
                    label = '/'.join(asset['path']) if asset else asset_id
                    self.current_progress_label.configure(text=f'{label}\n{status.get("stage", "").title()}: {status.get("detail", "")} · {status.get("elapsed",0)}s')
                    percent = status.get('percent')
                    if percent is None and status['state'] not in ('failed','stopped'):
                        if str(self.current_progress_bar.cget('mode')) != 'indeterminate':
                            self.current_progress_bar.configure(mode='indeterminate')
                            self.current_progress_bar.start(30)
                    else:
                        self.current_progress_bar.stop()
                        self.current_progress_bar.configure(mode='determinate',value=percent or 0)
            else:
                self.log(text)
        self.progress_tick += 1
        if self.project:
            for asset in self.project['assets']:
                if self.tree.exists(asset['id']):
                    state,bar,tag=artwork_status(asset,self.artwork_progress.get(asset['id']),self.progress_tick//5)
                    self.tree.item(asset['id'],values=(state,bar),tags=(tag,))
        self.poll_timer = self.after(100, self.poll)

    def destroy(self):
        if hasattr(self,'poll_timer'):
            self.after_cancel(self.poll_timer)
        super().destroy()

    def check(self):
        self.worker(lambda: self.messages.put(('log', self.comfy.check())))

    def model_settings(self):
        from models_ui import ModelSettings
        ModelSettings(self)

    def show_queue(self):
        from queue_ui import QueueWindow
        existing=getattr(self,'queue_window',None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return
        self.queue_window=QueueWindow(self)

    def start_comfy(self):
        self.worker(lambda: self.messages.put(('log', self.comfy.start(lambda t: self.messages.put(('log', t))))))

    def new(self):
        dialog = tk.Toplevel(self)
        dialog.title('New character')
        dialog.geometry('650x390')
        dialog.transient(self)
        dialog.grab_set()
        vars = {}
        for label, value in [('Name', 'My Character'), ('Description', 'A friendly flat 2D cartoon explorer, warm orange jacket, navy trousers, clean bold outlines'), ('Width', '1024'), ('Height', '1024')]:
            ttk.Label(dialog, text=label).pack(anchor='w', padx=14)
            vars[label] = tk.StringVar(value=value)
            ttk.Entry(dialog, textvariable=vars[label]).pack(fill='x', padx=14)
        mode = tk.StringVar(value='visemes')
        ttk.Label(dialog, text='Mouth mode').pack(anchor='w', padx=14)
        ttk.Combobox(dialog, values=['visemes', 'jaw'], textvariable=mode, state='readonly').pack(anchor='w', padx=14)
        sections = {view: tk.BooleanVar(value=True) for view in SECTIONS}
        choices = ttk.Frame(dialog)
        choices.pack(fill='x', padx=14, pady=10)
        for view, label in SECTIONS.items():
            ttk.Checkbutton(choices, text=label, variable=sections[view]).pack(side='left')
        def create():
            try:
                chosen = [v for v in SECTIONS if sections[v].get()]
                if not chosen:
                    raise ValueError('Select at least one section.')
                project = new_project(vars['Name'].get().strip() or 'Character', vars['Description'].get(), mode.get(), size=(int(vars['Width'].get()), int(vars['Height'].get())))
                project['enabled_views'] = chosen
                self.folder = ROOT / 'projects' / uuid.uuid4().hex[:10]
                self.project = project
                self.artwork_progress.clear()
                self.build_sections()
                save(project, self.folder)
                dialog.destroy()
                self.refresh()
                self.tree.selection_set('a000')
            except ValueError as e:
                messagebox.showerror('New character', str(e), parent=dialog)
        ttk.Button(dialog, text='Create project', command=create).pack(pady=5)

    def open(self):
        file = filedialog.askopenfilename(title='Open project.json', initialdir=ROOT / 'projects', filetypes=[('Character project', '*.json')])
        if file:
            self.open_project(file)

    def edit_prompt(self):
        if not self.project:
            messagebox.showinfo('Edit prompt', 'Create or open a character project first.', parent=self)
            return
        dialog = tk.Toplevel(self)
        dialog.title('Edit character prompt')
        dialog.geometry('650x420')
        dialog.minsize(440, 300)
        dialog.transient(self)
        dialog.grab_set()
        ttk.Label(dialog, text='Character description / prompt', font=('Segoe UI', 11, 'bold')).pack(anchor='w', padx=14, pady=(12, 4))
        ttk.Label(dialog, text='Used when rendering references. To change existing artwork, save your prompt and render the reference again.', wraplength=600).pack(fill='x', padx=14)
        editor = tk.Text(dialog, wrap='word', height=10, undo=True, font=('Segoe UI', 11))
        editor.pack(fill='both', expand=True, padx=14, pady=10)
        editor.insert('1.0', self.project['description'])
        editor.focus_set()
        def commit():
            description = editor.get('1.0', 'end-1c').strip()
            if not description:
                messagebox.showerror('Edit prompt', 'Enter a character description.', parent=dialog)
                return
            previous = self.project['description']
            self.project['description'] = description
            try:
                save(self.project, self.folder)
            except Exception as error:
                self.project['description'] = previous
                messagebox.showerror('Edit prompt', str(error), parent=dialog)
                return
            self.log('Character prompt saved. Future reference renders will use the updated description.')
            dialog.destroy()
        actions = ttk.Frame(dialog)
        actions.pack(fill='x', padx=14, pady=(0, 12))
        ttk.Button(actions, text='Save prompt', command=commit).pack(side='right')
        ttk.Button(actions, text='Cancel', command=dialog.destroy).pack(side='right', padx=8)
        dialog.bind('<Escape>', lambda event: dialog.destroy())

    def open_project(self, file):
        project = json.loads(Path(file).read_text(encoding='utf-8'))
        if project.get('version') != 1 or 'assets' not in project:
            raise ValueError('Select a Character Creator project.json file.')
        self.project, self.folder = project, Path(file).parent
        self.artwork_progress.clear()
        ensure_sections(project)
        save(project, self.folder)
        self.build_sections()
        self.refresh()
        self.tree.selection_set(self.project['assets'][0]['id'])

    def open_folder(self):
        os.startfile(self.folder or ROOT)

    def build_sections(self):
        for widget in self.section_controls.winfo_children():
            widget.destroy()
        self.section_vars = {}
        self.section_buttons = []
        for view in self.project['views']:
            if view not in SECTIONS and not any(a['view'] == view and a['file'] for a in self.project['assets']):
                continue
            var = tk.BooleanVar(value=view in enabled_views(self.project))
            self.section_vars[view] = var
            button = ttk.Checkbutton(self.section_controls, text=SECTIONS.get(view, view + ' (existing)'), variable=var, command=self.change_sections)
            button.pack(anchor='w')
            self.section_buttons.append(button)

    def change_sections(self):
        if self.busy:
            return
        self.project['enabled_views'] = [v for v, var in self.section_vars.items() if var.get()]
        save(self.project, self.folder)
        self.refresh()

    def refresh(self):
        if not self.project:
            return
        current = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for view in self.project['views']:
            if view not in SECTIONS and not any(a['view'] == view and a['file'] for a in self.project['assets']):
                continue
            label = SECTIONS.get(view, view)
            self.tree.insert('', 'end', iid=view, text=label, open=view in enabled_views(self.project))
        for a in self.project['assets']:
            if not self.tree.exists(a['view']):
                continue
            state,bar,tag = artwork_status(a,self.artwork_progress.get(a['id']))
            self.tree.insert(a['view'], 'end', iid=a['id'], text=' / '.join(p for p in a['path'] if p != a['view']), values=(state,bar),tags=(tag,))
        if current and self.tree.exists(current[0]):
            self.tree.selection_set(current)
        self.title_label.configure(text=f'{self.project["name"]} · {self.project["mode"]} · {len(self.project["assets"])} assets')

    def selected(self):
        selection = self.tree.selection()
        if not selection or not self.project:
            return None
        return next((a for a in self.project['assets'] if a['id'] == selection[0]), None)

    def select(self, event=None):
        a = self.selected()
        self.polygon = []
        if a:
            for k, var in self.fields.items():
                var.set(str(a[k]))
            self.key.set(a['key'])
        self.mask_candidate['values'] = [f'Candidate {i+1}' for i, _ in enumerate(a.get('mask_candidates', []))] if a else []
        self.mask_candidate.set('')
        if a and a['file'] in a.get('mask_candidates', []):
            self.mask_candidate.current(a['mask_candidates'].index(a['file']))
        self.preview()

    def preview_candidate(self, event=None):
        self.raw.set(True)
        self.preview()

    def choose_mask(self, add=False):
        a = self.selected()
        index = self.mask_candidate.current()
        if a and 0 <= index < len(a.get('mask_candidates', [])):
            selected = a['mask_candidates'][index]
            if add:
                current = Image.open(self.folder / a['file']).convert('RGBA')
                candidate = Image.open(self.folder / selected).convert('RGBA')
                if current.size != candidate.size:
                    raise ValueError('Mask canvases do not match.')
                current.putalpha(ImageChops.lighter(current.getchannel('A'), candidate.getchannel('A')))
                target = self.folder / 'artwork' / (a['id'] + '-combined-' + uuid.uuid4().hex[:8] + '.png')
                target.parent.mkdir(parents=True, exist_ok=True)
                current.save(target)
                selected = target.relative_to(self.folder).as_posix()
            a.update(file=selected, mask_choice_pending=False, approved=False, polygon=[])
            save(self.project, self.folder)
            self.refresh()
            self.select()

    def preview(self):
        a = self.selected()
        self.canvas.delete('all')
        if not a:
            self.canvas.create_text(350, 180, text='Choose artwork to preview', fill='#d8dee9', font=('Segoe UI', 18))
            return
        try:
            size = tuple(self.project['size'])
            im = Image.new('RGBA', size, '#d8dce2')
            draw = ImageDraw.Draw(im)
            for y in range(0, size[1], 32):
                for x in range(0, size[0], 32):
                    if (x // 32 + y // 32) % 2:
                        draw.rectangle((x, y, x+31, y+31), fill='#c2c9d1')
            if self.overlay.get() and a['kind'] != 'reference':
                ref = next(r for r in self.project['assets'] if r['kind'] == 'reference' and r['view'] == a['view'])
                if ref['file']:
                    ghost = prepare(self.folder, ref, size)
                    ghost.putalpha(ghost.getchannel('A').point(lambda p: int(p * .25)))
                    im.alpha_composite(ghost)
            if a['kind'] == 'reference' or self.raw.get():
                if a['file']:
                    shown = dict(a)
                    index = self.mask_candidate.current()
                    if 0 <= index < len(a.get('mask_candidates', [])) and a['mask_candidates'][index] != a['file']:
                        shown.update(file=a['mask_candidates'][index], polygon=[])
                    im.alpha_composite(prepare(self.folder, shown, size))
            else:
                im.alpha_composite(composite(self.project, self.folder, a['view'], a))
            mode = 'Selected layer' if self.raw.get() or a['kind'] == 'reference' else 'Assembled character (all visible parts)'
            self.preview_label.configure(text=mode + ' - ' + a['path'][-1] + (' - Reference overlay ON' if self.overlay.get() else ''))
            self.zoom = min(max(100, self.canvas.winfo_width()-24)/size[0], max(100, self.canvas.winfo_height()-24)/size[1])
            im.thumbnail((max(1, round(size[0]*self.zoom)), max(1, round(size[1]*self.zoom))))
            self.photo = ImageTk.PhotoImage(im)
            self.canvas.create_image(12, 12, image=self.photo, anchor='nw')
            if self.polygon:
                coords = [(12+(x*a['scale']+a['x'])*self.zoom, 12+(y*a['scale']+a['y'])*self.zoom) for x, y in self.polygon]
                for x, y in coords:
                    self.canvas.create_oval(x-3, y-3, x+3, y+3, fill='#ffbb00')
                if len(coords) > 1:
                    self.canvas.create_line(*[c for p in coords for c in p], fill='#ffbb00', width=2)
        except (OSError, ValueError) as error:
            self.canvas.create_text(300, 150, text=str(error), fill='white', width=550)

    def apply(self):
        a = self.selected()
        if not a:
            return
        self.require_committed_mask(a)
        values = {k: float(v.get()) for k, v in self.fields.items()}
        if not .05 <= values['scale'] <= 4 or not 0 <= values['tolerance'] <= 240:
            raise ValueError('Scale: 0.05–4. Tolerance: 0–240.')
        if any(abs(values[k]) > 8192 for k in ('x', 'y')):
            raise ValueError('Offsets must be within ±8192 pixels.')
        changed = any(a[k] != v for k, v in values.items()) or a['key'] != self.key.get()
        a.update(values, key=self.key.get())
        if changed:
            a['approved'] = False
            invalidate_dependents(self.project, a)
        save(self.project, self.folder)
        self.refresh()
        self.preview()

    def point(self, event):
        a = self.selected()
        if self.busy or not self.mask_mode.get() or not a or not a['file']:
            return
        x = ((event.x-12)/self.zoom-a['x'])/a['scale']
        y = ((event.y-12)/self.zoom-a['y'])/a['scale']
        self.polygon.append((round(x), round(y)))
        self.preview()

    def undo_point(self, event):
        if self.polygon:
            self.polygon.pop()
            self.preview()

    def keep_polygon(self):
        a = self.selected()
        if a and len(self.polygon) >= 3:
            self.require_committed_mask(a)
            a.update(polygon=list(self.polygon), approved=False)
            invalidate_dependents(self.project, a)
            save(self.project, self.folder)
            self.polygon = []
            self.refresh()
            self.preview()

    def clear_mask(self):
        a = self.selected()
        if a:
            a.update(polygon=[], approved=False)
            invalidate_dependents(self.project, a)
            self.polygon = []
            save(self.project, self.folder)
            self.refresh()
            self.preview()

    def require_committed_mask(self, a):
        index = self.mask_candidate.current()
        if 0 <= index < len(a.get('mask_candidates', [])) and a['mask_candidates'][index] != a['file']:
            raise ValueError('Click Use this mask to select the previewed candidate before editing or approving it.')

    def approve(self):
        a = self.selected()
        if a and a['file']:
            if a.get('mask_choice_pending'):
                raise ValueError('Review the detected masks and click Use this mask before approving.')
            # Persist visible controls before approval, so approval covers the shown settings.
            self.apply()
            if not prepare(self.folder, a, self.project['size']).getbbox() and not a.get('occluded'):
                raise ValueError('Artwork is empty. Adjust placement or mark a hidden far-side feature occluded.')
            a['approved'] = True
            save(self.project, self.folder)
            self.refresh()

    def occluded(self):
        a = self.selected()
        if not a or a['kind'] != 'part' or a['view'] == 'Frontal':
            raise ValueError('Occlusion is for invisible features in turned views only.')
        if not any(word in '/'.join(a['path']) for word in ('Eye', 'Pupil', 'Blink', 'Nose')):
            raise ValueError('Only fully hidden facial features can be marked occluded.')
        target = self.folder / 'artwork' / (a['id'] + '-occluded.png')
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGBA', tuple(self.project['size'])).save(target)
        a.update(file=target.relative_to(self.folder).as_posix(), occluded=True, approved=True, polygon=[])
        save(self.project, self.folder)
        self.refresh()
        self.preview()

    def import_image(self):
        a = self.selected()
        if not a:
            return
        file = filedialog.askopenfilename(filetypes=[('Artwork', '*.png *.webp *.jpg *.jpeg')])
        if not file:
            return
        im = Image.open(file).convert('RGBA')
        if a['kind'] == 'reference' and im.size != tuple(self.project['size']):
            fitted = ImageOps.contain(im, tuple(self.project['size']), Image.Resampling.LANCZOS)
            im = Image.new('RGBA', tuple(self.project['size']), '#cccccc')
            im.alpha_composite(fitted, ((im.width-fitted.width)//2, (im.height-fitted.height)//2))
            self.log('Reference fitted and padded to the project canvas without stretching.')
        target = self.folder / 'artwork' / (a['id'] + '-' + uuid.uuid4().hex[:8] + '.png')
        target.parent.mkdir(parents=True, exist_ok=True)
        im.save(target)
        a.update(file=target.relative_to(self.folder).as_posix(), approved=False, key=False, polygon=[], x=0, y=0, scale=1, occluded=False, mask_candidates=[], mask_choice_pending=False)
        invalidate_dependents(self.project, a)
        save(self.project, self.folder)
        self.refresh()
        self.select()

    def render(self):
        a = self.selected()
        if a:
            self.run_assets([a])

    def extract(self):
        a = self.selected()
        if a and a['kind'] == 'part':
            self.run_assets([a], extract=True)

    def render_view(self):
        a = self.selected()
        selection = self.tree.selection()
        view = a['view'] if a else selection[0] if selection else None
        if not self.project or view not in self.project['views']:
            return
        assets = [p for p in self.project['assets'] if p['view'] == view and p['kind'] == 'part' and not p['file']]
        if not assets:
            self.log(f'No missing parts in {view}. Finish any pending mask choices and artwork reviews.')
            return
        source = next(p for p in self.project['assets'] if p['view'] == view and p['kind'] == 'reference')
        if not source['file'] or not source['approved']:
            raise ValueError(f'Render/import and approve the {view} reference first.')
        self.log(f'{len(assets)} missing parts queued in {view}. Mask choices and approvals can wait until the batch finishes. Stop batch prevents further submissions.')
        self.run_assets(assets, batch=True)

    def render_sections(self):
        if not self.project:
            return
        chosen = enabled_views(self.project)
        if not chosen:
            raise ValueError('Check at least one section to render.')
        assets = [a for a in self.project['assets'] if a['view'] in chosen and a['kind'] == 'part' and not a['file']]
        if not assets:
            self.log('No missing parts in the checked sections. Existing artwork and mask candidates are preserved.')
            return
        needed = {a['view'] for a in assets}
        missing = [SECTIONS.get(a['view'], a['view']) for a in self.project['assets'] if a['kind'] == 'reference' and a['view'] in needed and (not a['file'] or not a['approved'])]
        if missing:
            raise ValueError('Render/import and approve the reference for these sections first: ' + ', '.join(missing) + '. Select each Reference and click Render selected. The Front reference is the identity source for turned references, even if Front is unchecked.')
        self.log(f'{len(assets)} missing parts queued across checked sections. Unchecked sections will not be rendered or required for PSD export.')
        self.run_assets(assets, batch=True)

    def run_assets(self, assets, extract=False, batch=False):
        self.active_artwork_id = None
        self.rendering_asset_ids = {a['id'] for a in assets}
        self.review_during_render = True
        auto_approve = self.auto_approve.get()
        fast_extract = batch and hasattr(self, 'fast_extract') and self.fast_extract.get()
        for a in assets:
            self.messages.put(('progress',(a['id'],dict(state='queued',detail='Waiting for earlier artwork',percent=None))))
        def work():
            self.messages.put(('log', self.comfy.check()))
            self.messages.put(('log', 'Automatic approval enabled: new artwork will use the first detected mask.' if auto_approve else 'Manual review enabled: mask choices and approval remain pending while rendering continues.'))
            saved = 0
            failed = []
            for index, a in enumerate(assets, 1):
                if self.cancel.is_set():
                    break
                label = a['view'] + ' / ' + '/'.join(a['path'])
                self.messages.put(('log', f'Rendering {index}/{len(assets)}: {label}'))
                try:
                    use_extract = extract or (fast_extract and a['path'][-1] in ('Nose', '+Left Eyebrow', '+Right Eyebrow', 'Neutral'))
                    if use_extract:
                        self.messages.put(('log', 'Extracting original reference pixels; skipping the Qwen edit for this piece.'))
                    render_asset(self.project, self.folder, a, self.comfy, lambda t: self.messages.put(('log', t)), self.cancel, extract=use_extract, auto_approve=auto_approve,
                        progress=lambda event, asset_id=a['id']: self.messages.put(('progress',(asset_id,event))))
                except (ComfyUnavailable, TimeoutError) as error:
                    self.messages.put(('progress',(a['id'],dict(state='failed',detail=str(error),percent=None))))
                    raise
                except Exception as error:
                    if self.cancel.is_set():
                        self.messages.put(('progress',(a['id'],dict(state='stopped',detail=str(error),percent=None))))
                        break
                    self.messages.put(('progress',(a['id'],dict(state='failed',detail=str(error),percent=None))))
                    if not batch:
                        raise
                    failed.append(label)
                    self.messages.put(('log', f'Failed: {label}: {error}. Continuing with the next part.'))
                    continue
                saved += 1
                self.messages.put(('artwork_saved', a['id']))
                if auto_approve:
                    self.messages.put(('log', 'Saved and automatically approved. First detected mask selected when available.'))
                elif a.get('mask_choice_pending'):
                    self.messages.put(('log', 'Saved with mask choices pending. Review them after rendering.'))
                else:
                    self.messages.put(('log', 'Saved for later alignment and transparency review.'))
            if batch:
                status = 'Batch stopped' if self.cancel.is_set() else 'Batch rendering finished'
                remaining = sum(not a['file'] for a in assets)
                review_note = 'Successful new generations were automatically approved.' if auto_approve else 'Mask choices and approvals remain separate.'
                self.messages.put(('log', f'{status}: {saved} saved, {len(failed)} failed, {remaining} still missing. {review_note}'))
                if failed:
                    self.messages.put(('log', 'Retry missing parts to retry: ' + '; '.join(failed)))
        self.worker(work)

    def export(self, draft=False):
        if not self.project:
            return
        file = filedialog.asksaveasfilename(initialdir=self.folder, initialfile='character-draft.psd' if draft else 'character.psd', defaultextension='.psd', filetypes=[('Photoshop', '*.psd')])
        if file:
            def work():
                path = export_psd(self.project, self.folder, file, draft)
                self.messages.put(('log', f'Saved {path}. Read the adjacent rigging notes before importing.'))
            self.worker(work)

    def close(self):
        if self.busy:
            self.log('Use Stop batch and wait for the current request to finish before closing.')
            self.cancel.set()
            return
        self.destroy()


if __name__ == '__main__':
    app = App()
    if len(sys.argv) == 3 and sys.argv[1] == '--project':
        app.open_project(sys.argv[2])
    app.mainloop()
