"""Live server queue, independent of the character rendering worker."""
import queue
import threading
import tkinter as tk
from tkinter import ttk
from creator import Comfy


class QueueWindow(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title('ComfyUI queue')
        self.geometry('1050x430')
        self.minsize(750,350)
        self.transient(app)
        self.client = Comfy(dict(app.config_data))
        self.results = queue.Queue()
        self.loading = False
        self.jobs = []
        self.notice = ''
        self.closed = False
        ttk.Label(self,text='Live ComfyUI queue — includes jobs submitted by other windows and projects. Refreshes every 2 seconds.',wraplength=980).pack(anchor='w',padx=12,pady=8)
        self.tree = ttk.Treeview(self,columns=('state','artwork','model','id'),show='headings',selectmode='extended')
        for column, label, width in [('state','Status',80),('artwork','Artwork / output',250),('model','Model',310),('id','Job ID',310)]:
            self.tree.heading(column,text=label)
            self.tree.column(column,width=width)
        self.tree.tag_configure('Running',foreground='#a35800')
        self.tree.tag_configure('Waiting',foreground='#2266a5')
        self.tree.pack(fill='both',expand=True,padx=12)
        self.status = ttk.Label(self,text='Reading queue…',wraplength=1000)
        self.status.pack(fill='x',padx=12,pady=5)
        controls=ttk.Frame(self)
        controls.pack(fill='x',padx=12)
        self.buttons=[]
        for text,action in [('Refresh',lambda:self.refresh()),('Stop / remove selected',lambda:self.refresh('selected')),
                            ('Remove all waiting',lambda:self.refresh('waiting')),('Stop all jobs',lambda:self.refresh('all'))]:
            button=ttk.Button(controls,text=text,command=action)
            button.pack(side='left',padx=(0,6))
            self.buttons.append(button)
        ttk.Label(self,text='Stop actions also pause this window’s character batch. Running jobs are interrupted at ComfyUI’s next safe point; a GPU operation may take time to respond. Completed artwork is kept.',wraplength=1000).pack(fill='x',padx=12,pady=10)
        self.receive_timer = self.after(100,self.receive)
        self.tick_timer = self.after(2000,self.tick)
        self.refresh()

    def tick(self):
        if not self.loading:
            self.refresh()
        self.tick_timer = self.after(2000,self.tick)

    def destroy(self):
        self.closed=True
        self.after_cancel(self.receive_timer)
        self.after_cancel(self.tick_timer)
        if not self.loading:
            self.client.http.close()
        super().destroy()

    def refresh(self,action=None):
        if self.loading:
            return
        selected = set(self.tree.selection())
        if action=='selected' and not selected:
            self.status.configure(text='Select one or more jobs first.')
            return
        if action:
            self.app.cancel.set()
            self.app.log('ComfyUI queue action requested. This character batch will submit no further jobs.')
        self.loading=True
        for button in self.buttons:
            button.configure(state='disabled')
        def work():
            try:
                jobs=self.client.queue_jobs()
                notice=None
                if action:
                    targets=[job for job in jobs if action=='all' or (action=='waiting' and job['state']=='Waiting') or (action=='selected' and job['id'] in selected)]
                    # Remove waiting jobs first so they cannot start behind the interruption.
                    targets.sort(key=lambda job:job['state']=='Running')
                    sent=0
                    for job in targets:
                        sent+=bool(self.client.cancel_job(job['id']))
                    notice=f'Cancellation requested for {sent} job(s). A running job may remain visible until it responds.'
                    jobs=self.client.queue_jobs()
                self.results.put((jobs,notice,None))
            except Exception as error:
                self.results.put((None,None,str(error)))
            finally:
                if self.closed:
                    self.client.http.close()
        threading.Thread(target=work,daemon=True).start()

    def receive(self):
        while not self.results.empty():
            jobs,notice,error=self.results.get()
            self.loading=False
            for button in self.buttons:
                button.configure(state='normal')
            if error:
                self.status.configure(text='Queue request failed: '+error,foreground='#b12424')
                continue
            self.jobs=jobs
            selected=self.tree.selection()
            wanted={job['id'] for job in jobs}
            for existing in self.tree.get_children():
                if existing not in wanted:
                    self.tree.delete(existing)
            for index,job in enumerate(jobs):
                values=(job['state'],job['artwork'],job['model'],job['id'])
                if self.tree.exists(job['id']):
                    self.tree.item(job['id'],values=values,tags=(job['state'],))
                    self.tree.move(job['id'],'',index)
                else:
                    self.tree.insert('','end',iid=job['id'],values=values,tags=(job['state'],))
            self.tree.selection_set([job_id for job_id in selected if job_id in wanted])
            if notice:
                self.notice=notice
                self.app.log(notice)
            running=sum(job['state']=='Running' for job in jobs)
            summary=f'{running} running · {len(jobs)-running} waiting.'
            self.status.configure(text=summary+(' '+self.notice if jobs else ' Queue is empty.'),foreground='#23763c' if not jobs else '#222222')
        self.receive_timer = self.after(100,self.receive)
