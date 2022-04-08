import os
from config import Config

import torch
import torch.optim as optim
from torch.utils.data import DataLoader

import random
import time
import numpy as np

import utils
from data import get_training_data, get_validation_data
from process import validate
from model import HWMNet, CMFNet
from tqdm import tqdm
import losses
from warmup_scheduler import GradualWarmupScheduler

opt = Config('training.yml')

gpus = ','.join([str(i) for i in opt.GPU])
os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
os.environ["CUDA_VISIBLE_DEVICES"] = gpus

# Set Seeds #
random.seed(1234)
np.random.seed(1234)
torch.manual_seed(1234)
torch.cuda.manual_seed_all(1234)
torch.backends.cudnn.benchmark = True

start_epoch = 1
mode = opt.MODEL.MODE
session = opt.MODEL.SESSION

# result_dir = os.path.join(opt.TRAINING.SAVE_DIR, mode, 'results', session)
# model_dir = os.path.join(opt.TRAINING.SAVE_DIR, mode, 'models', session)

# utils.mkdir(result_dir)
# utils.mkdir(model_dir)
utils.mkdir('pretrained_models')

train_dir = opt.TRAINING.TRAIN_DIR
val_dir = opt.TRAINING.VAL_DIR

# Model #
# model = Model()
l_net = HWMNet()
f_net = CMFNet()
l_net.cuda()
f_net.cuda()

device_ids = [i for i in range(torch.cuda.device_count())]
if torch.cuda.device_count() > 1:
    print("\n\nLet's use", torch.cuda.device_count(), "GPUs!\n\n")

new_lr = opt.OPTIM.LR_INITIAL

optimizer = optim.Adam(list(l_net.parameters()) + list(f_net.parameters()), lr=new_lr, betas=(0.9, 0.999), eps=1e-8)

# Scheduler #
warmup_epochs = 3
scheduler_cosine = optim.lr_scheduler.CosineAnnealingLR(optimizer, opt.OPTIM.NUM_EPOCHS - warmup_epochs,
                                                        eta_min=opt.OPTIM.LR_MIN)
scheduler = GradualWarmupScheduler(optimizer, multiplier=1, total_epoch=warmup_epochs, after_scheduler=scheduler_cosine)
scheduler.step()

# Resume #
# if opt.TRAINING.RESUME:
#     path_chk_rest = utils.get_last_path(model_dir, '_latest.pth')
#     utils.load_checkpoint(model, path_chk_rest)
#     start_epoch = utils.load_start_epoch(path_chk_rest) + 1
#     utils.load_optim(optimizer, path_chk_rest)
#
#     for i in range(1, start_epoch):
#         scheduler.step()
#     new_lr = scheduler.get_lr()[0]
#     print('------------------------------------------------------------------------------')
#     print("==> Resuming Training with learning rate:", new_lr)
#     print('------------------------------------------------------------------------------')

# Loss #
criterion_rl1 = losses.l1_relative
criterion_l1 = torch.nn.L1Loss()
criterion_char = losses.CharbonnierLoss()
criterion_edge = losses.EdgeLoss()
criterion_perc = losses.Perceptual()
criterion_tv = losses.total_variation_loss
criterion_cont = losses.ContrastLoss()

# DataLoaders #
train_dataset = get_training_data(train_dir, {'patch_size': opt.TRAINING.TRAIN_PS})
train_loader = DataLoader(dataset=train_dataset, batch_size=opt.OPTIM.BATCH_SIZE, shuffle=True, num_workers=16,
                          drop_last=False, pin_memory=True)

val_dataset = get_validation_data(val_dir, {'patch_size': opt.TRAINING.VAL_PS})
val_loader = DataLoader(dataset=val_dataset, batch_size=opt.OPTIM.BATCH_SIZE, shuffle=False, num_workers=16,
                        drop_last=False,
                        pin_memory=True)

print('===> Start Epoch {} End Epoch {}'.format(start_epoch, opt.OPTIM.NUM_EPOCHS + 1))
print('===> Loading datasets')

best_rmse = 1000
best_epoch = 1

for epoch in range(start_epoch, opt.OPTIM.NUM_EPOCHS + 1):
    epoch_start_time = time.time()
    epoch_loss = 0
    train_id = 1

    # Train #
    l_net.train()
    f_net.train()
    for i, data in enumerate(tqdm(train_loader), 0):
        inp = data[0].cuda()
        tar = data[1].cuda()
        mas = data[2].cuda()

        # --- Zero the parameter gradients --- #
        optimizer.zero_grad()

        # --- Forward + Backward + Optimize --- #
        stage1 = l_net(inp)
        stage1_s = stage1 * mas

        stage2 = f_net(inp)[0]
        stage2_ns = stage2 * (1 - mas)

        out = stage1_s + stage2_ns

        loss_rl1 = criterion_rl1(out, tar, mas)
        loss_tv = criterion_tv(out)
        loss_perc = criterion_perc(out, tar)

        loss = loss_rl1 + 0.02 * loss_tv + 0.04 * loss_perc

        loss.backward()
        optimizer.step()
        epoch_loss += loss.item()

    # Evaluation #
    if epoch % opt.TRAINING.VAL_AFTER_EVERY == 0:

        rmse = validate(l_net, f_net, val_loader)

        if rmse < best_rmse:
            best_rmse = rmse
            best_epoch = epoch
            torch.save({
                'epoch': best_epoch,
                'l_net': l_net.state_dict(),
                'f_net': f_net.state_dict(),
                'optimizer': optimizer.state_dict()
            }, os.path.join('pretrained_models', "model_best.pth"))

        print("[epoch %d RMSE: %.4f --- best_epoch %d Best_RMSE %.4f]" % (epoch, rmse, best_epoch, best_rmse))

    scheduler.step()
    print("------------------------------------------------------------------")
    print("Epoch: {}\tTime: {:.4f}\tLoss: {:.4f}\tLearningRate {:.8f}".format(epoch, time.time() - epoch_start_time,
                                                                              epoch_loss, scheduler.get_lr()[0]))
    print("------------------------------------------------------------------")
