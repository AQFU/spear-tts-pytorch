# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/A. Training (Lightning).ipynb.

# %% auto 0
__all__ = []

# %% ../nbs/A. Training (Lightning).ipynb 2
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

# %% ../nbs/A. Training (Lightning).ipynb 3
import lightning.pytorch as pl
import math

class TrainingTask(pl.LightningModule):
    def __init__(self, model, model_hparams=None):
        super().__init__()
        self.model = model
        self.model_hparams = model_hparams
    
    def configure_optimizers(self):
        """ Initialize AdamW optimizer"""
        all_params = set(self.model.parameters())
        wd_params = set()
        for m in self.model.modules():
            if isinstance(m, (nn.Linear, nn.Conv1d)):
                wd_params.add(m.weight)
                if m.bias is not None:
                    wd_params.add(m.bias)
        no_wd_params = all_params - wd_params

        optimizer = torch.optim.AdamW(lr=self.model_hparams['lr0'], betas=(0.9, 0.95), fused=True,
            params=[
                {"params": list(wd_params), "weight_decay": self.model_hparams['weight_decay']},
                {"params": list(no_wd_params), "weight_decay": 0.0},
            ]
        )
        
        # modified from https://github.com/Lightning-AI/lightning/issues/5449#issuecomment-1501597319
        def num_steps_per_epoch() -> int:
            """Get number of steps"""
            # Accessing _data_source is flaky and might break
            dataset = self.trainer.fit_loop._data_source.dataloader()
            dataset_size = len(dataset)
            num_devices = max(1, self.trainer.num_devices)
            # math.ceil so always overestimate (underestimating throws exceptions)
            num_steps = math.ceil(dataset_size / (self.trainer.accumulate_grad_batches * num_devices))
            return num_steps
        
        if self.model_hparams['pct_start'] is None:
            # 10k updates by default
            total_steps = self.model_hparams['epochs'] * num_steps_per_epoch()
            self.model_hparams['pct_start'] = min(0.3, 10000 / total_steps)

        lr_scheduler = torch.optim.lr_scheduler.OneCycleLR(
            optimizer,
            pct_start=self.model_hparams['pct_start'],
            max_lr=self.model_hparams['lr0'],
            steps_per_epoch=num_steps_per_epoch(),
            epochs=self.model_hparams['epochs']
        )

        return [optimizer], [{'scheduler': lr_scheduler, 'interval': 'step'}]
    
    def training_step(self, train_batch, batch_idx):
        x, y = train_batch
        train_logits, train_loss = self.model.forward(x, y)

        self.log("train_loss", train_loss, sync_dist=True)
        return train_loss
    
    def validation_step(self, val_batch, batch_idx):
        x, y = val_batch
        val_logits, val_loss = self.model.forward(x, y)

        self.log("val_loss", val_loss, sync_dist=True)
        return val_loss
    
    def test_step(self, val_batch, batch_idx):
        x, y = val_batch
        test_logits, test_loss = self.model.forward(x, y)

        self.log("test_loss", test_loss, sync_dist=True)
        return test_loss

# %% ../nbs/A. Training (Lightning).ipynb 4
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('--task', type=str, help='Task to train')
parser.add_argument('--seed', type=int, default=0, help='Global training seed')
parser.add_argument('--batch-size', type=int, default=16, help='total batch size for all GPUs')
parser.add_argument('--workers', type=int, default=8, help='max dataloader workers (per RANK in DDP mode)')
parser.add_argument('--input-dir', type=str, default='', help='input data path') # fixed in the model for now
parser.add_argument("--checkpoint-dir", type=str, default="./checkpoints/", help="directory to save the checkpoints")
parser.add_argument('--epochs', type=int, default=10, help='total training epochs')
parser.add_argument('--weight-decay', type=float, default=1e-2, help='optimizer weight decay')
parser.add_argument('--lr0', type=float, default=1e-4, help='optimizer initial learning rate')
parser.add_argument('--pct-start', type=float, default=None, help='optimizer percentage of total number of epochs when learning rate rises during one cycle (defaults to 10k updates)')
parser.add_argument('--model-size', type=str, default='small', help='model size')

args = parser.parse_args().__dict__

task_name: str = args.pop("task")
input_dir: str = args.pop("input_dir")
model_size: str = args.pop("model_size")
checkpoint_dir: str = args.pop("checkpoint_dir")
num_workers: int = args.pop("workers")
batch_size: int = args.pop("batch_size")
epochs: int = args.pop("epochs")

hyp_params = {}
hyp_params['pct_start'] = args['pct_start']
hyp_params['weight_decay'] = args['weight_decay']
hyp_params['lr0'] = args['lr0']
hyp_params['epochs'] = epochs

# %% ../nbs/A. Training (Lightning).ipynb 5
from lightning.pytorch.loggers import WandbLogger
from lightning.pytorch.callbacks import LearningRateMonitor
import importlib

torch.set_float32_matmul_precision('medium')

wandb_logger = WandbLogger(project=f"SpearTTS-{task_name}")

ckpt_callback = pl.callbacks.ModelCheckpoint(
     dirpath=f'{task_name}-{epochs}e',
     filename=task_name+"-{epoch}-{step}-{val_loss:.2f}",
     monitor="val_loss",
     save_top_k=4,
     every_n_epochs=1,
 )

lr_monitor_callback = LearningRateMonitor(logging_interval='step')

from torch.utils.data import DataLoader

task = importlib.import_module("spear_tts_pytorch."+task_name)

train_ds, val_ds = task.load_datasets(input_dir)

val_loader = DataLoader(val_ds,
    batch_size=batch_size,
    num_workers=num_workers,
    drop_last=False,
    pin_memory=True)

train_loader = DataLoader(train_ds,
    batch_size=batch_size,
    num_workers=num_workers,
    drop_last=False,
    shuffle=True,
    pin_memory=True)

model = task.make_model(model_size) 

task = TrainingTask(model, model_hparams=hyp_params)

trainer = pl.Trainer(max_epochs=hyp_params['epochs'],
                  accelerator="gpu",
                  profiler="simple",
                  precision='16-mixed',
                  enable_checkpointing=True,
                  logger=wandb_logger,
                  callbacks=[ckpt_callback, lr_monitor_callback])

trainer.fit(model=task, train_dataloaders=train_loader, val_dataloaders=val_loader)
