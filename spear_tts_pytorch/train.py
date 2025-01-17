# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/A. Training.ipynb.

# %% auto 0
__all__ = ['SimpleVisual', 'train']

# %% ../nbs/A. Training.ipynb 2
import io
import time
import random
from pathlib import Path

from fastprogress import progress_bar, master_bar
import fastprogress

import numpy as np
import pylab as plt

import IPython

import torch
import torch.nn as nn
from torch.utils.data.dataloader import DataLoader
from torch.profiler import record_function

# %% ../nbs/A. Training.ipynb 3
class SimpleVisual:
    def __init__ (self, model, total_steps):
        self.model = model
        self.total_steps = total_steps
        
        gs = plt.GridSpec(2, 1, height_ratios=[3,1])
        graph_fig = plt.figure(figsize=(10,6))
        self.graph_fig = graph_fig
        self.loss_p = graph_fig.add_subplot(gs[0])
        self.lr_p = graph_fig.add_subplot(gs[1], sharex=self.loss_p)
        self.lr_p.tick_params('x', labelbottom=False)
        self.graph_out = None
        
        self.its = []
        self.train_losses = []
        self.val_losses = []
        self.lr_history = []
            
    def show(self):
        self.graph_out = display(self.graph_fig, display_id=True, clear=True)
    
    def hide(self):
        if self.graph_out is not None:
            self.graph_out.update(IPython.display.HTML(''))
    
    def plot(self):
        loss_p, lr_p = self.loss_p, self.lr_p
        loss_p.clear()
        loss_p.plot(self.its, self.train_losses)
        loss_p.plot(self.its, self.val_losses)
        loss_p.set_xlim(0, self.total_steps)
        loss_p.set_yscale('log')
        lr_p.clear()
        lrs = np.array(self.lr_history)
        lr_p.plot(self.its, lrs)
        self.graph_out.update(self.graph_fig)
    
    def add_data(self, it, lr, train_loss, val_los):
        self.its.append(it)
        self.train_losses.append(train_loss)
        self.val_losses.append(val_los)
        self.lr_history.append(lr)
        self.plot()

# %% ../nbs/A. Training.ipynb 4
def train(checkpoint_path, model, train, val, half=True, bs=16, lr=1e-4,
          weight_decay=0.1, pct_start=None, epochs=10,
          dl_workers=8, visual_class = SimpleVisual, profiler=None,
          run_valid_every_iters=8000, table_row_every_iters=80000, chkpt_every_iters=None,
          device="cuda"):
    if pct_start is None:
        # 10k updates by default
        pct_start = min(0.3, 10000 / (epochs * len(train) / bs))
    if chkpt_every_iters is None:
        chkpt_every_iters = table_row_every_iters
    
    visual = visual_class(model, epochs*len(train))
    
    Path(checkpoint_path).mkdir(exist_ok=True)

    train_loader = DataLoader(train, batch_size=bs, num_workers=dl_workers, pin_memory=True, drop_last=False, shuffle=True)
    val_loader = DataLoader(val, batch_size=bs, num_workers=dl_workers, pin_memory=True, drop_last=False)
    
    try:
        scheduler = None
        all_params = set(model.parameters())
        wd_params = set()
        for m in model.modules():
            if isinstance(m, (nn.Linear, nn.Conv1d)):
                wd_params.add(m.weight)
                if m.bias is not None:
                    wd_params.add(m.bias)
        no_wd_params = all_params - wd_params

        optimizer = torch.optim.AdamW(lr=lr, betas=(0.9, 0.95), fused=True,
            params=[
                {"params": list(wd_params), "weight_decay": weight_decay},
                {"params": list(no_wd_params), "weight_decay": 0.0},
            ]
                                     )
        scaler = torch.cuda.amp.GradScaler(enabled=half)
        scheduler = torch.optim.lr_scheduler.OneCycleLR(
            optimizer, max_lr=lr, pct_start=pct_start, steps_per_epoch=len(train_loader), epochs=epochs)
        
        it = 0
        start_t = time.time()
        next_val_it = it + 50
        next_chkpt_it = chkpt_every_iters
        next_table_it = table_row_every_iters
        
        val_loss = torch.nan
        avg_train_loss = torch.nan
        
        visual.show()

        mb = master_bar(range(epochs))
        mb.write(["samples", "train", "val", "time"], table=True)
        running_loss = [0]
        
        def add_table_row():
            elapsed_t = time.time() - start_t
            mb.write([it, f"{avg_train_loss:.5f}", f"{val_loss:.5f}", fastprogress.core.format_time(elapsed_t)], table=True)
        
        for epoch in mb:
            bar = progress_bar(train_loader, parent=mb)
            for args in bar:
                with record_function("forward"):
                    args = [x.to(device, non_blocking=True) for x in args]

                    # zero the parameter gradients
                    optimizer.zero_grad(set_to_none=True)

                    with torch.autocast(device_type=device, dtype=torch.float16 if half else torch.float32, enabled=device!='cpu'):
                        ps, loss = model(*args)

                with record_function("backward"):
                    scaler.scale(loss).backward()
                    scaler.step(optimizer)
                    scaler.update()

                    scheduler.step()

                    if profiler is not None: profiler.step()

                with record_function("running_loss"):
                    running_loss.append(loss.item())
                    running_loss = running_loss[-5:]
                    avg_train_loss = sum(running_loss)/len(running_loss)

                if it >= next_chkpt_it:
                    with record_function("checkpoint"):
                        next_chkpt_it += chkpt_every_iters
                        torch.save(model.state_dict(), f'{checkpoint_path}/{it:08d}.pt')
                    
                if it >= next_val_it:
                    next_val_it += run_valid_every_iters
                    with record_function("validation"):
                        with record_function("model.eval"):
                            model.eval()
                        with torch.no_grad():
                            val_loss = 0
                            for args in val_loader:
                                args = [x.to(device, non_blocking=True) for x in args]
                                with torch.autocast(device_type=device, dtype=torch.float16 if half else torch.float32, enabled=device!='cpu'):
                                    ps, loss = model(*args)
                                val_loss += loss
                            N = len(val_loader)
                            val_loss = val_loss.item() / N
                        with record_function("model.train"):
                            model.train()
                    with record_function("plotting"):
                        visual.add_data(it, scheduler.get_last_lr(), avg_train_loss, val_loss)
                
                if it >= next_table_it:
                    add_table_row()
                    next_table_it += table_row_every_iters

                it += bs
                bar.comment = f"#{epoch+1}/{epochs} loss: {avg_train_loss:.3f} / {val_loss:.3f}"
    except KeyboardInterrupt:
        mb.write(f"interrupted")
        mb.show()
        pass
    finally:
        add_table_row()
        mb.show()
        visual.hide()
